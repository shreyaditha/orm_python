"""Unit of Work, identity map, and dirty-field tracking.

A Session is the boundary that makes ``commit()`` mean something:

* **New** objects (``add``) become INSERTs.
* **Dirty** objects (field descriptors recorded a change) become UPDATEs
  that list *only* the modified columns.
* **Deleted** objects become DELETEs.
* The three lists flush inside one real database transaction. Any exception
  rolls the whole unit back.

The identity map guarantees that ``session.query(User).get(id=1)`` twice
returns the *same Python object*. Without it, ``user_a.name = 'x';
user_b = session.query(User).get(id=1)`` would silently fork two copies of
one row — the classic ORM coherence bug SQLAlchemy's Session exists to
prevent.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Iterator, TypeVar

from pyorm.connection import DBAPIConnection, Engine, get_engine
from pyorm.exceptions import SessionError
from pyorm.fields import ForeignKey
from pyorm.models import Model
from pyorm.query import QuerySet

T = TypeVar("T", bound=Model)

_current_session: ContextVar[Session | None] = ContextVar("pyorm_session", default=None)


def current_session() -> Session | None:
    """Session bound to this context (set by ``Session`` as a context manager)."""
    return _current_session.get()


class IdentityMap:
    """``(model_class, pk) → instance`` for the lifetime of one Session."""

    def __init__(self) -> None:
        self._data: dict[tuple[type[Model], Any], Model] = {}

    def key(self, model: type[Model], pk: Any) -> tuple[type[Model], Any]:
        return (model, pk)

    def get(self, model: type[Model], pk: Any) -> Model | None:
        if pk is None:
            return None
        return self._data.get((model, pk))

    def add(self, obj: Model) -> None:
        if obj.pk is None:
            return
        self._data[(type(obj), obj.pk)] = obj

    def remove(self, obj: Model) -> None:
        if obj.pk is None:
            return
        self._data.pop((type(obj), obj.pk), None)

    def __contains__(self, obj: Model) -> bool:
        return (type(obj), obj.pk) in self._data


class Session:
    """Unit of Work. Prefer using it as a context manager so rollback is automatic."""

    def __init__(self, engine: Engine | None = None, *, echo: bool = False) -> None:
        self.engine = engine or get_engine()
        if echo:
            self.engine.echo = True
        self.identity_map = IdentityMap()
        self._new: list[Model] = []
        self._deleted: list[Model] = []
        self._connection: DBAPIConnection | None = None
        self._token: Any = None
        self.statements: list[tuple[str, tuple[Any, ...]]] = []

    @property
    def connection(self) -> DBAPIConnection | None:
        """Transactional connection while a flush/commit is in progress."""
        return self._connection

    def query(self, model: type[T]) -> QuerySet:
        return QuerySet(model, session=self)

    def add(self, obj: Model) -> None:
        """Schedule ``obj`` for INSERT (or track it if it already has a PK)."""
        obj._state.session = self
        if obj._state.deleted:
            obj._state.deleted = False
            if obj in self._deleted:
                self._deleted.remove(obj)
        if obj.pk is not None and self.identity_map.get(type(obj), obj.pk) is obj:
            return
        if obj not in self._new:
            self._new.append(obj)

    def delete(self, obj: Model) -> None:
        """Schedule ``obj`` for DELETE on the next commit/flush."""
        obj._state.session = self
        obj._state.deleted = True
        if obj in self._new:
            self._new.remove(obj)
            return
        if obj not in self._deleted:
            self._deleted.append(obj)

    def dirty_objects(self) -> list[Model]:
        seen: list[Model] = []
        for obj in list(self.identity_map._data.values()):
            if obj._state.deleted:
                continue
            if obj._state.modified:
                seen.append(obj)
        for obj in self._new:
            if obj.pk is not None and obj._state.modified and obj not in seen:
                seen.append(obj)
        return seen

    def flush(self) -> None:
        """Write pending INSERT/UPDATE/DELETE without committing the transaction."""
        owns = self._connection is None
        if owns:
            self._connection = self.engine.acquire()
            self._execute(self.engine.dialect.begin())
        try:
            for obj in list(self._new):
                self._insert(obj)
            for obj in self.dirty_objects():
                self._update(obj)
            for obj in list(self._deleted):
                self._delete_row(obj)
            if owns:
                self._execute(self.engine.dialect.commit())
        except Exception:
            if owns:
                try:
                    self._execute(self.engine.dialect.rollback())
                except Exception:
                    pass
                if self._connection is not None:
                    self.engine.release(self._connection)
                    self._connection = None
            raise
        else:
            if owns and self._connection is not None:
                self.engine.release(self._connection)
                self._connection = None

    def flush_object(self, obj: Model) -> None:
        """INSERT a single pending object (used by M2M so PKs exist immediately)."""
        if obj in self._new:
            owns = self._connection is None
            if owns:
                self._connection = self.engine.acquire()
                self._execute(self.engine.dialect.begin())
            try:
                self._insert(obj)
                if owns:
                    self._execute(self.engine.dialect.commit())
            except Exception:
                if owns:
                    self._execute(self.engine.dialect.rollback())
                raise
            finally:
                if owns and self._connection is not None:
                    self.engine.release(self._connection)
                    self._connection = None

    def commit(self) -> None:
        """Flush then COMMIT. On any failure, ROLLBACK the whole unit."""
        self.statements.clear()
        owns = self._connection is None
        if owns:
            self._connection = self.engine.acquire()
            self._execute(self.engine.dialect.begin())
        try:
            for obj in list(self._new):
                self._insert(obj)
            for obj in self.dirty_objects():
                self._update(obj)
            for obj in list(self._deleted):
                self._delete_row(obj)
            self._execute(self.engine.dialect.commit())
        except Exception:
            try:
                self._execute(self.engine.dialect.rollback())
            except Exception:
                pass
            raise
        finally:
            if self._connection is not None:
                self.engine.release(self._connection)
                self._connection = None

    def rollback(self) -> None:
        """Drop pending in-memory changes and ROLLBACK the DB transaction if open."""
        if self._connection is not None:
            try:
                self._execute(self.engine.dialect.rollback())
            finally:
                self.engine.release(self._connection)
                self._connection = None
        self._new.clear()
        self._deleted.clear()
        for obj in list(self.identity_map._data.values()):
            obj._state.modified.clear()

    def close(self) -> None:
        if self._connection is not None:
            try:
                self._execute(self.engine.dialect.rollback())
            except Exception:
                pass
            self.engine.release(self._connection)
            self._connection = None

    def __enter__(self) -> Session:
        self._token = _current_session.set(self)
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        try:
            if exc_type is None:
                self.commit()
            else:
                self.rollback()
        finally:
            self.close()
            if self._token is not None:
                _current_session.reset(self._token)
                self._token = None

    # ----------------------------------------------------------------- SQL
    def _execute(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]] | None:
        assert self._connection is not None
        bind = tuple(params or ())
        self.statements.append((sql, bind))
        return self.engine.execute(sql, params or [], conn=self._connection, fetch=False)

    def _fetch(self, sql: str, params: list[Any] | None = None) -> list[tuple[Any, ...]]:
        assert self._connection is not None
        bind = tuple(params or ())
        self.statements.append((sql, bind))
        return self.engine.fetchall(sql, params or [], conn=self._connection)

    def _insert(self, obj: Model) -> None:
        dialect = self.engine.dialect
        table = dialect.quote(obj._meta.table_name)
        cols: list[str] = []
        params: list[Any] = []
        pk = obj._meta.pk
        for field in obj._meta.concrete_fields():
            value = obj.__dict__.get(field.attname)
            if field.primary_key and value is None:
                continue
            cols.append(dialect.quote(field.column))
            params.append(field.to_db(value, dialect))
        col_sql = ", ".join(cols)
        ph = ", ".join(dialect.placeholder for _ in cols)
        sql = f"INSERT INTO {table} ({col_sql}) VALUES ({ph})"
        if dialect.returning_supported and pk is not None:
            sql += f" RETURNING {dialect.quote(pk.column)}"
            rows = self._fetch(sql, params)
            if rows:
                obj._state.initializing = True
                setattr(obj, pk.attname, pk.to_python(rows[0][0]))
                obj._state.initializing = False
        else:
            self._execute(sql, params)
            if pk is not None and obj.pk is None:
                follow = dialect.last_insert_id_sql(obj._meta.table_name, pk.column)
                if follow:
                    rows = self._fetch(follow, [])
                    if rows:
                        obj._state.initializing = True
                        setattr(obj, pk.attname, pk.to_python(rows[0][0]))
                        obj._state.initializing = False
        obj._state.snapshot_clean()
        obj._state.session = self
        if obj in self._new:
            self._new.remove(obj)
        self.identity_map.add(obj)

    def _update(self, obj: Model) -> None:
        if not obj._state.modified:
            return
        if obj.pk is None:
            raise SessionError(f"Cannot UPDATE {type(obj).__name__} without a primary key")
        dialect = self.engine.dialect
        assignments: list[str] = []
        params: list[Any] = []
        pk = obj._meta.pk
        for attname in sorted(obj._state.modified):
            if pk and attname == pk.attname:
                continue
            try:
                field = obj._meta.get_field(attname)
            except Exception:
                continue
            assignments.append(f"{dialect.quote(field.column)} = {dialect.placeholder}")
            params.append(field.to_db(obj.__dict__.get(attname), dialect))
        if not assignments:
            obj._state.modified.clear()
            return
        params.append(pk.to_db(obj.pk, dialect) if pk else obj.pk)
        sql = (
            f"UPDATE {dialect.quote(obj._meta.table_name)} SET {', '.join(assignments)} "
            f"WHERE {dialect.quote(pk.column)} = {dialect.placeholder}"
        )
        self._execute(sql, params)
        obj._state.modified.clear()

    def _delete_row(self, obj: Model) -> None:
        pk = obj._meta.pk
        if pk is None or obj.pk is None:
            if obj in self._deleted:
                self._deleted.remove(obj)
            return
        dialect = self.engine.dialect
        sql = (
            f"DELETE FROM {dialect.quote(obj._meta.table_name)} "
            f"WHERE {dialect.quote(pk.column)} = {dialect.placeholder}"
        )
        self._execute(sql, [pk.to_db(obj.pk, dialect)])
        self.identity_map.remove(obj)
        if obj in self._deleted:
            self._deleted.remove(obj)
        obj._state.deleted = True
        obj._state.persistent = False
