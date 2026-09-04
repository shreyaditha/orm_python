"""Extra tests aimed at public edge cases and the 85% coverage bar."""

from __future__ import annotations

from pathlib import Path

import pytest

from pyorm.cli import main
from pyorm.connection import configure, create_engine, get_engine, set_engine
from pyorm.dialect import PostgresDialect, SQLiteDialect
from pyorm.exceptions import ConfigurationError, FieldError, ValidationError
from pyorm.fields import CharField, ForeignKey, IntegerField, PrimaryKeyField
from pyorm.migrations import MigrationRunner, Operation, apply_operations, diff_states
from pyorm.models import Model, create_all, drop_all, registry
from pyorm.query import QueryCompiler, QuerySet
from pyorm.session import Session, current_session
from tests.sample_models import Post, Tag, User


def test_get_engine_requires_configure() -> None:
    previous = None
    try:
        previous = get_engine()
    except ConfigurationError:
        previous = None
    set_engine(None)
    try:
        with pytest.raises(ConfigurationError):
            get_engine()
    finally:
        set_engine(previous)


def test_unsupported_url() -> None:
    with pytest.raises(ConfigurationError):
        create_engine("mysql://localhost/db")


def test_unknown_constructor_kwarg() -> None:
    with pytest.raises(FieldError):
        User(name="x", not_a_field=1)


def test_charfield_assignment_on_model(engine) -> None:
    u = User(name="ok")
    with pytest.raises(ValidationError):
        u.name = "x" * 200


def test_model_repr_eq_pk(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=1, email="eq@x.com"))
    session = Session(engine)
    a = session.query(User).filter(email="eq@x.com").first()
    assert "User" in repr(a)
    a.pk = a.pk
    assert a == session.query(User).filter(email="eq@x.com").first()
    session.close()


