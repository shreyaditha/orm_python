# PyORM

A production-shaped Object-Relational Mapper written from scratch in Python — no SQLAlchemy, no Django ORM, no Peewee.

The only optional third-party dependency is `psycopg2` for PostgreSQL. SQLite uses Python's stdlib `sqlite3` driver.

```
66 tests passing · SQLite + PostgreSQL · Python 3.10+
```

---

## Why this exists

Most Python developers can *use* an ORM. Fewer can explain what's happening inside one.

PyORM is small enough to read in an afternoon and complete enough to defend in an interview. It makes the usual ORM internals visible:

- **Metaclass** — how `CharField()` on a class body becomes a descriptor after class creation
- **Descriptors** — how `order.customer` secretly runs SQL (and why that creates the N+1 problem)
- **Lazy QuerySets** — why `.filter(age__gt=18)` issues zero queries until you iterate
- **Unit of Work** — why a Session exists, what dirty tracking saves, why transactions matter
- **Identity map** — why two fetches of `User pk=1` return the same Python object
- **Migration differ** — how a JSON schema snapshot becomes `CREATE TABLE` / `ADD COLUMN` SQL

---

## Features

| Feature | Detail |
|---|---|
| **Models** | Metaclass field collection, auto `id` PK, `Meta.table_name` |
| **Fields** | `CharField`, `IntegerField`, `FloatField`, `BooleanField`, `TextField`, `DateTimeField`, `ForeignKey`, `ManyToManyField`, `PrimaryKeyField` |
| **QuerySet** | Lazy, chainable: `filter`, `exclude`, `order_by`, `limit`, `offset`, `values`, `select_related`, `count`, `exists`, `first`, `all` |
| **Lookup syntax** | `__gt`, `__gte`, `__lt`, `__lte`, `__contains`, `__in`, `__isnull` |
| **Session** | Unit of Work + identity map + dirty-only UPDATE + `BEGIN`/`COMMIT`/`ROLLBACK` |
| **Relations** | Forward FK (lazy descriptor), reverse FK (`related_name` QuerySet), M2M with auto join table |
| **Migrations** | `makemigrations` (JSON state diff) → reviewable Python files → `migrate` / rollback |
| **Dialects** | SQLite (`?` placeholders) and PostgreSQL (`%s`, `SERIAL`, `RETURNING`) behind a common interface |
| **Connection pool** | Thread-safe pool; SQLite `:memory:` uses a shared connection |
| **CLI** | `pyorm makemigrations` / `pyorm migrate` |

---

## Installation

```bash
# clone and install in editable mode (includes dev dependencies)
git clone https://github.com/example/pyorm.git
cd pyorm

python -m venv .venv
.venv\Scripts\activate        # Windows
source .venv/bin/activate     # macOS / Linux

pip install -e ".[dev]"
```

PostgreSQL support (optional):

```bash
pip install -e ".[postgres]"
```

---

## Quickstart

```python
from datetime import datetime, timezone
from pyorm import (
    CharField, DateTimeField, ForeignKey, IntegerField,
    Model, Session, configure, create_all,
)

configure("sqlite:///:memory:")

class User(Model):
    name = CharField(max_length=80)
    age  = IntegerField(default=0)

class Order(Model):
    customer   = ForeignKey(User, related_name="orders")
    created_at = DateTimeField(default=lambda: datetime.now(timezone.utc))

create_all()

with Session() as session:
    ada = User(name="Ada", age=36)
    session.add(ada)
    session.commit()
    session.add(Order(customer=ada))

    # lazy — no SQL yet
    qs = session.query(User).filter(age__gt=18).order_by("-name")

    # terminal call — SQL emitted here
    adults = qs.all()

    # one JOIN, no extra queries
    order = session.query(Order).select_related("customer").first()
    print(order.customer.name)   # "Ada"
```

---

## Lookup reference

```python
.filter(age__gt=18)          # >
.filter(age__gte=18)         # >=
.filter(age__lt=65)          # <
.filter(age__lte=65)         # <=
.filter(name__contains="da") # LIKE '%da%'
.filter(id__in=[1, 2, 3])    # IN (...)
.filter(email__isnull=True)  # IS NULL
.filter(name="Ada")          # exact match (default)
```

---

## Relations

### ForeignKey — lazy loading and N+1

```python
# 1 query for posts + 1 per distinct author = N+1
for post in session.query(Post).all():
    print(post.author.name)       # descriptor fires a SELECT on first access

# fix: collapse to a single JOIN
for post in session.query(Post).select_related("author").all():
    print(post.author.name)       # already in cache — no extra query
```

