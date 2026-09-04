"""Shared models for the test suite."""

from __future__ import annotations

from datetime import datetime, timezone

from pyorm.fields import (
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    ForeignKey,
    IntegerField,
    ManyToManyField,
    TextField,
)
from pyorm.models import Model


class User(Model):
    name = CharField(max_length=80)
    age = IntegerField(default=0)
    email = CharField(max_length=120, unique=True, null=True)
    is_active = BooleanField(default=True)
    score = FloatField(default=0.0)
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))

    class Meta:
        table_name = "users"
        indexes = [("name",)]


class Tag(Model):
    name = CharField(max_length=40, unique=True)


class Post(Model):
    title = CharField(max_length=200)
    body = TextField(default="")
    author = ForeignKey(User, related_name="posts")
    published = BooleanField(default=False)
    tags = ManyToManyField(Tag, related_name="posts")

    class Meta:
        table_name = "posts"


class Comment(Model):
    post = ForeignKey(Post, related_name="comments")
    author = ForeignKey(User, related_name="comments")
    body = TextField()
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))