def test_query_get_success_and_none_and_bool(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Zed", age=9, email="zed@x.com"))
    session = Session(engine)
    z = session.query(User).get(email="zed@x.com")
    assert z.name == "Zed"
    empty = session.query(User).filter(name="missing")
    assert empty.none().all() == []
    assert empty.none().count() == 0
    assert bool(session.query(User).filter(email="zed@x.com")) is True
    compiled = session.query(User).offset(0).limit(10).compile()
    assert "LIMIT" in compiled.sql
    session.close()


def test_filter_by_fk_object_and_related_lookup(engine) -> None:
    with Session(engine) as session:
        u = User(name="Auth", age=40, email="auth@x.com")
        session.add(u)
        session.commit()
        session.add(Post(title="Hello World", author=u))
    session = Session(engine)
    u = session.query(User).filter(email="auth@x.com").first()
    posts = session.query(Post).filter(author=u).all()
    assert len(posts) == 1
    joined = session.query(Post).filter(author__name__contains="Auth").all()
    assert joined[0].title == "Hello World"
    ordered = session.query(Post).order_by("author__name").all()
    assert ordered
    session.close()


def test_fk_set_int_and_none(engine) -> None:
    session = Session(engine)
    u = User(name="N", age=1, email="n@x.com")
    session.add(u)
    session.commit()
    p = Post(title="t", author=u)
    session.add(p)
    session.commit()
    p.author = u.pk
    assert p.author_id == u.pk
    p.author = None
    assert p.author is None
    with pytest.raises(TypeError):
        p.author = "nope"
    p.author = u
    session.close()


def test_drop_all_and_create(engine) -> None:
    drop_all(engine)
    create_all(engine)
    with Session(engine) as session:
        session.add(User(name="After", age=1, email="after@x.com"))
    assert Session(engine).query(User).count() == 1


def test_postgres_compiler_placeholders(engine) -> None:
    qs = QuerySet(User, engine=engine).filter(age__gt=5).order_by("-name").offset(2)
    compiled = QueryCompiler(PostgresDialect()).compile_select(qs)
    assert "%s" in compiled.sql
    assert "OFFSET" in compiled.sql
    count_c = QueryCompiler(PostgresDialect()).compile_count(qs)
    assert "COUNT(*)" in count_c.sql


def test_session_context_sets_current(engine) -> None:
    with Session(engine) as session:
        assert current_session() is session
        session.add(User(name="Ctx", age=1, email="ctx@x.com"))
    assert current_session() is None


def test_delete_unsaved_is_noop(engine) -> None:
    session = Session(engine)
    ghost = User(name="Ghost", age=1)
    session.add(ghost)
    session.delete(ghost)
    session.commit()
    assert session.query(User).filter(name="Ghost").first() is None
    session.close()


def test_flush_then_query(engine) -> None:
    session = Session(engine)
    session.add(User(name="Flush", age=3, email="flush@x.com"))
    session.flush()
    assert session.query(User).filter(name="Flush").first() is not None
    session.close()


def test_apply_add_and_drop_index(engine, tmp_path: Path) -> None:
    ops = [
        Operation("AddIndex", {"table": "users", "columns": ["email"], "name": "ix_users_email_extra"}),
    ]
    apply_operations(engine, ops, down=False)
    apply_operations(engine, ops, down=True)


def test_drop_column_and_table_diff(engine) -> None:
    old = registry.schema_state()
    new = {k: v for k, v in old.items() if k != "Comment"}
    ops = diff_states(old, new, engine.dialect)
    assert any(op.kind == "DropTable" for op in ops)
    user_old = {
        "User": {
            **old["User"],
            "fields": {**old["User"]["fields"], "nickname": {
                "class": "CharField",
                "column": "nickname",
                "null": True,
                "unique": False,
                "index": False,
                "primary_key": False,
                "default": None,
                "max_length": 20,
            }},
        }
    }
    user_new = {"User": old["User"]}
    ops2 = diff_states(user_old, user_new, engine.dialect)
    assert any(op.kind == "DropColumn" for op in ops2)


def test_cli_makemigrations(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    db = tmp_path / "cli.db"
    migs = tmp_path / "migs"
    code = main(
        [
            "--db",
            f"sqlite:///{db.as_posix()}",
            "--migrations-dir",
            str(migs),
            "--models",
            "tests.sample_models",
            "makemigrations",
        ]
    )
    assert code == 0
    assert any(migs.glob("*.py"))
    code = main(
        [
            "--db",
            f"sqlite:///{db.as_posix()}",
            "--migrations-dir",
            str(migs),
            "migrate",
        ]
    )
    assert code == 0
    code = main(
        [
            "--db",
            f"sqlite:///{db.as_posix()}",
            "--migrations-dir",
            str(migs),
            "migrate",
            "--rollback",
        ]
    )
    assert code == 0


def test_engine_echo_and_connection_cm(engine) -> None:
    engine.echo = True
    with engine.connection() as conn:
        engine.execute("SELECT 1", conn=conn, fetch=True)
    engine.echo = False


def test_sqlite_offset_without_limit(engine) -> None:
    compiled = QuerySet(User, engine=engine).offset(3).compile()
    assert "OFFSET 3" in compiled.sql


def test_isnull_false(engine) -> None:
    compiled = QuerySet(User, engine=engine).filter(email__isnull=False).compile()
    assert "IS NOT NULL" in compiled.sql


def test_unsupported_lookup(engine) -> None:
    with pytest.raises(FieldError):
        QuerySet(User, engine=engine).filter(name__regex="x").compile()


def test_values_execution(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Val", age=8, email="val@x.com"))
    session = Session(engine)
    rows = session.query(User).filter(name="Val").values("name").all()
    assert rows[0]["name"] == "Val"
    session.close()


def test_m2m_all_empty_when_unsaved() -> None:
    p = Post(title="x", author_id=1)
    assert p.tags.all().all() == []


def test_count_uses_cache(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="C", age=1, email="c@x.com"))
    session = Session(engine)
    qs = session.query(User).filter(name="C")
    list(qs)
    assert qs.count() == 1
    assert qs.exists() is True
    session.close()