### Reverse FK

```python
# ForeignKey(User, related_name="posts") installs user.posts
user.posts.order_by("-created_at").all()
```

### ManyToMany

```python
class Post(Model):
    tags = ManyToManyField(Tag, related_name="posts")

post.tags.add(python_tag, orm_tag)
post.tags.remove(orm_tag)
post.tags.set([python_tag])        # replace all
post.tags.all()                    # QuerySet
```

---

## Session and Unit of Work

```python
with Session() as session:
    user = session.query(User).filter(email="ada@example.com").first()

    user.name = "Ada King"   # marks 'name' as dirty — nothing hits the DB yet
    session.commit()         # UPDATE blog_user SET "name" = ? WHERE "id" = ?
                             # only the dirtied column appears in SET
```

The identity map ensures two fetches of the same row return the same object:

```python
a = session.query(User).filter(id=1).first()
b = session.query(User).filter(id=1).first()
assert a is b   # True
```

---

## Migrations

```bash
# generate a migration file by diffing the current models against the saved state
pyorm makemigrations --models examples.blog_demo --db sqlite:///blog.db

# apply all pending migrations
pyorm migrate --db sqlite:///blog.db

# roll back the last migration
pyorm migrate --rollback --db sqlite:///blog.db
```

Each migration is a plain Python file with `up(engine)` and `down(engine)` — fully reviewable, no binary blobs.

Supported operations: `CreateTable`, `DropTable`, `AddColumn`, `DropColumn`, `RenameColumn`, `AddIndex`, `DropIndex`.

---

## Running the demo

```bash
python -m examples.blog_demo
```

Prints: migration generation, inserts, lazy vs `select_related` query counts, lookup SQL, dirty-field UPDATE, reverse FK, and M2M operations.

---

## Tests

```bash
pytest                                      # 66 tests, ~0.6 s
pytest --cov=pyorm --cov-report=term-missing
```

PostgreSQL tests run only when `PYORM_PG_URL` is set, so the suite is fully SQLite-only by default.

```bash
# run Postgres tests (requires a running Postgres instance)
$env:PYORM_PG_URL = "postgresql://user:pass@localhost/testdb"
pytest tests/test_postgres.py -v
```

---

## Benchmark

```bash
python benchmarks/benchmark.py
```

Compares PyORM against raw `sqlite3` for bulk inserts and reads. The output is honest — ORM abstraction has a measurable cost, and the benchmark shows exactly where.

---

## How `.filter().first()` runs

```
User.objects.filter(age__gt=18).first()
        │
        ▼
  QuerySet  — clone with WhereNode, still no SQL
        │
        │  .first() → terminal call
        ▼
  QueryCompiler  →  SELECT "t0"."id", "t0"."name", "t0"."age"
                    FROM "user" AS "t0"
                    WHERE "t0"."age" > ?
                    LIMIT 1        params=[18]
        │
        ▼
  Engine / connection pool  (dialect selects ? vs %s)
        │
        ▼
  hydrate row → Model instance → Session identity map
```

---

## Project structure

```
pyorm/
├── models.py        # ModelMeta metaclass, ModelOptions, Model base
├── fields.py        # Field types, validation, DDL, to_python / to_db
├── descriptors.py   # FieldDescriptor, ForwardFKDescriptor, ReverseManyDescriptor
├── query.py         # QuerySet, WhereNode, QueryCompiler
├── session.py       # Session, Unit of Work, identity map, dirty tracking
├── connection.py    # Engine, ConnectionPool, Dialect interface
├── dialect.py       # SQLiteDialect, PostgreSQLDialect
├── migrations.py    # MigrationRunner, schema differ, operation classes
├── exceptions.py    # PyORMError hierarchy
└── cli.py           # pyorm CLI entry point

examples/
└── blog_demo.py     # end-to-end demo (User, Post, Tag, Comment)

tests/               # 66 tests across connection, fields, queries, session,
                     # migrations, relationships, lazy loading, and Postgres
benchmarks/
└── benchmark.py     # ORM vs raw sqlite3
```

---

## Design decisions

A full explanation of every tradeoff — metaclass vs `__init_subclass__`, clone vs explicit execution, auto-flush vs commit-only, pooling, migration diffing, and what was deliberately left out — is in [ARCHITECTURE.md](ARCHITECTURE.md).

---

## License

MIT
