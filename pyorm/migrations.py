"""Schema differ, migration file generation, and apply/rollback.

State is stored as JSON (not pickle) so diffs are reviewable in git. Each
generated module exposes ``up(engine)`` and ``down(engine)`` that run
parameter-free DDL (DDL has no user values, so identifiers are quoted and
there is nothing to bind).

SQLite cannot drop a column on very old versions; we use ``ALTER TABLE …
DROP COLUMN`` which is supported since SQLite 3.35 (2021). Rename is
``ALTER TABLE … RENAME COLUMN``. If one field disappears and another of
the same type appears, we treat it as a rename (best-effort heuristic).
"""

from __future__ import annotations

import importlib.util
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pyorm.connection import Engine, get_engine
from pyorm.dialect import Dialect
from pyorm.exceptions import MigrationError
from pyorm.models import registry

MIGRATIONS_TABLE = "_migrations"


@dataclass
class Operation:
    kind: str
    payload: dict[str, Any]

    def up_sql(self, dialect: Dialect) -> list[str]:
        return _sql_for(self, dialect, down=False)

    def down_sql(self, dialect: Dialect) -> list[str]:
        return _sql_for(self, dialect, down=True)


def _q(dialect: Dialect, name: str) -> str:
    return dialect.quote(name)


def _sql_for(op: Operation, dialect: Dialect, *, down: bool) -> list[str]:
    k = op.kind
    p = op.payload
    if k == "CreateTable":
        sql = p["create_sql"] if not down else f"DROP TABLE IF EXISTS {_q(dialect, p['table'])}"
        return [sql]
    if k == "DropTable":
        if down:
            return [p["create_sql"]]
        return [f"DROP TABLE IF EXISTS {_q(dialect, p['table'])}"]
    if k == "AddColumn":
        if down:
            return [
                f"ALTER TABLE {_q(dialect, p['table'])} DROP COLUMN {_q(dialect, p['column'])}"
            ]
        return [
            f"ALTER TABLE {_q(dialect, p['table'])} ADD COLUMN {p['ddl']}"
        ]
    if k == "DropColumn":
        if down:
            return [f"ALTER TABLE {_q(dialect, p['table'])} ADD COLUMN {p['ddl']}"]
        return [
            f"ALTER TABLE {_q(dialect, p['table'])} DROP COLUMN {_q(dialect, p['column'])}"
        ]
    if k == "RenameColumn":
        if down:
            return [
                f"ALTER TABLE {_q(dialect, p['table'])} RENAME COLUMN "
                f"{_q(dialect, p['new_column'])} TO {_q(dialect, p['old_column'])}"
            ]
        return [
            f"ALTER TABLE {_q(dialect, p['table'])} RENAME COLUMN "
            f"{_q(dialect, p['old_column'])} TO {_q(dialect, p['new_column'])}"
        ]
    if k == "AddIndex":
        cols = ", ".join(_q(dialect, c) for c in p["columns"])
        name = p["name"]
        if down:
            return [f"DROP INDEX IF EXISTS {_q(dialect, name)}"]
        return [
            f"CREATE INDEX IF NOT EXISTS {_q(dialect, name)} "
            f"ON {_q(dialect, p['table'])} ({cols})"
        ]
    if k == "DropIndex":
        cols = ", ".join(_q(dialect, c) for c in p["columns"])
        name = p["name"]
        if down:
            return [
                f"CREATE INDEX IF NOT EXISTS {_q(dialect, name)} "
                f"ON {_q(dialect, p['table'])} ({cols})"
            ]
        return [f"DROP INDEX IF EXISTS {_q(dialect, name)}"]
    raise MigrationError(f"Unknown operation {k}")


def diff_states(old: dict[str, Any], new: dict[str, Any], dialect: Dialect) -> list[Operation]:
    """Compare two registry snapshots and return migration operations."""
    ops: list[Operation] = []
    old_models = set(old)
    new_models = set(new)

    for name in sorted(new_models - old_models):
        ops.append(
            Operation(
                "CreateTable",
                {
                    "model": name,
                    "table": new[name]["table"],
                    "create_sql": _create_sql_from_state(new[name], dialect),
                },
            )
        )
        for ix in new[name].get("indexes", []):
            ops.append(_add_index_op(new[name]["table"], ix))
        for fname, fstate in new[name].get("fields", {}).items():
            if fstate.get("index") and not fstate.get("primary_key"):
                ops.append(_add_index_op(new[name]["table"], [fstate["column"]]))

    for name in sorted(old_models - new_models):
        ops.append(
            Operation(
                "DropTable",
                {
                    "model": name,
                    "table": old[name]["table"],
                    "create_sql": _create_sql_from_state(old[name], dialect),
                },
            )
        )

    for name in sorted(old_models & new_models):
        ops.extend(_diff_model(old[name], new[name], dialect))
    return ops


