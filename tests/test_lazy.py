from __future__ import annotations

from pyorm.session import Session
from tests.sample_models import Post, User


def test_lazy_queryset_does_not_hit_db_until_terminal(engine) -> None:
    engine.reset_query_stats()
    qs = Session(engine).query(User).filter(age__gt=1)
    assert engine.query_count == 0
    qs.count()
    assert engine.query_count == 1


def test_foreign_key_lazy_load_and_cache(engine) -> None:
    with Session(engine) as session:
        ada = User(name="Ada", age=36)
        session.add(ada)
        session.commit()
        # commit closed the context; reopen
    with Session(engine) as session:
        ada = session.query(User).filter(name="Ada").first()
        post = Post(title="Notes", author=ada)
        session.add(post)
        session.commit()

    session = Session(engine)
    post = session.query(Post).first()
    engine.reset_query_stats()
    author = post.author
    assert author.name == "Ada"
    first_count = engine.query_count
    assert first_count == 1
    assert post.author is author  # cached, no second query
    assert engine.query_count == first_count
    session.close()


def test_n_plus_one_vs_select_related(engine) -> None:
    with Session(engine) as session:
        u1 = User(name="A", age=20)
        u2 = User(name="B", age=21)
        session.add(u1)
        session.add(u2)
        session.commit()
        session.add(Post(title="p1", author=u1))
        session.add(Post(title="p2", author=u2))
        session.add(Post(title="p3", author=u1))
        session.commit()

    session = Session(engine)
    engine.reset_query_stats()
    posts = session.query(Post).all()
    names = [p.author.name for p in posts]
    lazy_queries = engine.query_count
    # 1 for posts + 1 per distinct author first seen (identity map reuses A)
    assert lazy_queries == 3  # posts + A + B
    assert set(names) == {"A", "B"}
    session.close()

    session = Session(engine)
    engine.reset_query_stats()
    posts = session.query(Post).select_related("author").all()
    names = [p.author.name for p in posts]
    assert names
    assert engine.query_count == 1
    session.close()
