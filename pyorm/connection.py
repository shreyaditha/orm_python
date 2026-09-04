"""DB-API connection pool and engine.

The ORM never talks to sqlite3 or psycopg2 except through this module. That
is the seam that keeps QuerySet / Session backend-agnostic: they bind
parameters and hand ``(sql, params)`` to ``Engine.execute``.
"""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import unquote, urlparse

from pyorm.dialect import Dialect, PostgresDialect, SQLiteDialect
from pyorm.exceptions import ConfigurationError


class DBAPICursor(Protocol):
    description: Any
    lastrowid: int

    def execute(self, sql: str, params: Sequence[Any] = ...) -> Any: ...
    def fetchone(self) -> tuple[Any, ...] | None: ...
    def fetchall(self) -> list[tuple[Any, ...]]: ...
    def close(self) -> None: ...


class DBAPIConnection(Protocol):
    def cursor(self) -> DBAPICursor: ...
    def close(self) -> None: ...
    def commit(self) -> None: ...
    def rollback(self) -> None: ...


@dataclass
class QueryLogEntry:
    sql: str
    params: tuple[Any, ...]


@dataclass
class Engine:
    """Facade over a pool plus a dialect.

    ``query_count`` / ``query_log`` exist so tests and the demo can assert
    exact round-trips. That is how we *prove* laziness and the N+1 problem
    instead of waving at it.
    """

    url: str
    dialect: Dialect
    pool: ConnectionPool
    query_count: int = 0
    query_log: list[QueryLogEntry] = field(default_factory=list)
    echo: bool = False
    _count_lock: threading.Lock = field(default_factory=threading.Lock)

    def reset_query_stats(self) -> None:
        with self._count_lock:
            self.query_count = 0
            self.query_log.clear()

    def acquire(self) -> DBAPIConnection:
        return self.pool.acquire()

    def release(self, conn: DBAPIConnection) -> None:
        self.pool.release(conn)

    @contextmanager
    def connection(self) -> Iterator[DBAPIConnection]:
        conn = self.acquire()
        try:
            yield conn
        finally:
            self.release(conn)

    def execute(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        conn: DBAPIConnection | None = None,
        fetch: bool = False,
    ) -> list[tuple[Any, ...]] | None:
        """Run one parameterized statement.

        If ``conn`` is omitted a pooled connection is borrowed for this call
        only. Session.commit() passes its transactional connection so several
        statements share one BEGIN/COMMIT.
        """
        bind = tuple(self.dialect.adapt_param(p) for p in (params or ()))
        with self._count_lock:
            self.query_count += 1
            self.query_log.append(QueryLogEntry(sql=sql, params=bind))
        if self.echo:
            print(f"SQL[{self.query_count}]: {sql} | params={bind}")

        owns = conn is None
        if owns:
            conn = self.acquire()
        assert conn is not None
        try:
            cursor = conn.cursor()
            try:
                cursor.execute(sql, bind)
                rows: list[tuple[Any, ...]] | None = cursor.fetchall() if fetch else None
                if owns:
                    conn.commit()
                return rows
            finally:
                cursor.close()
        except Exception:
            if owns:
                conn.rollback()
            raise
        finally:
            if owns:
                self.release(conn)

    def fetchone(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        conn: DBAPIConnection | None = None,
    ) -> tuple[Any, ...] | None:
        rows = self.execute(sql, params, conn=conn, fetch=True)
        if not rows:
            return None
        return rows[0]

    def fetchall(
        self,
        sql: str,
        params: Sequence[Any] | None = None,
        *,
        conn: DBAPIConnection | None = None,
    ) -> list[tuple[Any, ...]]:
        rows = self.execute(sql, params, conn=conn, fetch=True)
        return rows or []

    def close(self) -> None:
        self.pool.close()


