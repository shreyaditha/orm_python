from __future__ import annotations

import pytest

from pyorm.session import Session
from tests.sample_models import User


def test_identity_map_returns_same_instance(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=36, email="ada@example.com"))

    session = Session(engine)
    a = session.query(User).filter(name="Ada").first()
    b = session.query(User).filter(name="Ada").first()
    assert a is b
    session.close()


def test_dirty_tracking_updates_only_changed_columns(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=36, email="ada@example.com"))

    session = Session(engine)
    user = session.query(User).filter(name="Ada").first()
    user.age = 37
    session.commit()
    updates = [sql for sql, _ in session.statements if sql.startswith("UPDATE")]
    assert len(updates) == 1
    assert "age" in updates[0]
    assert "email" not in updates[0]
    assert "name" not in updates[0]
    session.close()


def test_transaction_rollback_on_failure(engine) -> None:
    session = Session(engine)
    session.add(User(name="Ada", age=36, email="unique@example.com"))
    session.commit()

    session = Session(engine)
    session.add(User(name="Grace", age=40, email="unique@example.com"))
    with pytest.raises(Exception):
        session.commit()

    session = Session(engine)
    assert session.query(User).filter(name="Grace").first() is None
    assert session.query(User).count() == 1
    session.close()


def test_delete_removes_row(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Tmp", age=1))
    session = Session(engine)
    user = session.query(User).filter(name="Tmp").first()
    session.delete(user)
    session.commit()
    assert session.query(User).filter(name="Tmp").first() is None
    session.close()


def test_rollback_discards_pending_insert(engine) -> None:
    session = Session(engine)
    session.add(User(name="Ghost", age=1))
    session.rollback()
    assert session.query(User).filter(name="Ghost").first() is None
    session.close()
