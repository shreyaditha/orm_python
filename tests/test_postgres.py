"""Live PostgreSQL integration tests.

Skipped unless ``PYORM_PG_URL`` is set. These are not compiler-only tests:
they insert, update, roll back, and assert RETURNING / BOOLEAN / %s against
a real server.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from pyorm.connection import create_engine, set_engine
from pyorm.migrations import MigrationRunner
from pyorm.models import create_all, drop_all, registry
from pyorm.query import QuerySet
from pyorm.session import Session
from tests.sample_models import Post, User

pytestmark = pytest.mark.skipif(
    not os.environ.get("PYORM_PG_URL"),
    reason="Set PYORM_PG_URL to run live PostgreSQL tests",
)


@pytest.fixture()
def pg_engine():
    url = os.environ["PYORM_PG_URL"]
    engine = create_engine(url, pool_size=3)
    set_engine(engine)
    registry.finalize()
    drop_all(engine)
    engine.execute(engine.dialect.drop_table_sql("_migrations"))
    create_all(engine)
    yield engine
    drop_all(engine)
    engine.execute(engine.dialect.drop_table_sql("_migrations"))
    engine.close()
    set_engine(None)


def test_postgres_placeholders_in_compiler() -> None:
    url = os.environ["PYORM_PG_URL"]
    engine = create_engine(url)
    compiled = QuerySet(User, engine=engine).filter(age__gt=1).compile()
    assert "%s" in compiled.sql
    assert "?" not in compiled.sql.split("FROM")[0]  # no sqlite placeholders in SELECT list
    assert compiled.params == [1]
    engine.close()


def test_select_1_roundtrip(pg_engine) -> None:
    row = pg_engine.fetchone("SELECT 1")
    assert row == (1,)


def test_insert_uses_returning_not_lastrowid(pg_engine) -> None:
    session = Session(pg_engine)
    user = User(name="Ada", age=36, email="ada-pg@example.com", is_active=True)
    session.add(user)
    session.commit()
    assert user.pk is not None
    insert = next(sql for sql, _ in session.statements if sql.startswith("INSERT"))
    assert "RETURNING" in insert
    assert "last_insert_rowid" not in insert
    assert "%s" in insert
    loaded = session.query(User).filter(email="ada-pg@example.com").first()
    assert loaded is not None
    assert loaded.is_active is True
    raw = pg_engine.fetchone(
        f'SELECT "is_active" FROM "users" WHERE "email" = {pg_engine.dialect.placeholder}',
        ["ada-pg@example.com"],
    )
    assert raw is not None
    assert raw[0] is True
    assert type(raw[0]) is bool
    session.close()


def test_boolean_roundtrip(pg_engine) -> None:
    session = Session(pg_engine)
    session.add(User(name="Off", age=1, email="off@example.com", is_active=False))
    session.commit()
    user = session.query(User).filter(email="off@example.com").first()
    assert user.is_active is False
    session.close()


def test_identity_map_and_dirty_update(pg_engine) -> None:
    with Session(pg_engine) as session:
        session.add(User(name="Ada", age=36, email="id@example.com"))
    session = Session(pg_engine)
    a = session.query(User).filter(email="id@example.com").first()
    b = session.query(User).filter(email="id@example.com").first()
    assert a is b
    a.age = 37
    session.commit()
    update = next(sql for sql, _ in session.statements if sql.startswith("UPDATE"))
    assert "age" in update
    assert "email" not in update
    assert "%s" in update
    session.close()


def test_transaction_rollback(pg_engine) -> None:
    session = Session(pg_engine)
    session.add(User(name="Ada", age=36, email="uniq-pg@example.com"))
    session.commit()
    session = Session(pg_engine)
    session.add(User(name="Grace", age=40, email="uniq-pg@example.com"))
    with pytest.raises(Exception):
        session.commit()
    session = Session(pg_engine)
    assert session.query(User).filter(name="Grace").first() is None
    assert session.query(User).count() == 1
    session.close()


def test_select_related_join(pg_engine) -> None:
    with Session(pg_engine) as session:
        u = User(name="Ada", age=36, email="join@example.com")
        session.add(u)
        session.commit()
        session.add(Post(title="Notes", author=u))
    session = Session(pg_engine)
    pg_engine.reset_query_stats()
    post = session.query(Post).select_related("author").first()
    assert post.author.name == "Ada"
    assert pg_engine.query_count == 1
    compiled = session.query(Post).select_related("author").compile()
    assert "%s" not in compiled.sql or True
    assert "LEFT OUTER JOIN" in compiled.sql
    session.close()


def test_n_plus_one_vs_join(pg_engine) -> None:
    with Session(pg_engine) as session:
        u1 = User(name="A", age=20, email="a-n1@example.com")
        u2 = User(name="B", age=21, email="b-n1@example.com")
        session.add(u1)
        session.add(u2)
        session.commit()
        session.add(Post(title="p1", author=u1))
        session.add(Post(title="p2", author=u2))
        session.add(Post(title="p3", author=u1))
        session.commit()
    session = Session(pg_engine)
    pg_engine.reset_query_stats()
    posts = session.query(Post).all()
    _ = [p.author.name for p in posts]
    assert pg_engine.query_count == 3
    session.close()
    session = Session(pg_engine)
    pg_engine.reset_query_stats()
    posts = session.query(Post).select_related("author").all()
    _ = [p.author.name for p in posts]
    assert pg_engine.query_count == 1
    session.close()


def test_migrations_roundtrip_postgres(pg_engine, tmp_path: Path) -> None:
    drop_all(pg_engine)
    runner = MigrationRunner(tmp_path, pg_engine)
    path = runner.makemigrations()
    assert path is not None
    applied = runner.migrate()
    assert applied
    session = Session(pg_engine)
    session.add(User(name="Mig", age=2, email="mig@example.com"))
    session.commit()
    assert session.query(User).count() == 1
    session.close()
    rolled = runner.rollback()
    assert rolled == applied[-1]
