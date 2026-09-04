from __future__ import annotations

from pyorm.exceptions import DoesNotExist, MultipleObjectsReturned
from pyorm.session import Session
from tests.sample_models import User

import pytest


def test_exists_first_values_slice(engine) -> None:
    with Session(engine) as session:
        for i in range(5):
            session.add(User(name=f"U{i}", age=10 + i, email=f"u{i}@x.com"))

    session = Session(engine)
    qs = session.query(User).order_by("age")
    assert qs.exists() is True
    assert qs.first().name == "U0"
    page = qs.offset(2).limit(2).all()
    assert [u.name for u in page] == ["U2", "U3"]
    sliced = session.query(User).order_by("age")[1:3]
    assert [u.name for u in sliced] == ["U1", "U2"]
    rows = session.query(User).values("name", "age").order_by("age").all()
    assert rows[0]["name"] == "U0"
    assert session.query(User).filter(name="nope").exists() is False
    session.close()


def test_get_errors(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=1, email="a@x.com"))
        session.add(User(name="Ada", age=2, email="b@x.com"))
    session = Session(engine)
    with pytest.raises(MultipleObjectsReturned):
        session.query(User).get(name="Ada")
    with pytest.raises(DoesNotExist):
        session.query(User).get(name="Nobody")
    session.close()
