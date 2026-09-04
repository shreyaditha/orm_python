"""Compare PyORM overhead against raw sqlite3 or psycopg2.

Set PYORM_PG_URL (or PYORM_DB_URL) to benchmark against live PostgreSQL.
Otherwise SQLite is used.
"""

from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from pyorm.connection import create_engine
from pyorm.fields import CharField, ForeignKey, IntegerField
from pyorm.models import Model, create_all, drop_all, registry
from pyorm.session import Session

N = 10_000


class BenchUser(Model):
    name = CharField(max_length=40)
    age = IntegerField()

    class Meta:
        table_name = "bench_user"


class BenchPost(Model):
    title = CharField(max_length=80)
    author = ForeignKey(BenchUser, related_name="bench_posts")

    class Meta:
        table_name = "bench_post"


def timed(label: str, fn) -> float:
    start = time.perf_counter()
    fn()
    elapsed = time.perf_counter() - start
    print(f"  {label:28s} {elapsed*1000:10.1f} ms")
    return elapsed


def main() -> None:
    url = os.environ.get("PYORM_DB_URL") or os.environ.get("PYORM_PG_URL")
    if url and (url.startswith("postgresql://") or url.startswith("postgres://")):
        _run_postgres(url)
    else:
        _run_sqlite()


def _run_sqlite() -> None:
    db_path = Path("_bench.sqlite")
    if db_path.exists():
        db_path.unlink()

    raw = sqlite3.connect(str(db_path))
    raw.execute(
        "CREATE TABLE bench_user (id INTEGER PRIMARY KEY AUTOINCREMENT, name VARCHAR(40) NOT NULL, age INTEGER NOT NULL)"
    )
    raw.execute(
        "CREATE TABLE bench_post ("
        "id INTEGER PRIMARY KEY AUTOINCREMENT, title VARCHAR(80) NOT NULL, "
        "author_id INTEGER NOT NULL REFERENCES bench_user(id))"
    )
    raw.commit()

    engine = create_engine(f"sqlite:///{db_path.as_posix()}")
    registry.finalize()
    create_all(engine)
    print(f"Backend: sqlite ({db_path})")
    _report(raw, engine, sqlite=True)
    raw.close()
    engine.close()
    db_path.unlink(missing_ok=True)


def _run_postgres(url: str) -> None:
    import psycopg2

    engine = create_engine(url)
    registry.finalize()
    drop_all(engine)
    create_all(engine)
    raw = psycopg2.connect(url)
    raw.autocommit = True
    print(f"Backend: postgres ({url.split('@')[-1]})")
    _report(raw, engine, sqlite=False)
    raw.close()
    drop_all(engine)
    engine.close()


def _report(raw: Any, engine, *, sqlite: bool) -> None:
    raw_label = "raw sqlite3" if sqlite else "raw psycopg2"
    print(f"\nBulk insert of {N} users + {N} posts")
    t_raw = timed(f"{raw_label} executemany", lambda: _raw_insert(raw, N, sqlite=sqlite))
    t_orm = timed("PyORM Session.add/commit", lambda: _orm_insert(engine, N))
    print(f"  overhead factor: {t_orm / t_raw:.1f}x")

    print("\nFiltered JOIN (posts where author.age > 50)")
    t_raw = timed(f"{raw_label} JOIN", lambda: _raw_join(raw, sqlite=sqlite))
    t_orm = timed("PyORM select_related + filter", lambda: _orm_join(engine))
    print(f"  overhead factor: {t_orm / t_raw:.1f}x")

    print("\nPaginated query (OFFSET 5000 LIMIT 50)")
    t_raw = timed(f"{raw_label} LIMIT/OFFSET", lambda: _raw_page(raw, sqlite=sqlite))
    t_orm = timed("PyORM offset/limit", lambda: _orm_page(engine))
    print(f"  overhead factor: {t_orm / t_raw:.1f}x")

    print("\nNote: PyORM pays for hydration, identity-map bookkeeping, and")
    print("descriptor dirty-tracking. Bulk inserts additionally pay per-row Python")
    print("object construction. Use executemany-style APIs for ETL; use the ORM")
    print("for application code where correctness and laziness matter more than microseconds.")


def _ph(sqlite: bool) -> str:
    return "?" if sqlite else "%s"


def _raw_insert(conn: Any, n: int, *, sqlite: bool) -> None:
    ph = _ph(sqlite)
    cur = conn.cursor() if not sqlite else conn
    execute = cur.executemany if not sqlite else conn.executemany
    execute(
        f"INSERT INTO bench_user (name, age) VALUES ({ph}, {ph})",
        [(f"u{i}", i % 80) for i in range(n)],
    )
    execute(
        f"INSERT INTO bench_post (title, author_id) VALUES ({ph}, {ph})",
        [(f"p{i}", (i % n) + 1) for i in range(n)],
    )
    if sqlite:
        conn.commit()
    elif hasattr(cur, "close"):
        cur.close()


def _orm_insert(engine, n: int) -> None:
    session = Session(engine)
    chunk = 500
    for start in range(0, n, chunk):
        for i in range(start, min(start + chunk, n)):
            session.add(BenchUser(name=f"orm-u{i}", age=i % 80))
        session.commit()
    users = session.query(BenchUser).filter(name__contains="orm-u").limit(n).all()
    for i, user in enumerate(users[:n]):
        session.add(BenchPost(title=f"orm-p{i}", author=user))
        if i % chunk == chunk - 1:
            session.commit()
    session.commit()
    session.close()


def _raw_join(conn: Any, *, sqlite: bool) -> None:
    sql = (
        "SELECT p.title, u.name FROM bench_post p "
        "JOIN bench_user u ON p.author_id = u.id WHERE u.age > 50 LIMIT 100"
    )
    if sqlite:
        conn.execute(sql).fetchall()
        return
    cur = conn.cursor()
    cur.execute(sql)
    cur.fetchall()
    cur.close()


def _orm_join(engine) -> None:
    session = Session(engine)
    list(
        session.query(BenchPost)
        .select_related("author")
        .filter(author__age__gt=50)
        .limit(100)
    )
    session.close()


def _raw_page(conn: Any, *, sqlite: bool) -> None:
    sql = "SELECT id, name, age FROM bench_user LIMIT 50 OFFSET 5000"
    if sqlite:
        conn.execute(sql).fetchall()
        return
    cur = conn.cursor()
    cur.execute(sql)
    cur.fetchall()
    cur.close()


def _orm_page(engine) -> None:
    session = Session(engine)
    session.query(BenchUser).offset(5000).limit(50).all()
    session.close()


if __name__ == "__main__":
    main()
