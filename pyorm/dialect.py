"""SQL dialects. The rest of the ORM speaks only this interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class Dialect(ABC):
    """Backend-specific SQL fragments and parameter style.

    Query compilation never concatenates user values into SQL. Values always
    travel separately as bind parameters; only this object decides whether
    placeholders are ``?`` (SQLite) or ``%s`` (psycopg2).
    """

    name: str
    placeholder: str
    returning_supported: bool
    boolean_as_integer: bool

    def quote(self, identifier: str) -> str:
        """Quote an identifier, doubling any embedded quotes."""
        return '"' + identifier.replace('"', '""') + '"'

    def qualify(self, table: str, column: str) -> str:
        return f"{self.quote(table)}.{self.quote(column)}"

    @abstractmethod
    def pk_column_ddl(self, column: str) -> str:
        """DDL for an auto-incrementing integer primary key column."""

    @abstractmethod
    def boolean_type(self) -> str:
        """SQL type used to store booleans."""

    @abstractmethod
    def datetime_type(self) -> str:
        """SQL type used to store datetimes."""

    @abstractmethod
    def float_type(self) -> str:
        """SQL type used to store floats."""

    def varchar_type(self, max_length: int) -> str:
        return f"VARCHAR({max_length})"

    def integer_type(self) -> str:
        return "INTEGER"

    def text_type(self) -> str:
        return "TEXT"

    def begin(self) -> str:
        return "BEGIN"

    def commit(self) -> str:
        return "COMMIT"

    def rollback(self) -> str:
        return "ROLLBACK"

    def last_insert_id_sql(self, table: str, pk_column: str) -> str | None:
        """Follow-up SQL to read the generated PK, or None if RETURNING was used."""
        return None

    def drop_table_sql(self, table: str) -> str:
        return f"DROP TABLE IF EXISTS {self.quote(table)}"

    def adapt_param(self, value: Any) -> Any:
        """Convert a Python value into something the DB-API driver accepts."""
        return value


class SQLiteDialect(Dialect):
    """SQLite 3 dialect. Placeholders are ``?``; booleans are stored as 0/1."""

    name = "sqlite"
    placeholder = "?"
    returning_supported = False
    boolean_as_integer = True

    def pk_column_ddl(self, column: str) -> str:
        # INTEGER PRIMARY KEY is an alias for the rowid and auto-increments.
        return f"{self.quote(column)} INTEGER PRIMARY KEY AUTOINCREMENT"

    def boolean_type(self) -> str:
        return "INTEGER"

    def datetime_type(self) -> str:
        return "TEXT"

    def float_type(self) -> str:
        return "REAL"

    def last_insert_id_sql(self, table: str, pk_column: str) -> str:
        return "SELECT last_insert_rowid()"

    def adapt_param(self, value: Any) -> Any:
        if isinstance(value, bool):
            return int(value)
        return value


class PostgresDialect(Dialect):
    """PostgreSQL dialect via psycopg2 (``%s`` placeholders)."""

    name = "postgres"
    placeholder = "%s"
    returning_supported = True
    boolean_as_integer = False

    def pk_column_ddl(self, column: str) -> str:
        return f"{self.quote(column)} SERIAL PRIMARY KEY"

    def boolean_type(self) -> str:
        return "BOOLEAN"

    def datetime_type(self) -> str:
        return "TIMESTAMP"

    def float_type(self) -> str:
        return "DOUBLE PRECISION"

    def last_insert_id_sql(self, table: str, pk_column: str) -> str | None:
        return None

    def drop_table_sql(self, table: str) -> str:
        # CASCADE drops dependent FKs and SERIAL sequences so teardown is order-safe.
        return f"DROP TABLE IF EXISTS {self.quote(table)} CASCADE"