def _diff_model(old: dict[str, Any], new: dict[str, Any], dialect: Dialect) -> list[Operation]:
    ops: list[Operation] = []
    table = new["table"]
    old_fields: dict[str, Any] = old.get("fields", {})
    new_fields: dict[str, Any] = new.get("fields", {})
    old_names = set(old_fields)
    new_names = set(new_fields)
    added = new_names - old_names
    removed = old_names - new_names

    # Best-effort rename: one removed + one added with the same field class.
    renamed: set[str] = set()
    if len(added) == 1 and len(removed) == 1:
        old_name = next(iter(removed))
        new_name = next(iter(added))
        if old_fields[old_name].get("class") == new_fields[new_name].get("class"):
            ops.append(
                Operation(
                    "RenameColumn",
                    {
                        "table": table,
                        "old_column": old_fields[old_name]["column"],
                        "new_column": new_fields[new_name]["column"],
                    },
                )
            )
            renamed.add(old_name)
            renamed.add(new_name)

    for fname in sorted(removed):
        if fname in renamed:
            continue
        st = old_fields[fname]
        ops.append(
            Operation(
                "DropColumn",
                {
                    "table": table,
                    "column": st["column"],
                    "ddl": _column_ddl(st, dialect),
                },
            )
        )
    for fname in sorted(added):
        if fname in renamed:
            continue
        st = new_fields[fname]
        ops.append(
            Operation(
                "AddColumn",
                {
                    "table": table,
                    "column": st["column"],
                    "ddl": _column_ddl(st, dialect),
                },
            )
        )

    old_ix = {tuple(x) for x in old.get("indexes", [])}
    new_ix = {tuple(x) for x in new.get("indexes", [])}
    for ix in sorted(new_ix - old_ix):
        ops.append(_add_index_op(table, list(ix)))
    for ix in sorted(old_ix - new_ix):
        ops.append(
            Operation(
                "DropIndex",
                {"table": table, "columns": list(ix), "name": _index_name(table, list(ix))},
            )
        )
    return ops


def _add_index_op(table: str, columns: list[str]) -> Operation:
    return Operation(
        "AddIndex",
        {"table": table, "columns": list(columns), "name": _index_name(table, list(columns))},
    )


def _index_name(table: str, columns: list[str]) -> str:
    return f"ix_{table}_{'_'.join(columns)}"


def _column_ddl(state: dict[str, Any], dialect: Dialect) -> str:
    """Rebuild a column DDL fragment from serialized field state."""
    cls_name = state["class"]
    col = dialect.quote(state["column"])
    type_sql = _type_for(cls_name, state, dialect)
    if cls_name == "PrimaryKeyField" or state.get("primary_key"):
        return dialect.pk_column_ddl(state["column"])
    bits = [col, type_sql]
    if not state.get("null"):
        bits.append("NOT NULL")
    if state.get("unique"):
        bits.append("UNIQUE")
    return " ".join(bits)


def _type_for(cls_name: str, state: dict[str, Any], dialect: Dialect) -> str:
    mapping = {
        "IntegerField": dialect.integer_type(),
        "PrimaryKeyField": dialect.integer_type(),
        "CharField": dialect.varchar_type(int(state.get("max_length") or 255)),
        "TextField": dialect.text_type(),
        "BooleanField": dialect.boolean_type(),
        "DateTimeField": dialect.datetime_type(),
        "FloatField": dialect.float_type(),
        "ForeignKey": dialect.integer_type(),
    }
    return mapping.get(cls_name, dialect.text_type())


def _create_sql_from_state(model_state: dict[str, Any], dialect: Dialect) -> str:
    columns = []
    for _name, st in model_state.get("fields", {}).items():
        columns.append(_column_ddl(st, dialect))
    body = ",\n  ".join(columns)
    return f"CREATE TABLE IF NOT EXISTS {dialect.quote(model_state['table'])} (\n  {body}\n)"


