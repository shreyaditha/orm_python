"""Model base class, metaclass, and the global model registry.

Design decision
---------------
A metaclass (not ``__init_subclass__``) is used because it runs *before* the
class object exists, which is when we need to:

1. Pull ``Field`` instances out of the class dict so they do not stay as
   raw class attributes.
2. Install descriptors in their place.
3. Invent an implicit ``PrimaryKeyField`` if the user omitted one.
4. Register the class so ForeignKey('User') string references can resolve.

Django's ORM does the same; SQLAlchemy originally used a similar metaclass
on ``declarative_base()`` and later added ``__init_subclass__`` for 2.0.
The metaclass form is the one interviewers expect you to be able to draw.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterator

from pyorm.connection import Engine, get_engine
from pyorm.exceptions import ConfigurationError, FieldError
from pyorm.fields import (
    NOT_PROVIDED,
    Field,
    ForeignKey,
    ManyToManyField,
    PrimaryKeyField,
)

if TYPE_CHECKING:
    from pyorm.dialect import Dialect
    from pyorm.query import QuerySet
    from pyorm.session import Session


class InstanceState:
    """Per-instance bookkeeping used by Session and descriptors."""

    __slots__ = ("session", "initializing", "modified", "persistent", "deleted")

    def __init__(self) -> None:
        self.session: Session | None = None
        self.initializing: bool = True
        self.modified: set[str] = set()
        self.persistent: bool = False
        self.deleted: bool = False

    def snapshot_clean(self) -> None:
        self.modified.clear()
        self.persistent = True
        self.deleted = False


class ModelOptions:
    """Collected metadata for one concrete model (the ``_meta`` object)."""

    def __init__(self, model: type[Model], attrs: dict[str, Any]) -> None:
        self.model = model
        meta_cfg = attrs.pop("Meta", None) or getattr(model, "Meta", None)
        self.table_name: str = getattr(meta_cfg, "table_name", None) or _default_table_name(
            model.__name__
        )
        self.abstract: bool = getattr(meta_cfg, "abstract", False)
        self.indexes: list[tuple[str, ...]] = list(getattr(meta_cfg, "indexes", []) or [])
        self.fields: dict[str, Field] = {}
        self.m2m: dict[str, ManyToManyField] = {}
        self.pk: Field | None = None

    def add_field(self, name: str, field: Field) -> None:
        self.fields[name] = field
        if field.primary_key:
            self.pk = field

    def get_field(self, name: str) -> Field:
        if name in self.fields:
            return self.fields[name]
        for field in self.fields.values():
            if field.attname == name or field.column == name:
                return field
        raise FieldError(f"{self.model.__name__} has no field {name!r}")

    def concrete_fields(self) -> list[Field]:
        """Columns that actually exist on this table (excludes M2M).

        ForeignKey is indexed under both ``author`` and ``author_id``; we
        de-duplicate by SQL column so CREATE TABLE does not emit it twice.
        """
        seen: set[str] = set()
        unique: list[Field] = []
        for field in sorted(self.fields.values(), key=lambda f: f._creation_counter):
            if field.column in seen:
                continue
            seen.add(field.column)
            unique.append(field)
        return unique

    def fk_fields(self) -> list[ForeignKey]:
        return [f for f in self.fields.values() if isinstance(f, ForeignKey)]

    def create_table_sql(self, dialect: Dialect) -> str:
        columns = [f.ddl(dialect) for f in self.concrete_fields()]
        body = ",\n  ".join(columns)
        return f"CREATE TABLE IF NOT EXISTS {dialect.quote(self.table_name)} (\n  {body}\n)"

    def schema_state(self) -> dict[str, Any]:
        return {
            "table": self.table_name,
            "fields": {
                name: field.schema_state()
                for name, field in self.fields.items()
                if name == field.name
            },
            "m2m": {name: field.schema_state() for name, field in self.m2m.items()},
            "indexes": [list(ix) for ix in self.indexes],
        }


def _default_table_name(class_name: str) -> str:
    chars: list[str] = []
    for i, ch in enumerate(class_name):
        if ch.isupper() and i:
            chars.append("_")
        chars.append(ch.lower())
    return "".join(chars)


class ModelRegistry:
    """Process-wide map of model name → class.

    Finalize is idempotent and must run after every model class in the
    application has been imported: that is when string FK targets and reverse
    accessors can be wired.
    """

    def __init__(self) -> None:
        self._models: dict[str, type[Model]] = {}
        self._finalized = False

    def register(self, model: type[Model]) -> None:
        self._models[model.__name__] = model
        self._finalized = False

    def clear(self) -> None:
        """Drop all registered models. Used by the test suite."""
        self._models.clear()
        self._finalized = False

    def get(self, name: str) -> type[Model]:
        try:
            return self._models[name]
        except KeyError as exc:
            raise ConfigurationError(f"No model registered with name {name!r}") from exc

    def __iter__(self) -> Iterator[type[Model]]:
        return iter(self._models.values())

    def concrete_models(self) -> list[type[Model]]:
        return [m for m in self._models.values() if not m._meta.abstract]

    def finalize(self) -> None:
        if self._finalized:
            return
        for model in self.concrete_models():
            for field in model._meta.fk_fields():
                field.related_model()  # resolve strings
                _install_reverse_fk(field)
            for name, m2m in list(model._meta.m2m.items()):
                _install_m2m(model, name, m2m)
        self._finalized = True

    def schema_state(self) -> dict[str, Any]:
        self.finalize()
        return {m.__name__: m._meta.schema_state() for m in self.concrete_models()}


registry = ModelRegistry()


class ModelMeta(type):
    """Collect Field attributes and produce ``Model._meta``."""

    def __new__(
        mcs,
        name: str,
        bases: tuple[type, ...],
        attrs: dict[str, Any],
        **kwargs: Any,
    ) -> type:
        if name == "Model" and attrs.get("__module__") == "pyorm.models":
            return super().__new__(mcs, name, bases, attrs)

        fields: dict[str, Field] = {}
        m2m_fields: dict[str, ManyToManyField] = {}
        for base in bases:
            base_meta = getattr(base, "_meta", None)
            if base_meta is None:
                continue
            for fname, field in base_meta.fields.items():
                fields[fname] = field
            for fname, field in base_meta.m2m.items():
                m2m_fields[fname] = field

        declared: list[tuple[str, Field | ManyToManyField]] = []
        for key, value in list(attrs.items()):
            if isinstance(value, Field):
                declared.append((key, value))
            elif isinstance(value, ManyToManyField):
                declared.append((key, value))
        declared.sort(key=lambda item: item[1]._creation_counter)

        new_class = super().__new__(mcs, name, bases, attrs)
        meta = ModelOptions(new_class, attrs)
        new_class._meta = meta  # type: ignore[attr-defined]

        for fname, field in fields.items():
            meta.add_field(fname, field)
        for fname, field in m2m_fields.items():
            meta.m2m[fname] = field

        for fname, value in declared:
            if isinstance(value, ManyToManyField):
                value.contribute_to_class(new_class, fname)
                meta.m2m[fname] = value
            else:
                value.contribute_to_class(new_class, fname)
                meta.add_field(fname, value)
                # ForeignKey is stored under its relation name; also index attname.
                if isinstance(value, ForeignKey):
                    meta.fields[value.attname] = value  # type: ignore[assignment]

        if not meta.abstract and meta.pk is None:
            pk = PrimaryKeyField()
            pk.contribute_to_class(new_class, "id")
            meta.add_field("id", pk)

        if not meta.abstract:
            registry.register(new_class)
        return new_class


class Model(metaclass=ModelMeta):
    """Base class for every mapped table.

    User code::

        class User(Model):
            name = CharField(max_length=80)

        session.add(User(name="Ada"))
        session.commit()
    """

    _meta: ModelOptions
    _state: InstanceState

    def __init__(self, **kwargs: Any) -> None:
        self._state = InstanceState()
        self._state.initializing = True
        provided = set(kwargs)
        for field in self._meta.concrete_fields():
            if isinstance(field, ForeignKey):
                if field.name in kwargs:
                    setattr(self, field.name, kwargs[field.name])
                    continue
                if field.attname in kwargs:
                    setattr(self, field.attname, kwargs[field.attname])
                    continue
                if field.has_default():
                    setattr(self, field.attname, field.get_default())
                else:
                    self.__dict__[field.attname] = None
                continue
            if field.name in kwargs:
                setattr(self, field.name, kwargs[field.name])
            elif field.has_default():
                setattr(self, field.name, field.get_default())
            else:
                self.__dict__[field.attname] = None
        extra = provided - set(self._meta.fields) - {f.name for f in self._meta.fk_fields()}
        extra -= {f.attname for f in self._meta.concrete_fields()}
        extra -= set(self._meta.m2m)
        if extra:
            raise FieldError(f"Unknown field(s) for {type(self).__name__}: {sorted(extra)}")
        self._state.initializing = False
        self._state.modified.clear()

    @property
    def pk(self) -> Any:
        if self._meta.pk is None:
            return None
        return getattr(self, self._meta.pk.attname, None)

    @pk.setter
    def pk(self, value: Any) -> None:
        assert self._meta.pk is not None
        setattr(self, self._meta.pk.attname, value)

    def __repr__(self) -> str:
        pk = self.pk
        return f"<{type(self).__name__} pk={pk!r}>"

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, type(self)):
            return NotImplemented
        if self.pk is None or other.pk is None:
            return self is other
        return self.pk == other.pk

    def __hash__(self) -> int:
        return hash((type(self), self.pk)) if self.pk is not None else id(self)

    @classmethod
    def query(cls, session: Session | None = None) -> QuerySet:
        """Return a lazy QuerySet for this model."""
        from pyorm.query import QuerySet

        return QuerySet(cls, session=session)

    objects: Any  # patched below as a manager descriptor


class ManagerDescriptor:
    """``User.objects`` → QuerySet bound to the default engine / current session."""

    def __get__(self, instance: Model | None, owner: type[Model] | None = None) -> QuerySet:
        if owner is None:
            raise ConfigurationError("Manager accessed without a model")
        from pyorm.query import QuerySet
        from pyorm.session import current_session

        return QuerySet(owner, session=current_session())


Model.objects = ManagerDescriptor()  # type: ignore[misc]


def _install_reverse_fk(field: ForeignKey) -> None:
    from pyorm.descriptors import ReverseManyDescriptor

    related = field.related_model()
    name = field.related_name
    if name is None:
        name = f"{field.model.__name__.lower()}_set"  # type: ignore[union-attr]
    if name == "+":
        return
    if hasattr(related, name) and not isinstance(
        getattr(related, name), ReverseManyDescriptor
    ):
        # Already a real attribute (user-defined); skip unless it's ours.
        existing = getattr(type(related), name, None)
        if not isinstance(existing, ReverseManyDescriptor):
            return
    setattr(related, name, ReverseManyDescriptor(field))


def _install_m2m(model: type[Model], name: str, field: ManyToManyField) -> None:
    from pyorm.descriptors import ManyToManyDescriptor

    related = field.related_model()
    if field.through is None:
        field.through = _create_through_model(model, related, field)
    reverse = field.related_name or f"{model.__name__.lower()}_set"
    if reverse != "+" and not isinstance(getattr(related, reverse, None), ManyToManyDescriptor):
        reverse_field = ManyToManyField(model, related_name="+", through=field.through)
        reverse_field.name = reverse
        reverse_field.model = related
        setattr(related, reverse, ManyToManyDescriptor(reverse_field, reverse=True, source=field))


def _create_through_model(
    left: type[Model],
    right: type[Model],
    field: ManyToManyField,
) -> type[Model]:
    table = f"{left._meta.table_name}_{field.name}"
    class_name = f"{left.__name__}{right.__name__}Through"

    class Meta:
        table_name = table

    attrs: dict[str, Any] = {
        "__module__": left.__module__,
        "Meta": Meta,
        left.__name__.lower(): ForeignKey(left, related_name="+"),
        right.__name__.lower(): ForeignKey(right, related_name="+"),
    }
    through = ModelMeta(class_name, (Model,), attrs)
    return through  # type: ignore[return-value]


def create_all(engine: Engine | None = None) -> None:
    """CREATE TABLE for every registered model (tests / quickstart).

    Production schema changes should go through ``pyorm migrate``. This helper
    exists so unit tests do not have to generate migration files.
    """
    engine = engine or get_engine()
    registry.finalize()
    dialect = engine.dialect
    for model in _topological_models():
        engine.execute(model._meta.create_table_sql(dialect))
        for field in model._meta.concrete_fields():
            if field.index and not field.unique and not field.primary_key:
                ix = f"ix_{model._meta.table_name}_{field.column}"
                sql = (
                    f"CREATE INDEX IF NOT EXISTS {dialect.quote(ix)} "
                    f"ON {dialect.quote(model._meta.table_name)} ({dialect.quote(field.column)})"
                )
                engine.execute(sql)
        for cols in model._meta.indexes:
            ix = f"ix_{model._meta.table_name}_{'_'.join(cols)}"
            col_sql = ", ".join(dialect.quote(c) for c in cols)
            sql = (
                f"CREATE INDEX IF NOT EXISTS {dialect.quote(ix)} "
                f"ON {dialect.quote(model._meta.table_name)} ({col_sql})"
            )
            engine.execute(sql)


def drop_all(engine: Engine | None = None) -> None:
    engine = engine or get_engine()
    registry.finalize()
    dialect = engine.dialect
    for model in reversed(_topological_models()):
        engine.execute(dialect.drop_table_sql(model._meta.table_name))


def _topological_models() -> list[type[Model]]:
    """Create referenced (parent) tables before tables that FK to them."""
    models = registry.concrete_models()
    remaining = {m.__name__: m for m in models}
    ordered: list[type[Model]] = []
    visiting: set[str] = set()

    def visit(name: str) -> None:
        if name not in remaining:
            return
        if name in visiting:
            return
        visiting.add(name)
        model = remaining[name]
        for fk in model._meta.fk_fields():
            target = fk.related_model()
            if target.__name__ in remaining and target is not model:
                visit(target.__name__)
        ordered.append(model)
        remaining.pop(name, None)

    for name in list(remaining):
        visit(name)
    return ordered
