from __future__ import annotations

from pyorm.session import Session
from tests.sample_models import Comment, Post, Tag, User


def test_reverse_fk_queryset(engine) -> None:
    with Session(engine) as session:
        user = User(name="Ada", age=36)
        session.add(user)
        session.commit()
        session.add(Post(title="One", author=user))
        session.add(Post(title="Two", author=user))
        session.commit()

    session = Session(engine)
    user = session.query(User).filter(name="Ada").first()
    titles = sorted(p.title for p in user.posts.all())
    assert titles == ["One", "Two"]
    session.close()


def test_m2m_add_remove_set(engine) -> None:
    with Session(engine) as session:
        user = User(name="Ada", age=36)
        session.add(user)
        py = Tag(name="python")
        orm = Tag(name="orm")
        session.add(py)
        session.add(orm)
        session.commit()
        post = Post(title="Hello", author=user)
        session.add(post)
        session.commit()
        post.tags.add(py, orm)
        names = sorted(t.name for t in post.tags.all())
        assert names == ["orm", "python"]
        post.tags.remove(orm)
        assert [t.name for t in post.tags.all()] == ["python"]
        post.tags.set([orm])
        assert [t.name for t in post.tags.all()] == ["orm"]