class ConnectionPool:
    """Thread-safe fixed-size pool.

    This is intentionally small: acquire/release with a condition variable,
    create up to ``max_size`` connections, and give them back on release.
    It is not industrial (no health checks, no LIFO discard). For a teaching
    ORM that is the right tradeoff — the *idea* of pooling is visible in
    ~60 lines, which you can walk through in an interview.

    SQLite ``:memory:`` is a special case: each connection is a different
    empty database, so we keep a single shared connection.
    """

    def __init__(
        self,
        creator: Callable[[], DBAPIConnection],
        max_size: int = 5,
        *,
        shared: bool = False,
    ) -> None:
        self._creator = creator
        self._max_size = max(1, max_size)
        self._shared = shared
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._idle: list[DBAPIConnection] = []
        self._created = 0
        self._closed = False
        self._shared_conn: DBAPIConnection | None = None
        if shared:
            self._shared_conn = creator()
            self._created = 1

    def acquire(self) -> DBAPIConnection:
        if self._shared:
            assert self._shared_conn is not None
            return self._shared_conn
        with self._cond:
            if self._closed:
                raise ConfigurationError("Connection pool is closed")
            while True:
                if self._idle:
                    return self._idle.pop()
                if self._created < self._max_size:
                    conn = self._creator()
                    self._created += 1
                    return conn
                self._cond.wait(timeout=30)
                if self._closed:
                    raise ConfigurationError("Connection pool is closed")

    def release(self, conn: DBAPIConnection) -> None:
        if self._shared:
            return
        with self._cond:
            if self._closed:
                try:
                    conn.close()
                except Exception:
                    pass
                return
            self._idle.append(conn)
            self._cond.notify()

    def close(self) -> None:
        with self._cond:
            self._closed = True
            while self._idle:
                conn = self._idle.pop()
                try:
                    conn.close()
                except Exception:
                    pass
            if self._shared_conn is not None:
                try:
                    self._shared_conn.close()
                except Exception:
                    pass
                self._shared_conn = None
            self._cond.notify_all()


def _sqlite_connect(database: str) -> sqlite3.Connection:
    conn = sqlite3.connect(database, check_same_thread=False, isolation_level=None)
    conn.row_factory = None
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _postgres_connect(url: str) -> DBAPIConnection:
    try:
        import psycopg2
    except ImportError as exc:
        raise ConfigurationError(
            "psycopg2 is required for PostgreSQL. Install with: pip install 'pyorm[postgres]'"
        ) from exc
    parsed = urlparse(url)
    dbname = unquote(parsed.path.lstrip("/"))
    conn = psycopg2.connect(
        host=parsed.hostname or "localhost",
        port=parsed.port or 5432,
        user=unquote(parsed.username) if parsed.username else None,
        password=unquote(parsed.password) if parsed.password else None,
        dbname=dbname,
    )
    conn.autocommit = True
    return conn  # type: ignore[return-value]


def create_engine(url: str, *, pool_size: int = 5, echo: bool = False) -> Engine:
    """Create an Engine from a SQLAlchemy-style URL (the only resemblance).

    Examples
    --------
    ``sqlite:///:memory:``
    ``sqlite:///./blog.db``
    ``postgresql://user:pass@localhost:5432/blog``
    """
    if url.startswith("sqlite:"):
        # sqlite:///relative  sqlite:////absolute  sqlite:///:memory:
        rest = url[len("sqlite:") :]
        if rest.startswith("////"):
            database = rest[3:]  # /abs path
        elif rest.startswith("///"):
            database = rest[3:]
        else:
            database = rest.lstrip("/")
        if not database:
            database = ":memory:"
        shared = database == ":memory:"
        pool = ConnectionPool(
            lambda db=database: _sqlite_connect(db),
            max_size=1 if shared else pool_size,
            shared=shared,
        )
        return Engine(url=url, dialect=SQLiteDialect(), pool=pool, echo=echo)

    if url.startswith("postgresql://") or url.startswith("postgres://"):
        pool = ConnectionPool(lambda: _postgres_connect(url), max_size=pool_size)
        return Engine(url=url, dialect=PostgresDialect(), pool=pool, echo=echo)

    raise ConfigurationError(f"Unsupported database URL: {url!r}")


_default_engine: Engine | None = None


def configure(url: str, **kwargs: Any) -> Engine:
    """Set the process-wide default engine used when no Session is provided."""
    global _default_engine
    _default_engine = create_engine(url, **kwargs)
    return _default_engine


def get_engine() -> Engine:
    if _default_engine is None:
        raise ConfigurationError(
            "No engine configured. Call pyorm.configure('sqlite:///:memory:') "
            "or pass a Session/Engine explicitly."
        )
    return _default_engine


def set_engine(engine: Engine | None) -> None:
    global _default_engine
    _default_engine = engine
