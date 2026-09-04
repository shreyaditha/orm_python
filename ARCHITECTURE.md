# Architecture

This document is the interview companion to the code. Each section states the decision, the reason, and how SQLAlchemy or Django solved the same problem.

## 1. Metaclass models, not `__init_subclass__`

**Decision.** `ModelMeta` collects `Field` instances from the class dict, installs descriptors, invents a `PrimaryKeyField` named `id` if needed, and registers the class.

**Why.** The metaclass runs *while the class is being created*, which is the moment class attributes are still the `CharField()` objects the user typed. After creation those names must be descriptors, or `User.name = "x"` cannot participate in dirty tracking.

**Tradeoff.** `__init_subclass__` (SQLAlchemy 2.0 declarative, Pydantic v2) is easier to debug. A metaclass is what Django’s `ModelBase` still uses, and it is the version most interviewers expect you to draw on a whiteboard.

String `ForeignKey('User')` targets are resolved in `ModelRegistry.finalize()`, after every model class in the process has been imported. That is the same two-phase trick Django uses to allow circular relations.

## 2. Fields are schema + validation + descriptors

**Decision.** Each field knows `db_type(dialect)`, `validate()`, `to_python()`, `to_db()`, and becomes a `FieldDescriptor` on the model.

**Why.** Putting Python-side `CharField(max_length=…)` enforcement *and* `VARCHAR(n)` DDL on the same object keeps the migration differ honest: the JSON snapshot is serialized from the same `schema_state()` the compiler uses.

**Tradeoff.** SQLAlchemy splits `TypeEngine` (SQL) from validators (optional). Django keeps them together, like PyORM. Together is better for a teaching ORM; split is better when you need vendor-specific types without changing validation.

## 3. QuerySets are lazy descriptions

**Decision.** `filter`, `exclude`, `order_by`, `limit`, `offset`, `values`, and `select_related` all `_clone()` the QuerySet. SQL is compiled only in `_fetch_all`, `count`, `exists`, `first`, or slicing.

**Why.** Laziness lets you pass a QuerySet into a helper that adds more filters without issuing a query. It is also how we *prove* laziness in tests: `assert engine.query_count == 0` after `.filter(...)`.

**Tradeoff.** Django clones; SQLAlchemy’s `Query` / 2.0 `select()` is a statement object you execute explicitly (`session.scalars(stmt)`). Explicit execution is clearer. Clone-and-evaluate is the API people already know from Django, so the demo reads naturally.

**Injection.** Identifiers are dialect-quoted. Values never enter the SQL string. Lookups become `?` or `%s` plus a params list. `__contains` becomes `LIKE ?` with the `%` wrapped in the *parameter*, not the SQL.

## 4. ForeignKey loading is a descriptor (and that creates N+1)

**Decision.** `order.customer` is `ForwardFKDescriptor.__get__`. The first access SELECTs by PK and stores `_{name}_cache` on the instance. Later accesses return the cache. `select_related('customer')` emits a `LEFT OUTER JOIN` and fills that cache during hydration.

**Why.** Attributes that secretly run SQL are how every major ORM exposes relations. Descriptors are the Python protocol for “attribute access with extra behavior.”

**N+1.** `for order in session.query(Order).all(): print(order.customer)` is 1 query for orders plus one per distinct customer not already in the identity map. Tests in `tests/test_lazy.py` assert the exact counts. `select_related` collapses this to one JOIN. There is no implicit join-on-access (Django’s `select_related` is also opt-in; SQLAlchemy’s `joinedload` is opt-in too). Automatic join would hide cost; opt-in teaches the cost.

## 5. Session = Unit of Work + identity map + dirty UPDATE

**Decision.**

- `add` / `delete` / field assignment schedule work.
- `commit` wraps INSERT/UPDATE/DELETE in `BEGIN` … `COMMIT`, and `ROLLBACK` on any exception.
- UPDATE lists only keys in `_state.modified` (set by descriptors when `_state.initializing` is false).
- `(class, pk) → instance` so two fetches of `User` pk=1 are the same object.

