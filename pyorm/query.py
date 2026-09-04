"""Lazy QuerySet and parameterized SQL compiler.

A QuerySet is a *description* of a SELECT, not a result. Cloning on every
chainable call (``filter``, ``order_by``, …) is copied from Django: each
method returns a new object so ``qs.filter(a=1)`` does not mutate ``qs``.
SQL runs only from ``_fetch_all``, ``count``, ``exists``, ``first``, or
slicing — the terminal operations.

Values never enter the SQL string. Lookups become placeholders plus a
params list (``?`` or ``%s`` depending on dialect).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterator, Sequence

from pyorm.connection import Engine, get_engine
from pyorm.dialect import Dialect
from pyorm.exceptions import DoesNotExist, FieldError, MultipleObjectsReturned, QueryError
from pyorm.fields import Field, ForeignKey
from pyorm.models import Model, registry

LOOKUP_OPERATORS: dict[str, str] = {
    "exact": "=",
    "gt": ">",
    "gte": ">=",
    "lt": "<",
    "lte": "<=",
    "contains": "LIKE",
    "in": "IN",
    "isnull": "IS",
}


@dataclass
class Join:
    from_alias: str
    from_column: str
    table: str
    alias: str
    to_column: str
    field_name: str
    model: type[Model]


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]


@dataclass
class WhereNode:
    lookups: dict[str, Any]
    negated: bool = False


class QuerySet:
    """Chainable, lazy SELECT builder.

    Typical path for ``User.objects.filter(age__gt=18).first()``::

        QuerySet.filter  → clone with a WhereNode
        QuerySet.first   → _compile SELECT … LIMIT 1
        Engine.execute   → sqlite3/psycopg2 with bound params
        hydrate          → User instance, registered in the Session identity map
    """

    def __init__(
        self,
        model: type[Model],
        *,
        session: Any | None = None,
        engine: Engine | None = None,
    ) -> None:
        self.model = model
        self.session = session
        self._engine = engine
        self._where: list[WhereNode] = []
        self._order: list[tuple[str, bool]] = []  # (column, descending)
        self._limit: int | None = None
        self._offset: int | None = None
        self._select_related: list[str] = []
        self._values: list[str] | None = None
        self._result_cache: list[Any] | None = None
        self._empty: bool = False

    # ------------------------------------------------------------------ clone
    def _clone(self) -> QuerySet:
        qs = QuerySet(self.model, session=self.session, engine=self._engine)
        qs._where = list(self._where)
        qs._order = list(self._order)
        qs._limit = self._limit
        qs._offset = self._offset
        qs._select_related = list(self._select_related)
        qs._values = None if self._values is None else list(self._values)
        qs._empty = self._empty
        return qs

    @property
    def engine(self) -> Engine:
        if self._engine is not None:
            return self._engine
        if self.session is not None:
            return self.session.engine  # type: ignore[no-any-return]
        return get_engine()

    def none(self) -> QuerySet:
        qs = self._clone()
        qs._empty = True
        qs._result_cache = []
        return qs

    # ---------------------------------------------------------- chain methods
    def filter(self, **kwargs: Any) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        if kwargs:
            qs._where.append(WhereNode(dict(kwargs), negated=False))
        return qs

    def exclude(self, **kwargs: Any) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        if kwargs:
            qs._where.append(WhereNode(dict(kwargs), negated=True))
        return qs

    def order_by(self, *fields: str) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        for spec in fields:
            descending = spec.startswith("-")
            name = spec[1:] if descending else spec
            qs._order.append((name, descending))
        return qs

    def limit(self, n: int) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        qs._limit = n
        return qs

    def offset(self, n: int) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        qs._offset = n
        return qs

    def values(self, *fields: str) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        qs._values = list(fields) if fields else [
            f.attname for f in self.model._meta.concrete_fields()
        ]
        return qs

    def select_related(self, *fields: str) -> QuerySet:
        qs = self._clone()
        qs._result_cache = None
        qs._select_related.extend(fields)
        return qs

    # -------------------------------------------------------- terminal methods
    def compile(self) -> CompiledQuery:
        """Return SQL + params without executing. Used by tests and the demo."""
        registry.finalize()
        return QueryCompiler(self.engine.dialect).compile_select(self)

    def all(self) -> list[Any]:
        self._fetch_all()
        assert self._result_cache is not None
        return list(self._result_cache)

    def first(self) -> Any | None:
        if self._result_cache is not None:
            return self._result_cache[0] if self._result_cache else None
        qs = self.limit(1)
        rows = qs.all()
        return rows[0] if rows else None

    def get(self, **kwargs: Any) -> Any:
        qs = self.filter(**kwargs) if kwargs else self
        rows = qs.limit(2).all()
        if not rows:
            raise DoesNotExist(f"{self.model.__name__} matching query does not exist")
        if len(rows) > 1:
            raise MultipleObjectsReturned(f"{self.model.__name__} returned more than one row")
        return rows[0]

    def count(self) -> int:
        if self._empty:
            return 0
        if self._result_cache is not None:
            return len(self._result_cache)
        compiled = QueryCompiler(self.engine.dialect).compile_count(self)
        conn = self.session.connection if self.session is not None else None
        row = self.engine.fetchone(compiled.sql, compiled.params, conn=conn)
        return int(row[0]) if row else 0

    def exists(self) -> bool:
        if self._empty:
            return False
        if self._result_cache is not None:
            return bool(self._result_cache)
        qs = self.limit(1)
        compiled = qs.compile()
        conn = self.session.connection if self.session is not None else None
        row = self.engine.fetchone(compiled.sql, compiled.params, conn=conn)
        return row is not None

    def _fetch_all(self) -> None:
        if self._result_cache is not None:
            return
        if self._empty:
            self._result_cache = []
            return
        compiled = self.compile()
        conn = self.session.connection if self.session is not None else None
        rows = self.engine.fetchall(compiled.sql, compiled.params, conn=conn)
        compiler = QueryCompiler(self.engine.dialect)
        columns = compiler.select_columns(self)
        self._result_cache = [self._hydrate(row, columns) for row in rows]

    def _hydrate(self, row: Sequence[Any], columns: list[tuple[type[Model], Field, str]]) -> Any:
        if self._values is not None:
            return {columns[i][1].name: row[i] for i in range(len(self._values))}

        by_model: dict[type[Model], dict[str, Any]] = {}
        aliases: dict[type[Model], str] = {}
        for idx, (model, field, alias) in enumerate(columns):
            by_model.setdefault(model, {})
            aliases[model] = alias
            raw = row[idx] if idx < len(row) else None
            by_model[model][field.attname] = None if raw is None else field.to_python(raw)

        instance = self._instance_from_values(self.model, by_model.get(self.model, {}))
        for field_name in self._select_related:
            fk = self.model._meta.get_field(field_name)
            if not isinstance(fk, ForeignKey):
                continue
            related = fk.related_model()
            related_vals = by_model.get(related)
            cache_name = f"_{fk.name}_cache"
            if not related_vals or related_vals.get(related._meta.pk.attname) is None:
                instance.__dict__[cache_name] = None
                continue
            related_obj = self._instance_from_values(related, related_vals)
            instance.__dict__[cache_name] = related_obj
        return instance

    def _instance_from_values(self, model: type[Model], values: dict[str, Any]) -> Model:
        pk_name = model._meta.pk.attname
        pk = values.get(pk_name)
        if self.session is not None and pk is not None:
            existing = self.session.identity_map.get(model, pk)
            if existing is not None:
                return existing
        from pyorm.models import InstanceState

        obj = model.__new__(model)
        obj._state = InstanceState()
        obj._state.initializing = True
        for field in model._meta.concrete_fields():
            if isinstance(field, ForeignKey):
                obj.__dict__[field.attname] = values.get(field.attname)
            else:
                obj.__dict__[field.attname] = values.get(field.attname)
        obj._state.initializing = False
        obj._state.snapshot_clean()
        if self.session is not None:
            obj._state.session = self.session
            if pk is not None:
                self.session.identity_map.add(obj)
        return obj

    def __iter__(self) -> Iterator[Any]:
        self._fetch_all()
        assert self._result_cache is not None
        return iter(self._result_cache)

    def __len__(self) -> int:
        self._fetch_all()
        assert self._result_cache is not None
        return len(self._result_cache)

    def __bool__(self) -> bool:
        return self.exists()

    def __getitem__(self, key: int | slice) -> Any:
        if isinstance(key, slice):
            qs = self._clone()
            qs._result_cache = None
            start = key.start or 0
            if start < 0:
                self._fetch_all()
                assert self._result_cache is not None
                return self._result_cache[key]
            qs._offset = (self._offset or 0) + start
            if key.stop is not None:
                qs._limit = max(0, key.stop - start)
            return qs.all() if key.step not in (None, 1) else qs.all()
        if key < 0:
            self._fetch_all()
            assert self._result_cache is not None
            return self._result_cache[key]
        qs = self.offset((self._offset or 0) + key).limit(1)
        rows = qs.all()
        if not rows:
            raise IndexError("QuerySet index out of range")
        return rows[0]


class QueryCompiler:
    """Turn a QuerySet into parameterized SQL.

    The compiler walks lookups like ``author__name__contains`` and emits
    JOINs as needed. Injection safety: identifiers are quoted via the
    dialect; values are always bind parameters.
    """

    def __init__(self, dialect: Dialect) -> None:
        self.dialect = dialect

    def compile_select(self, qs: QuerySet) -> CompiledQuery:
        if qs._empty:
            return CompiledQuery(sql="SELECT 1 WHERE 1=0", params=[])
        d = self.dialect
        joins, alias_map = self._joins(qs)
        columns = self.select_columns(qs)
        col_sql = ", ".join(
            d.qualify(alias, field.column) for _, field, alias in columns
        )
        from_sql = f"{d.quote(qs.model._meta.table_name)} AS {d.quote(alias_map[qs.model])}"
        join_sql = "".join(self._join_clause(j) for j in joins)
        where_sql, params = self._where_sql(qs, alias_map)
        order_sql = self._order_sql(qs, alias_map)
        limit_sql = self._limit_sql(qs)
        sql = f"SELECT {col_sql} FROM {from_sql}{join_sql}{where_sql}{order_sql}{limit_sql}"
        return CompiledQuery(sql=sql, params=params)

    def compile_count(self, qs: QuerySet) -> CompiledQuery:
        d = self.dialect
        joins, alias_map = self._joins(qs)
        from_sql = f"{d.quote(qs.model._meta.table_name)} AS {d.quote(alias_map[qs.model])}"
        join_sql = "".join(self._join_clause(j) for j in joins)
        where_sql, params = self._where_sql(qs, alias_map)
        sql = f"SELECT COUNT(*) FROM {from_sql}{join_sql}{where_sql}"
        return CompiledQuery(sql=sql, params=params)

    def select_columns(self, qs: QuerySet) -> list[tuple[type[Model], Field, str]]:
        alias_map = self._alias_map(qs)
        if qs._values is not None:
            cols: list[tuple[type[Model], Field, str]] = []
            for name in qs._values:
                field = qs.model._meta.get_field(name)
                cols.append((qs.model, field, alias_map[qs.model]))
            return cols
        cols = [
            (qs.model, f, alias_map[qs.model])
            for f in qs.model._meta.concrete_fields()
        ]
        for rel_name in qs._select_related:
            fk = qs.model._meta.get_field(rel_name)
            if not isinstance(fk, ForeignKey):
                raise FieldError(f"{rel_name} is not a ForeignKey")
            related = fk.related_model()
            alias = alias_map[related]
            for f in related._meta.concrete_fields():
                cols.append((related, f, alias))
        return cols

    def _alias_map(self, qs: QuerySet) -> dict[type[Model], str]:
        _, alias_map = self._joins(qs)
        return alias_map

    def _joins(self, qs: QuerySet) -> tuple[list[Join], dict[type[Model], str]]:
        alias_map: dict[type[Model], str] = {qs.model: "t0"}
        joins: list[Join] = []
        next_alias = 1

        def ensure_join(model: type[Model], field_name: str) -> type[Model]:
            nonlocal next_alias
            field = model._meta.get_field(field_name)
            if not isinstance(field, ForeignKey):
                raise FieldError(f"{model.__name__}.{field_name} is not a relation")
            related = field.related_model()
            if related in alias_map:
                return related
            alias = f"t{next_alias}"
            next_alias += 1
            alias_map[related] = alias
            joins.append(
                Join(
                    from_alias=alias_map[model],
                    from_column=field.column,
                    table=related._meta.table_name,
                    alias=alias,
                    to_column=related._meta.pk.column,
                    field_name=field.name,
                    model=related,
                )
            )
            return related

        needed = set(qs._select_related)
        for node in qs._where:
            for key in node.lookups:
                path, _ = _split_lookup(key)
                if len(path) > 1:
                    needed.add(path[0])
        for name in needed:
            ensure_join(qs.model, name)
        for field_name, _ in qs._order:
            path, _ = _split_lookup(field_name)
            if len(path) > 1:
                ensure_join(qs.model, path[0])
        return joins, alias_map

    def _join_clause(self, join: Join) -> str:
        d = self.dialect
        return (
            f" LEFT OUTER JOIN {d.quote(join.table)} AS {d.quote(join.alias)}"
            f" ON {d.qualify(join.from_alias, join.from_column)}"
            f" = {d.qualify(join.alias, join.to_column)}"
        )

    def _where_sql(self, qs: QuerySet, alias_map: dict[type[Model], str]) -> tuple[str, list[Any]]:
        parts: list[str] = []
        params: list[Any] = []
        for node in qs._where:
            sql, node_params = self._lookups_sql(qs.model, node.lookups, alias_map)
            if not sql:
                continue
            if node.negated:
                parts.append(f"NOT ({sql})")
            else:
                parts.append(f"({sql})")
            params.extend(node_params)
        if not parts:
            return "", params
        return " WHERE " + " AND ".join(parts), params

    def _lookups_sql(
        self,
        model: type[Model],
        lookups: dict[str, Any],
        alias_map: dict[type[Model], str],
    ) -> tuple[str, list[Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        ph = self.dialect.placeholder
        for key, value in lookups.items():
            path, lookup = _split_lookup(key)
            target_model = model
            for rel in path[:-1]:
                field = target_model._meta.get_field(rel)
                if not isinstance(field, ForeignKey):
                    raise FieldError(f"{rel} is not a ForeignKey")
                target_model = field.related_model()
            field_name = path[-1]
            field = target_model._meta.get_field(field_name)
            alias = alias_map[target_model]
            column = self.dialect.qualify(alias, field.column)
            sql, extra = self._lookup_clause(column, field, lookup, value, ph)
            clauses.append(sql)
            params.extend(extra)
        return " AND ".join(clauses), params

    def _lookup_clause(
        self,
        column: str,
        field: Field,
        lookup: str,
        value: Any,
        ph: str,
    ) -> tuple[str, list[Any]]:
        if lookup not in LOOKUP_OPERATORS:
            raise FieldError(f"Unsupported lookup {lookup!r}")
        if lookup == "isnull":
            return (f"{column} IS NULL" if value else f"{column} IS NOT NULL"), []
        if lookup == "in":
            seq = list(value)
            if not seq:
                return "1=0", []
            placeholders = ", ".join(ph for _ in seq)
            params = [_bind(field, v, self.dialect) for v in seq]
            return f"{column} IN ({placeholders})", params
        if lookup == "contains":
            text = "" if value is None else str(value)
            return f"{column} LIKE {ph}", [f"%{text}%"]
        if hasattr(value, "_meta") and hasattr(value, "pk"):
            value = value.pk
        return f"{column} {LOOKUP_OPERATORS[lookup]} {ph}", [_bind(field, value, self.dialect)]

    def _order_sql(self, qs: QuerySet, alias_map: dict[type[Model], str]) -> str:
        if not qs._order:
            return ""
        bits: list[str] = []
        for name, descending in qs._order:
            path, _ = _split_lookup(name)
            target = qs.model
            for rel in path[:-1]:
                field = target._meta.get_field(rel)
                if not isinstance(field, ForeignKey):
                    raise FieldError(f"{rel} is not a ForeignKey")
                target = field.related_model()
            field = target._meta.get_field(path[-1])
            sql = self.dialect.qualify(alias_map[target], field.column)
            bits.append(f"{sql} {'DESC' if descending else 'ASC'}")
        return " ORDER BY " + ", ".join(bits)

    def _limit_sql(self, qs: QuerySet) -> str:
        bits = ""
        if qs._limit is not None:
            bits += f" LIMIT {int(qs._limit)}"
        if qs._offset:
            if qs._limit is None:
                bits += " LIMIT -1" if self.dialect.name == "sqlite" else ""
                if self.dialect.name != "sqlite":
                    # PostgreSQL requires LIMIT before OFFSET; use ALL.
                    bits += " LIMIT ALL"
            bits += f" OFFSET {int(qs._offset)}"
        return bits


def _split_lookup(key: str) -> tuple[list[str], str]:
    parts = key.split("__")
    if len(parts) >= 2 and parts[-1] in LOOKUP_OPERATORS:
        return parts[:-1], parts[-1]
    return parts, "exact"


def _bind(field: Field, value: Any, dialect: Dialect) -> Any:
    if value is None:
        return None
    if hasattr(value, "_meta") and hasattr(value, "pk"):
        value = value.pk
    try:
        python = field.to_python(value)
    except Exception:
        python = value
    return field.to_db(python, dialect)
