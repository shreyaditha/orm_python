"""Command-line blog demo for PyORM.

Run from the repo root::

    python -m examples.blog_demo

Prints: migrations, inserts, lazy vs select_related query counts,
lookup filters, dirty-field UPDATE SQL, deletes, and many-to-many tags.
"""

from __future__ import annotations

import os
import tempfile
from datetime import datetime, timezone

from pyorm import (
    CharField,
    DateTimeField,
    ForeignKey,
    ManyToManyField,
    Model,
    Session,
    TextField,
    configure,
)
from pyorm.migrations import MigrationRunner
from pyorm.models import drop_all, registry

# --------------------------------------------------------------------------- models


class User(Model):
    name = CharField(max_length=80)
    email = CharField(max_length=120, unique=True)

    class Meta:
        table_name = "blog_user"


class Tag(Model):
    name = CharField(max_length=40, unique=True)

    class Meta:
        table_name = "blog_tag"


class Post(Model):
    title = CharField(max_length=200)
    body = TextField(default="")
    author = ForeignKey(User, related_name="posts")
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))
    tags = ManyToManyField(Tag, related_name="posts")

    class Meta:
        table_name = "blog_post"


class Comment(Model):
    post = ForeignKey(Post, related_name="comments")
    author = ForeignKey(User, related_name="comments")
    body = TextField()
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))

    class Meta:
        table_name = "blog_comment"


def banner(title: str) -> None:
    print("\n" + "=" * 72)
    print(title)
    print("=" * 72)


def main() -> None:
    url = os.environ.get("PYORM_DB_URL") or os.environ.get("PYORM_PG_URL") or "sqlite:///:memory:"
    engine = configure(url, echo=False)
    registry.finalize()
    print(f"Backend: {engine.dialect.name} ({url.split('@')[-1] if '@' in url else url})")

    drop_all(engine)
    engine.execute(engine.dialect.drop_table_sql("_migrations"))

    banner("1. Migrations")
    mig_dir = tempfile.mkdtemp(prefix="pyorm_demo_migs_")
    runner = MigrationRunner(mig_dir, engine)
    path = runner.makemigrations()
    print(f"Generated: {path}")
    applied = runner.migrate()
    print(f"Applied: {applied}")

    banner("2. Inserts via Session (Unit of Work)")
    with Session(engine) as session:
        ada = User(name="Ada Lovelace", email="ada@example.com")
        alan = User(name="Alan Turing", email="alan@example.com")
        session.add(ada)
        session.add(alan)
        python = Tag(name="python")
        orm = Tag(name="orm")
        session.add(python)
        session.add(orm)
    print(f"Users: {session.query(User).count()}")  # session closed; new query
    session = Session(engine)
    ada = session.query(User).filter(email="ada@example.com").first()
    alan = session.query(User).filter(email="alan@example.com").first()
    python = session.query(Tag).filter(name="python").first()
    orm_tag = session.query(Tag).filter(name="orm").first()
    p1 = Post(title="Notes on Analytical Engines", body="…", author=ada)
    p2 = Post(title="On Computable Numbers", body="…", author=alan)
    p3 = Post(title="More Engines", body="…", author=ada)
    session.add(p1)
    session.add(p2)
    session.add(p3)
    session.commit()
    p1.tags.add(python, orm_tag)
    p2.tags.add(python)
    session.add(Comment(post=p1, author=alan, body="Fascinating."))
    session.commit()
    session.close()
    print("Inserted 3 posts, 2 tags, 1 comment.")

    banner("3. Lazy loading vs select_related (N+1)")
    # A brand-new Session has an empty identity map. Reusing the insert
    # session would hide N+1 because Ada/Alan would already be cached.
    session = Session(engine)
    engine.reset_query_stats()
    posts = session.query(Post).all()
    titles = []
    for post in posts:
        titles.append(f"{post.title} by {post.author.name}")
    lazy_count = engine.query_count
    print("Lazy access to post.author (fresh Session, empty identity map):")
    for line in titles:
        print(f"  {line}")
    print(f"  Query count: {lazy_count}  (1 SELECT posts + 1 per distinct author)")
    print("  This is the N+1 problem: one query for the list, plus more for relations.")
    session.close()

    session = Session(engine)
    engine.reset_query_stats()
    posts = session.query(Post).select_related("author").all()
    titles = [f"{post.title} by {post.author.name}" for post in posts]
    eager_count = engine.query_count
    print("Same loop after .select_related('author'):")
    for line in titles:
        print(f"  {line}")
    print(f"  Query count: {eager_count}  (single JOIN — the N+1 fix)")

    banner("4. Lookup syntax")
    rows = (
        session.query(Post)
        .filter(title__contains="Engine")
        .order_by("-title")
        .all()
    )
    compiled = (
        session.query(Post).filter(title__contains="Engine").order_by("-title").compile()
    )
    print(f"SQL: {compiled.sql}")
    print(f"params: {compiled.params}")
    print("Results:", [p.title for p in rows])

    banner("5. Dirty-field UPDATE (only changed columns)")
    user = session.query(User).filter(email="ada@example.com").first()
    user.name = "Ada King"
    session.statements.clear()
    session.commit()
    for sql, params in session.statements:
        print(f"  {sql} | {params}")
    update = next(sql for sql, _ in session.statements if sql.startswith("UPDATE"))
    assert "name" in update
    assert "email" not in update
    print("Only `name` appears in SET — email was not dirtied.")

    banner("6. Delete + reverse relation + M2M")
    comment = session.query(Comment).first()
    session.delete(comment)
    session.commit()
    print(f"Comments remaining: {session.query(Comment).count()}")
    ada = session.query(User).filter(email="ada@example.com").first()
    print("Ada's posts via reverse FK:", [p.title for p in ada.posts.order_by("title")])
    post = session.query(Post).filter(title__contains="Analytical").first()
    print("Tags on first post:", [t.name for t in post.tags.all()])

    session.close()
    print("\nDemo complete.")


if __name__ == "__main__":
    main()
