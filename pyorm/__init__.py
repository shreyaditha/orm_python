"""PyORM — a from-scratch Python ORM (no SQLAlchemy / Django / Peewee)."""

from __future__ import annotations

from pyorm.connection import Engine, configure, create_engine, get_engine, set_engine
from pyorm.exceptions import (
    ConfigurationError,
    DoesNotExist,
    FieldError,
    MigrationError,
    MultipleObjectsReturned,
    PyORMError,
    QueryError,
    SessionError,
    ValidationError,
)
from pyorm.fields import (
    NOT_PROVIDED,
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKey,
    IntegerField,
    ManyToManyField,
    PrimaryKeyField,
    TextField,
)
from pyorm.migrations import MigrationRunner
from pyorm.models import Model, create_all, drop_all, registry
from pyorm.query import QuerySet
from pyorm.session import Session, current_session

__all__ = [
    "BooleanField",
    "CharField",
    "ConfigurationError",
    "DateTimeField",
    "DoesNotExist",
    "Engine",
    "FieldError",
    "FloatField",
    "ForeignKey",
    "IntegerField",
    "ManyToManyField",
    "MigrationError",
    "MigrationRunner",
    "Model",
    "MultipleObjectsReturned",
    "NOT_PROVIDED",
    "PrimaryKeyField",
    "PyORMError",
    "QueryError",
    "QuerySet",
    "Session",
    "SessionError",
    "TextField",
    "ValidationError",
    "configure",
    "create_all",
    "create_engine",
    "current_session",
    "drop_all",
    "get_engine",
    "registry",
    "set_engine",
]

__version__ = "0.1.0"
