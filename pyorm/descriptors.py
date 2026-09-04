"""Descriptors that implement lazy ForeignKey loading and relation managers.

Why descriptors
---------------
``some_order.customer`` looks like an attribute but must sometimes run SQL.
Python's descriptor protocol (``__get__`` / ``__set__``) is the language
feature designed for that: the ORM can intercept access, issue one SELECT,
and cache the result on the instance dict so the second access is free.

This is also how the N+1 problem is born. Looping ``for order in orders:
print(order.customer)`` fires 1 query for the list plus N queries for
customers. ``select_related('customer')`` replaces that with a JOIN.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Iterable

from pyorm.exceptions import QueryError, SessionError

if TYPE_CHECKING:
    from pyorm.fields import ForeignKey, ManyToManyField
    from pyorm.models import Model
    from pyorm.query import QuerySet
    from pyorm.session import Session


def _session_for(instance: Model) -> Session | None:
    return instance._state.session


class ForwardFKDescriptor:
    """Lazy many-to-one: ``order.customer`` → Customer instance or None."""

    def __init__(self, field: ForeignKey) -> None:
        self.field = field
        self.cache_name = f"_{field.name}_cache"

    def __get__(self, instance: Model | None, owner: type[Model] | None = None) -> Any:
        if instance is None:
            return self.field
        if self.cache_name in instance.__dict__:
            return instance.__dict__[self.cache_name]
        fk_id = instance.__dict__.get(self.field.attname)
        if fk_id is None:
            instance.__dict__[self.cache_name] = None
            return None
        related_model = self.field.related_model()
        session = _session_for(instance)
        obj: Model | None
        if session is not None:
            cached = session.identity_map.get(related_model, fk_id)
            if cached is not None:
                instance.__dict__[self.cache_name] = cached
                return cached
            obj = session.query(related_model).filter(**{related_model._meta.pk.attname: fk_id}).first()
        else:
            obj = related_model.query().filter(**{related_model._meta.pk.attname: fk_id}).first()
        instance.__dict__[self.cache_name] = obj
        return obj

    def __set__(self, instance: Model, value: Any) -> None:
        related_model = self.field.related_model()
        if value is None:
            instance.__dict__[self.field.attname] = None
            instance.__dict__[self.cache_name] = None
        elif isinstance(value, related_model):
            instance.__dict__[self.field.attname] = value.pk
            instance.__dict__[self.cache_name] = value
        elif isinstance(value, int):
            instance.__dict__[self.field.attname] = value
            instance.__dict__.pop(self.cache_name, None)
        elif isinstance(value, str) and value.isdigit():
            instance.__dict__[self.field.attname] = int(value)
            instance.__dict__.pop(self.cache_name, None)
        else:
            raise TypeError(
                f"{self.field.name} must be {related_model.__name__}, int, or None; "
                f"got {type(value).__name__}"
            )
        state = getattr(instance, "_state", None)
        if state is not None and not state.initializing:
            state.modified.add(self.field.attname)


class ReverseManyDescriptor:
    """One-to-many reverse accessor: ``user.orders`` → QuerySet[Order]."""

    def __init__(self, field: ForeignKey) -> None:
        self.field = field

    def __get__(self, instance: Model | None, owner: type[Model] | None = None) -> Any:
        if instance is None:
            return self
        related_model = self.field.model
        assert related_model is not None
        session = _session_for(instance)
        from pyorm.query import QuerySet

        qs = QuerySet(related_model, session=session)
        if instance.pk is None:
            return qs.none()
        return qs.filter(**{self.field.attname: instance.pk})


class ManyToManyDescriptor:
    """Installs a ``ManyToManyManager`` on each instance."""

    def __init__(
        self,
        field: ManyToManyField,
        *,
        reverse: bool = False,
        source: ManyToManyField | None = None,
    ) -> None:
        self.field = field
        self.reverse = reverse
        self.source = source

    def __get__(self, instance: Model | None, owner: type[Model] | None = None) -> Any:
        if instance is None:
            return self.field
        return ManyToManyManager(instance, self.field, reverse=self.reverse)


class ManyToManyManager:
    """``post.tags.add()`` / ``.remove()`` / ``.set()`` / ``.all()``.

    Mutations execute immediately (Django's choice, not Unit-of-Work). The
    join rows are not first-class entities the user asked to track; folding
    them into Session.commit() would hide a second write protocol. Immediate
    SQL keeps the manager obvious in a demo: you call ``.add()`` and a
    parameterized INSERT hits the join table.
    """

    def __init__(self, instance: Model, field: ManyToManyField, *, reverse: bool = False) -> None:
        self.instance = instance
        self.field = field
        self.reverse = reverse

    def _through(self) -> type[Model]:
        through = self.field.through
        if through is None or isinstance(through, str):
            from pyorm.models import registry

            registry.finalize()
            through = self.field.through
        assert through is not None and not isinstance(through, str)
        return through

    def _sides(self) -> tuple[str, str, type[Model]]:
        """Return (this_fk_attname, other_fk_attname, other_model)."""
        through = self._through()
        fks = through._meta.fk_fields()
        this_model = type(self.instance)
        other_model = self.field.related_model()
        this_fk = next(f for f in fks if f.related_model() is this_model)
        other_fk = next(f for f in fks if f.related_model() is other_model)
        return this_fk.attname, other_fk.attname, other_model

    def all(self) -> QuerySet:
        from pyorm.query import QuerySet

        if self.instance.pk is None:
            return QuerySet(self.field.related_model(), session=_session_for(self.instance)).none()
        through = self._through()
        this_attr, other_attr, other_model = self._sides()
        session = _session_for(self.instance)
        links = QuerySet(through, session=session).filter(**{this_attr: self.instance.pk}).all()
        ids = [getattr(link, other_attr) for link in links]
        qs = QuerySet(other_model, session=session)
        if not ids:
            return qs.none()
        pk_name = other_model._meta.pk.attname
        return qs.filter(**{f"{pk_name}__in": ids})

    def add(self, *objects: Model) -> None:
        self._require_pk()
        through = self._through()
        this_attr, other_attr, _ = self._sides()
        session = _session_for(self.instance)
        engine = session.engine if session is not None else None
        from pyorm.connection import get_engine
        from pyorm.session import Session

        if engine is None:
            engine = get_engine()
        for obj in objects:
            if obj.pk is None:
                raise SessionError("Cannot add an unsaved object to a many-to-many relation")
            existing = (
                through.query(session)
                .filter(**{this_attr: self.instance.pk, other_attr: obj.pk})
                .first()
            )
            if existing is not None:
                continue
            link = through(**{this_attr: self.instance.pk, other_attr: obj.pk})
            if session is not None:
                session.add(link)
                session.flush_object(link)
            else:
                with Session(engine) as tmp:
                    tmp.add(link)
                    tmp.commit()

    def remove(self, *objects: Model) -> None:
        self._require_pk()
        through = self._through()
        this_attr, other_attr, _ = self._sides()
        session = _session_for(self.instance)
        from pyorm.connection import get_engine
        from pyorm.query import QuerySet
        from pyorm.session import Session

        engine = session.engine if session is not None else get_engine()
        for obj in objects:
            qs = QuerySet(through, session=session).filter(
                **{this_attr: self.instance.pk, other_attr: obj.pk}
            )
            rows = qs.all()
            if session is not None:
                for row in rows:
                    session.delete(row)
                session.flush()
            else:
                with Session(engine) as tmp:
                    for row in rows:
                        tmp.delete(row)
                    tmp.commit()

    def set(self, objects: Iterable[Model]) -> None:
        self._require_pk()
        objects = list(objects)
        through = self._through()
        this_attr, _, _ = self._sides()
        session = _session_for(self.instance)
        from pyorm.connection import get_engine
        from pyorm.query import QuerySet
        from pyorm.session import Session

        engine = session.engine if session is not None else get_engine()
        existing = QuerySet(through, session=session).filter(**{this_attr: self.instance.pk}).all()
        if session is not None:
            for row in existing:
                session.delete(row)
            session.flush()
            self.add(*objects)
        else:
            with Session(engine) as tmp:
                for row in existing:
                    tmp.delete(row)
                tmp.commit()
            self.add(*objects)

    def _require_pk(self) -> None:
        if self.instance.pk is None:
            raise QueryError("Save the instance before mutating many-to-many relations")
