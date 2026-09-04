"""Field types declared as class attributes on Model subclasses.

Each field is both a schema declaration (SQL type, constraints, defaults)
and a Python descriptor once the metaclass binds it onto the model. The
descriptor is what gives us dirty-field tracking: assignment after
construction marks the field as modified so UPDATE statements only include
changed columns.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any

from pyorm.dialect import Dialect
from pyorm.exceptions import ValidationError

if TYPE_CHECKING:
    from pyorm.models import Model

NOT_PROVIDED: Any = object()


class Field:
    """Base column definition.

    Parameters
    ----------
    null:
        When True, SQL NULL / Python None are allowed.
    default:
        Static value or zero-arg callable used when the constructor omits
        this field. ``NOT_PROVIDED`` means "no default" (NULL if null=True,
        otherwise the insert must supply a value).
    unique:
        Emit a UNIQUE constraint.
    index:
        Request a non-unique index (created at CREATE TABLE / migrate time).
    primary_key:
        Mark this column as the model's primary key.
    db_column:
        Override the SQL column name (defaults to the Python attribute name).
    """

    def __init__(
        self,
        *,
        null: bool = False,
        default: Any = NOT_PROVIDED,
        unique: bool = False,
        index: bool = False,
        primary_key: bool = False,
        db_column: str | None = None,
    ) -> None:
        self.null = null
        self.default = default
        self.unique = unique
        self.index = index
        self.primary_key = primary_key
        self.db_column = db_column
        self.name: str = ""
        self.model: type[Model] | None = None
        self._creation_counter = _next_creation_counter()

    @property
    def attname(self) -> str:
        """Python attribute that stores the raw (usually scalar) value."""
        return self.name

    @property
    def column(self) -> str:
        return self.db_column or self.name

    def contribute_to_class(self, cls: type[Model], name: str) -> None:
        """Called by the model metaclass when this field is bound."""
        self.name = name
        self.model = cls
        if self.db_column is None:
            self.db_column = name
        setattr(cls, name, FieldDescriptor(self))

    def has_default(self) -> bool:
        return self.default is not NOT_PROVIDED

    def get_default(self) -> Any:
        if not self.has_default():
            return None
        value = self.default
        return value() if callable(value) else value

    def validate(self, value: Any) -> None:
        if value is None:
            if self.null or self.primary_key:
                return
            if self.has_default():
                return
            raise ValidationError(f"{self.model.__name__}.{self.name} cannot be null")

    def to_python(self, value: Any) -> Any:
        """Coerce a database or user value into the Python type for this field."""
        return value

    def to_db(self, value: Any, dialect: Dialect | None = None) -> Any:
        """Coerce a Python value into a bind parameter."""
        if value is None:
            return None
        return value

    def db_type(self, dialect: Dialect) -> str:
        raise NotImplementedError

    def ddl(self, dialect: Dialect) -> str:
        """Column fragment used inside CREATE TABLE / ADD COLUMN."""
        if self.primary_key and isinstance(self, PrimaryKeyField):
            return dialect.pk_column_ddl(self.column)
        parts = [dialect.quote(self.column), self.db_type(dialect)]
        if self.primary_key:
            parts.append("PRIMARY KEY")
        if not self.null and not self.primary_key:
            parts.append("NOT NULL")
        if self.unique and not self.primary_key:
            parts.append("UNIQUE")
        return " ".join(parts)

    def schema_state(self) -> dict[str, Any]:
        """JSON-serializable snapshot used by the migration differ."""
        default: Any
        if callable(self.default):
            default = "<callable>"
        elif self.default is NOT_PROVIDED:
            default = None
        else:
            default = self.default
            if isinstance(default, datetime):
                default = default.isoformat()
        return {
            "class": type(self).__name__,
            "column": self.column,
            "null": self.null,
            "unique": self.unique,
            "index": self.index,
            "primary_key": self.primary_key,
            "default": default,
        }


class FieldDescriptor:
    """Stores the field value on the instance dict and records dirty fields.

    Why a descriptor instead of a plain attribute? The Unit of Work needs to
    know *which* columns changed. ``__set__`` is the single choke point where
    we can record that without requiring ``obj.mark_dirty('name')`` at every
    call site. Hydration sets ``_state.initializing`` so loading a row does
    not mark every column dirty.
    """

    def __init__(self, field: Field) -> None:
        self.field = field

    def __get__(self, instance: Model | None, owner: type[Model] | None = None) -> Any:
        if instance is None:
            return self.field
        return instance.__dict__.get(self.field.attname)

    def __set__(self, instance: Model, value: Any) -> None:
        self.field.validate(value)
        coerced = None if value is None else self.field.to_python(value)
        name = self.field.attname
        instance.__dict__[name] = coerced
        state = getattr(instance, "_state", None)
        if state is not None and not state.initializing:
            state.modified.add(name)


_creation_counter = 0


def _next_creation_counter() -> int:
    global _creation_counter
    _creation_counter += 1
    return _creation_counter


class IntegerField(Field):
    """32/64-bit integer column."""

    def to_python(self, value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{self.name} must be an integer, got {value!r}") from exc

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None:
            self.to_python(value)

    def db_type(self, dialect: Dialect) -> str:
        return dialect.integer_type()


class PrimaryKeyField(IntegerField):
    """Auto-incrementing integer primary key. Every concrete model gets one
    named ``id`` if the user does not declare a primary key.
    """

    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("primary_key", True)
        kwargs.setdefault("null", True)  # None until INSERT assigns it
        super().__init__(**kwargs)

    def db_type(self, dialect: Dialect) -> str:
        return dialect.integer_type()


class CharField(Field):
    """Short string with a hard ``max_length`` (enforced in Python and DDL)."""

    def __init__(self, max_length: int = 255, **kwargs: Any) -> None:
        if max_length <= 0:
            raise ValidationError("CharField max_length must be positive")
        self.max_length = max_length
        super().__init__(**kwargs)

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is None:
            return
        text = str(value)
        if len(text) > self.max_length:
            raise ValidationError(
                f"{self.name} exceeds max_length={self.max_length} ({len(text)} chars)"
            )

    def db_type(self, dialect: Dialect) -> str:
        return dialect.varchar_type(self.max_length)

    def schema_state(self) -> dict[str, Any]:
        state = super().schema_state()
        state["max_length"] = self.max_length
        return state


class TextField(Field):
    """Unbounded string stored as TEXT."""

    def to_python(self, value: Any) -> str | None:
        if value is None:
            return None
        return str(value)

    def db_type(self, dialect: Dialect) -> str:
        return dialect.text_type()


class BooleanField(Field):
    """Boolean column. SQLite stores 0/1; PostgreSQL stores BOOLEAN."""

    def to_python(self, value: Any) -> bool | None:
        if value is None:
            return None
        if isinstance(value, bool):
            return value
        if value in (0, 1, "0", "1"):
            return bool(int(value))
        if isinstance(value, str) and value.lower() in ("true", "false"):
            return value.lower() == "true"
        raise ValidationError(f"{self.name} must be a boolean, got {value!r}")

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None:
            self.to_python(value)

    def to_db(self, value: Any, dialect: Dialect | None = None) -> Any:
        if value is None:
            return None
        python = self.to_python(value)
        if dialect is not None and dialect.boolean_as_integer:
            return int(python) if python is not None else None
        return python

    def db_type(self, dialect: Dialect) -> str:
        return dialect.boolean_type()


class DateTimeField(Field):
    """Naive datetime stored as ISO-8601 text (SQLite) or TIMESTAMP (Postgres)."""

    def to_python(self, value: Any) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        if isinstance(value, str):
            text = value.replace("Z", "+00:00")
            try:
                return datetime.fromisoformat(text)
            except ValueError as exc:
                raise ValidationError(f"{self.name} is not a valid datetime: {value!r}") from exc
        raise ValidationError(f"{self.name} must be datetime, got {value!r}")

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None:
            self.to_python(value)

    def to_db(self, value: Any, dialect: Dialect | None = None) -> Any:
        if value is None:
            return None
        python = self.to_python(value)
        if python is None:
            return None
        if dialect is not None and dialect.name == "postgres":
            return python
        return python.isoformat(sep=" ")

    def db_type(self, dialect: Dialect) -> str:
        return dialect.datetime_type()


class FloatField(Field):
    """Floating-point column."""

    def to_python(self, value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise ValidationError(f"{self.name} must be a float, got {value!r}") from exc

    def validate(self, value: Any) -> None:
        super().validate(value)
        if value is not None:
            self.to_python(value)

    def db_type(self, dialect: Dialect) -> str:
        return dialect.float_type()


class ForeignKey(IntegerField):
    """Many-to-one relation stored as an integer column ``{name}_id``.

    Accessing ``instance.author`` (the field name) is lazy: the descriptor
    issues a SELECT the first time and caches the related instance. The raw
    key is always available as ``instance.author_id``.
    """

    def __init__(
        self,
        to: type[Model] | str,
        *,
        related_name: str | None = None,
        on_delete: str = "CASCADE",
        **kwargs: Any,
    ) -> None:
        self.to = to
        self.related_name = related_name
        self.on_delete = on_delete
        super().__init__(**kwargs)

    @property
    def attname(self) -> str:
        return f"{self.name}_id"

    @property
    def column(self) -> str:
        return self.db_column or self.attname

    def contribute_to_class(self, cls: type[Model], name: str) -> None:
        from pyorm.descriptors import ForwardFKDescriptor

        self.name = name
        self.model = cls
        if self.db_column is None:
            self.db_column = self.attname
        setattr(cls, name, ForwardFKDescriptor(self))
        # Raw id remains assignable (``post.author_id = 3``).
        setattr(cls, self.attname, FieldDescriptor(self))

    def related_model(self) -> type[Model]:
        from pyorm.models import registry

        if isinstance(self.to, str):
            resolved = registry.get(self.to)
            self.to = resolved
        return self.to  # type: ignore[return-value]

    def schema_state(self) -> dict[str, Any]:
        state = super().schema_state()
        target = self.to.__name__ if not isinstance(self.to, str) else self.to
        state["to"] = target
        state["related_name"] = self.related_name
        state["on_delete"] = self.on_delete
        return state

    def ddl(self, dialect: Dialect) -> str:
        related = self.related_model()
        related_table = related._meta.table_name
        related_pk = related._meta.pk.column
        null_sql = "" if self.null else " NOT NULL"
        unique_sql = " UNIQUE" if self.unique else ""
        return (
            f"{dialect.quote(self.column)} {self.db_type(dialect)}{null_sql}{unique_sql} "
            f"REFERENCES {dialect.quote(related_table)}({dialect.quote(related_pk)})"
        )


class ManyToManyField:
    """Declarative many-to-many. No column is added to either table.

    A hidden through model (join table) is created when the registry is
    finalized. The Python attribute becomes a ``ManyToManyManager``.
    """

    def __init__(
        self,
        to: type[Model] | str,
        *,
        related_name: str | None = None,
        through: type[Model] | str | None = None,
    ) -> None:
        self.to = to
        self.related_name = related_name
        self.through = through
        self.name: str = ""
        self.model: type[Model] | None = None
        self._creation_counter = _next_creation_counter()

    def contribute_to_class(self, cls: type[Model], name: str) -> None:
        from pyorm.descriptors import ManyToManyDescriptor

        self.name = name
        self.model = cls
        setattr(cls, name, ManyToManyDescriptor(self))

    def related_model(self) -> type[Model]:
        from pyorm.models import registry

        if isinstance(self.to, str):
            self.to = registry.get(self.to)
        return self.to  # type: ignore[return-value]

    def schema_state(self) -> dict[str, Any]:
        target = self.to.__name__ if not isinstance(self.to, str) else self.to
        return {
            "class": "ManyToManyField",
            "to": target,
            "related_name": self.related_name,
            "through": None
            if self.through is None
            else (self.through if isinstance(self.through, str) else self.through.__name__),
        }
