from __future__ import annotations

from pathlib import Path

from pyorm.cli import main
from pyorm.connection import create_engine
from pyorm.migrations import MigrationRunner, diff_states
from pyorm.models import registry
from tests.sample_models import User


def test_diff_create_table(engine) -> None:
    empty: dict = {}
    new = registry.schema_state()
    ops = diff_states(empty, new, engine.dialect)
    kinds = {op.kind for op in ops}
    assert "CreateTable" in kinds


def test_makemigrations_and_migrate_roundtrip(tmp_path: Path) -> None:
    engine = create_engine("sqlite:///:memory:")
    runner = MigrationRunner(tmp_path, engine)
    # Fresh state vs current models
    path = runner.makemigrations()
    assert path is not None
    assert path.exists()
    applied = runner.migrate()
    assert applied
    assert runner.applied_names() == applied
    # Second makemigrations is a no-op
    assert runner.makemigrations() is None
    rolled = runner.rollback()
    assert rolled == applied[-1]
    engine.close()


def test_add_column_diff(engine) -> None:
    state = registry.schema_state()
    old = {
        "User": {
            "table": "users",
            "fields": {
                k: v
                for k, v in state["User"]["fields"].items()
                if k != "score"
            },
            "m2m": {},
            "indexes": [],
        }
    }
    new = {"User": {**state["User"], "m2m": {}, "indexes": []}}
    ops = diff_states(old, new, engine.dialect)
    assert any(op.kind == "AddColumn" and op.payload["column"] == "score" for op in ops)


def test_rename_column_heuristic(engine) -> None:
    old = {
        "User": {
            "table": "users",
            "fields": {
                "id": {"class": "PrimaryKeyField", "column": "id", "null": True, "unique": False, "index": False, "primary_key": True, "default": None},
                "fullname": {"class": "CharField", "column": "fullname", "null": False, "unique": False, "index": False, "primary_key": False, "default": None, "max_length": 80},
            },
            "m2m": {},
            "indexes": [],
        }
    }
    new = {
        "User": {
            "table": "users",
            "fields": {
                "id": old["User"]["fields"]["id"],
                "name": {"class": "CharField", "column": "name", "null": False, "unique": False, "index": False, "primary_key": False, "default": None, "max_length": 80},
            },
            "m2m": {},
            "indexes": [],
        }
    }
    ops = diff_states(old, new, engine.dialect)
    assert len(ops) == 1
    assert ops[0].kind == "RenameColumn"


def test_cli_migrate_no_files(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "t.db"
    monkeypatch.chdir(tmp_path)
    code = main(["--db", f"sqlite:///{db.as_posix()}", "--migrations-dir", str(tmp_path / "migs"), "migrate"])
    assert code == 0
