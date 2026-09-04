from __future__ import annotations

from datetime import datetime

import pytest

from pyorm.exceptions import ValidationError
from pyorm.fields import BooleanField, CharField, DateTimeField, FloatField, IntegerField
from pyorm.models import Model


def test_charfield_enforces_max_length() -> None:
    field = CharField(max_length=3)
    field.name = "name"
    with pytest.raises(ValidationError):
        field.validate("abcd")
    field.validate("ab")


def test_integer_coercion() -> None:
    field = IntegerField()
    field.name = "age"
    assert field.to_python("7") == 7
    with pytest.raises(ValidationError):
        field.to_python("nope")


def test_boolean_coercion() -> None:
    field = BooleanField()
    field.name = "flag"
    assert field.to_python(1) is True
    assert field.to_python("false") is False
    with pytest.raises(ValidationError):
        field.to_python("maybe")


def test_datetime_iso() -> None:
    field = DateTimeField()
    field.name = "ts"
    parsed = field.to_python("2024-01-02 03:04:05")
    assert parsed == datetime(2024, 1, 2, 3, 4, 5)


def test_float_coercion() -> None:
    field = FloatField()
    field.name = "score"
    assert field.to_python("1.5") == 1.5


def test_null_rejected() -> None:
    field = CharField(max_length=10)
    field.name = "name"

    class Dummy(Model):
        name = CharField(max_length=10)

    field.model = Dummy
    with pytest.raises(ValidationError):
        field.validate(None)


def test_default_callable() -> None:
    field = IntegerField(default=lambda: 42)
    assert field.get_default() == 42
