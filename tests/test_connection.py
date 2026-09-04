from __future__ import annotations

from pyorm.connection import ConnectionPool, create_engine
from pyorm.session import Session
from tests.sample_models import User


def test_sqlite_roundtrip(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=36))
    session = Session(engine)
    assert session.query(User).filter(name="Ada").first().age == 36
    session.close()


def test_pool_reuses_connections() -> None:
    created = {"n": 0}

    def factory():
        created["n"] += 1
        engine = create_engine("sqlite:///:memory:")
        return engine.acquire()

    pool = ConnectionPool(factory, max_size=2, shared=False)
    a = pool.acquire()
    pool.release(a)
    b = pool.acquire()
    assert a is b
    assert created["n"] == 1
    pool.close()


def test_filter_lookup_execution(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Ada", age=36))
        session.add(User(name="Alan", age=12))
    session = Session(engine)
    adults = session.query(User).filter(age__gte=18).all()
    assert [u.name for u in adults] == ["Ada"]
    assert session.query(User).filter(name__contains="A").count() == 2
    assert session.query(User).filter(age__in=[12]).first().name == "Alan"
    assert session.query(User).exclude(age__lt=18).first().name == "Ada"
    session.close()
