"""Public exception types raised by PyORM."""

from __future__ import annotations


class PyORMError(Exception):
    """Base class for every PyORM error."""


class ValidationError(PyORMError):
    """Raised when a field value fails validation before it is written."""


class FieldError(PyORMError):
    """Raised when a lookup or assignment refers to an unknown field."""


class QueryError(PyORMError):
    """Raised when a QuerySet is used incorrectly."""


class MultipleObjectsReturned(QueryError):
    """Raised by ``get()`` when more than one row matches."""


class DoesNotExist(QueryError):
    """Raised by ``get()`` when no row matches."""


class SessionError(PyORMError):
    """Raised for Unit-of-Work / identity-map misuse."""


class MigrationError(PyORMError):
    """Raised when schema diffing or migration apply/rollback fails."""


class ConfigurationError(PyORMError):
    """Raised when the engine or model registry is not ready."""