**Why.** Without an identity map, `a.name = 'x'; b = query.get(id=a.pk); b.name` is still the old value — two Python objects for one row. Without dirty tracking, every commit writes every column and races with other writers. Without a transaction, a failed third INSERT would leave the first two committed.

**Tradeoff.** SQLAlchemy’s Session is a persistence context with flush auto-triggered before queries. PyORM flushes on `commit()` (and immediately for M2M helpers that need a PK). Auto-flush is convenient and surprising. Explicit commit is easier to explain: “nothing hits the database until commit.”

Django’s default is *no* unit of work: `instance.save()` is one UPDATE. That is simpler and makes N+1 writes easy. PyORM follows SQLAlchemy here because UoW is the concept the project exists to demonstrate.

## 6. Reverse FK and ManyToMany

**Decision.** A `ForeignKey(..., related_name='orders')` installs `ReverseManyDescriptor` on the parent: `user.orders` is a QuerySet (`WHERE author_id = ?`). `ManyToManyField` auto-creates a through model / join table. `.add()` / `.remove()` / `.set()` execute immediately once both sides have PKs.

**Why.** Immediate M2M SQL is Django’s model. Folding join-row inserts into `Session.commit()` would be more “pure UoW” but would mean `post.tags.add(tag)` appeared to do nothing until commit — confusing in a demo. The tradeoff is documented: M2M is not transactional with the rest of the unit unless you already have an open transaction connection.

SQLAlchemy uses `relationship()` with an explicit `secondary=` table and association-proxy helpers. More flexible; more configuration.

## 7. Connection pool and dialects

**Decision.** `Engine` + `Dialect` + a small thread-safe pool. SQLite `:memory:` uses a **shared** connection because every sqlite3 memory connection is a different empty database.

**Why.** The rest of the ORM only ever sees `(sql, params)`. PostgreSQL is a dialect (`%s`, `SERIAL`, `RETURNING`) behind the same interface. Live Postgres tests run only when `PYORM_PG_URL` is set so CI can be SQLite-only.

**Tradeoff.** This pool does not check liveness or recycle stale connections. SQLAlchemy’s `QueuePool` does. For a teaching library, 60 readable lines beat a hidden industrial pool.

## 8. Migrations as JSON state + generated Python

**Decision.** `registry.schema_state()` dumps field metadata to `migrations/state.json`. The differ emits `CreateTable`, `AddColumn`, `DropColumn`, `RenameColumn` (one-removed + one-added, same type), `AddIndex` / `DropIndex`. Files expose `up(engine)` / `down(engine)`. Applied names live in `_migrations`.

**Why.** JSON is grep-able in PRs (pickle is not). Generated Python is reviewable, unlike a black-box `alembic upgrade`. Rename is best-effort because true rename detection needs an identity that Python class attributes do not have — Django asks you to name the operation; Alembic has a similar heuristic.

**Tradeoff.** No autodetect for `null` / `max_length` changes, no data migrations, no concurrent-index Postgres flags. Those are where Alembic’s years of edge cases live. PyORM covers the operations you can explain on a whiteboard.

## 9. What we deliberately left out

| Feature | Why skip it |
|---|---|
| Implicit lazy joins on filter | Cost should be visible (`select_related`) |
| Attribute-based filter (`User.age > 18`) | Requires a ColumnElement system (SQLAlchemy core). Django-style kwargs are enough. |
| Composite primary keys | Identity map keys and UPDATE WHERE explode in complexity. |
| Async drivers | Would double the connection layer without teaching a new ORM idea. |
| Identity map eviction / weakrefs | SQLAlchemy uses weakrefs so abandoned instances can GC. PyORM keeps strong refs for the session lifetime — simpler, leakier if you keep a Session forever. |

## 10. How to talk through this in an interview

1. Draw Model → Field descriptors → table DDL.
2. Draw QuerySet clone → compiler → parameterized SQL → hydrate.
3. Draw `order.customer` descriptor → cache; then N+1 vs JOIN.
4. Draw Session: new / dirty / deleted; identity map box; BEGIN/COMMIT.
5. Admit overhead (run `python benchmarks/benchmark.py`) and say when you would drop to `executemany`.