def _render_migration_file(name: str, ops: list[Operation], dialect_name: str) -> str:
    ops_literal = json.dumps([{"kind": o.kind, "payload": o.payload} for o in ops], indent=4)
    return (
        '"""Auto-generated by pyorm makemigrations. Review before applying."""\n'
        "from __future__ import annotations\n\n"
        "from pyorm.connection import Engine\n"
        "from pyorm.migrations import Operation, apply_operations\n\n"
        f"NAME = {name!r}\n"
        f"DIALECT = {dialect_name!r}\n"
        f"OPERATIONS = {ops_literal}\n\n"
        "def up(engine: Engine) -> None:\n"
        "    ops = [Operation(item['kind'], item['payload']) for item in OPERATIONS]\n"
        "    apply_operations(engine, ops, down=False)\n\n"
        "def down(engine: Engine) -> None:\n"
        "    ops = [Operation(item['kind'], item['payload']) for item in OPERATIONS]\n"
        "    apply_operations(engine, ops, down=True)\n"
    )


def apply_operations(engine: Engine, ops: list[Operation], *, down: bool = False) -> None:
    sequence = list(reversed(ops)) if down else ops
    for op in sequence:
        for sql in _sql_for(op, engine.dialect, down=down):
            engine.execute(sql)


class MigrationRunner:
    """Filesystem + ``_migrations`` table coordinator."""

    def __init__(self, directory: str | Path, engine: Engine | None = None) -> None:
        self.directory = Path(directory)
        self.engine = engine or get_engine()
        self.directory.mkdir(parents=True, exist_ok=True)
        self.state_path = self.directory / "state.json"

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, indent=2, default=str), encoding="utf-8")

    def makemigrations(self) -> Path | None:
        registry.finalize()
        new_state = registry.schema_state()
        old_state = self.load_state()
        ops = diff_states(old_state, new_state, self.engine.dialect)
        if not ops:
            return None
        existing = self._migration_files()
        next_num = 1
        if existing:
            next_num = int(existing[-1].stem.split("_", 1)[0]) + 1
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S")
        name = f"{next_num:04d}_auto_{stamp}"
        path = self.directory / f"{name}.py"
        path.write_text(
            _render_migration_file(name, ops, self.engine.dialect.name),
            encoding="utf-8",
        )
        self.save_state(new_state)
        return path

    def _migration_files(self) -> list[Path]:
        files = [p for p in self.directory.glob("*.py") if re.match(r"^\d{4}_", p.stem)]
        return sorted(files, key=lambda p: p.stem)

    def _ensure_table(self) -> None:
        d = self.engine.dialect
        # Postgres INTEGER PRIMARY KEY is not auto-incrementing; SERIAL is.
        # SQLite INTEGER PRIMARY KEY AUTOINCREMENT aliases the rowid.
        sql = (
            f"CREATE TABLE IF NOT EXISTS {d.quote(MIGRATIONS_TABLE)} ("
            f"{d.pk_column_ddl('id')}, "
            f"{d.quote('name')} {d.varchar_type(255)} NOT NULL UNIQUE, "
            f"{d.quote('applied_at')} {d.datetime_type()} NOT NULL)"
        )
        self.engine.execute(sql)

    def applied_names(self) -> list[str]:
        self._ensure_table()
        d = self.engine.dialect
        rows = self.engine.fetchall(
            f"SELECT {d.quote('name')} FROM {d.quote(MIGRATIONS_TABLE)} ORDER BY {d.quote('id')}"
        )
        return [row[0] for row in rows]

    def migrate(self) -> list[str]:
        self._ensure_table()
        applied = set(self.applied_names())
        ran: list[str] = []
        for path in self._migration_files():
            if path.stem in applied:
                continue
            module = _load_migration(path)
            module.up(self.engine)
            self._record(path.stem)
            ran.append(path.stem)
        return ran

    def rollback(self) -> str | None:
        self._ensure_table()
        applied = self.applied_names()
        if not applied:
            return None
        name = applied[-1]
        path = self.directory / f"{name}.py"
        if not path.exists():
            raise MigrationError(f"Migration file missing for {name}")
        module = _load_migration(path)
        module.down(self.engine)
        d = self.engine.dialect
        self.engine.execute(
            f"DELETE FROM {d.quote(MIGRATIONS_TABLE)} WHERE {d.quote('name')} = {d.placeholder}",
            [name],
        )
        return name

    def _record(self, name: str) -> None:
        d = self.engine.dialect
        now = datetime.now(timezone.utc).isoformat()
        self.engine.execute(
            f"INSERT INTO {d.quote(MIGRATIONS_TABLE)} "
            f"({d.quote('name')}, {d.quote('applied_at')}) "
            f"VALUES ({d.placeholder}, {d.placeholder})",
            [name, now],
        )


def _load_migration(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(path.stem, path)
    if spec is None or spec.loader is None:
        raise MigrationError(f"Cannot import migration {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if not hasattr(module, "up") or not hasattr(module, "down"):
        raise MigrationError(f"{path} must define up() and down()")
    return module
