from __future__ import annotations

from pyorm.connection import create_engine
from pyorm.dialect import PostgresDialect, SQLiteDialect
from pyorm.query import QuerySet
from tests.sample_models import Post, User


def test_filter_sql_parameterized_sqlite() -> None:
    engine = create_engine("sqlite:///:memory:")
    qs = QuerySet(User, engine=engine).filter(age__gt=18, name__contains="jo").order_by("-age").limit(5)
    compiled = qs.compile()
    assert "?" in compiled.sql
    assert "18" not in compiled.sql
    assert "jo" not in compiled.sql
    assert compiled.params == [18, "%jo%"]
    assert "WHERE" in compiled.sql
    assert "ORDER BY" in compiled.sql
    assert "LIMIT 5" in compiled.sql
    engine.close()


def test_exclude_and_in_lookups() -> None:
    engine = create_engine("sqlite:///:memory:")
    qs = QuerySet(User, engine=engine).filter(age__in=[1, 2, 3]).exclude(is_active=False)
    compiled = qs.compile()
    assert "IN (?, ?, ?)" in compiled.sql
    assert "NOT" in compiled.sql
    assert compiled.params[0:3] == [1, 2, 3]
    engine.close()


def test_isnull_lookup() -> None:
    engine = create_engine("sqlite:///:memory:")
    compiled = QuerySet(User, engine=engine).filter(email__isnull=True).compile()
    assert "IS NULL" in compiled.sql
    assert compiled.params == []
    engine.close()


def test_empty_in_is_unsatisfiable() -> None:
    engine = create_engine("sqlite:///:memory:")
    compiled = QuerySet(User, engine=engine).filter(age__in=[]).compile()
    assert "1=0" in compiled.sql
    engine.close()


def test_select_related_emits_join() -> None:
    engine = create_engine("sqlite:///:memory:")
    compiled = QuerySet(Post, engine=engine).select_related("author").filter(title__contains="py").compile()
    assert "LEFT OUTER JOIN" in compiled.sql
    assert "users" in compiled.sql
    assert compiled.params == ["%py%"]
    engine.close()


def test_values_selects_subset() -> None:
    engine = create_engine("sqlite:///:memory:")
    compiled = QuerySet(User, engine=engine).values("name", "age").compile()
    assert "name" in compiled.sql
    engine.close()


def test_postgres_placeholder_style() -> None:
    assert PostgresDialect().placeholder == "%s"
    assert SQLiteDialect().placeholder == "?"
