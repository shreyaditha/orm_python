"""Hit remaining public APIs so coverage stays honestly above 85%."""

from __future__ import annotations

import pytest

from pyorm.connection import ConnectionPool, create_engine
from pyorm.dialect import PostgresDialect, SQLiteDialect
from pyorm.exceptions import ConfigurationError, MigrationError, ValidationError
from pyorm.fields import (
    BooleanField,
    CharField,
    DateTimeField,
    FloatField,
    IntegerField,
    PrimaryKeyField,
    TextField,
)
from pyorm.migrations import Operation, _sql_for
from pyorm.session import Session
from tests.sample_models import User


def test_both_dialects_ddl_and_adapt() -> None:
    sqlite = SQLiteDialect()
    pg = PostgresDialect()
    assert "AUTOINCREMENT" in sqlite.pk_column_ddl("id")
    assert "SERIAL" in pg.pk_column_ddl("id")
    assert sqlite.boolean_type() == "INTEGER"
    assert pg.boolean_type() == "BOOLEAN"
    assert sqlite.datetime_type() == "TEXT"
    assert pg.datetime_type() == "TIMESTAMP"
    assert sqlite.float_type() == "REAL"
    assert pg.float_type() == "DOUBLE PRECISION"
    assert sqlite.varchar_type(10) == "VARCHAR(10)"
    assert sqlite.last_insert_id_sql("t", "id") == "SELECT last_insert_rowid()"
    assert pg.last_insert_id_sql("t", "id") is None
    assert sqlite.adapt_param(True) == 1
    assert sqlite.adapt_param("x") == "x"
    assert sqlite.begin() == "BEGIN"
    assert pg.commit() == "COMMIT"
    assert pg.rollback() == "ROLLBACK"
    assert sqlite.qualify("t", "c") == '"t"."c"'


def test_field_validate_and_ddl() -> None:
    dialect = SQLiteDialect()
    with pytest.raises(ValidationError):
        CharField(max_length=0)
    pk = PrimaryKeyField()
    pk.name = "id"
    assert "PRIMARY KEY" in pk.ddl(dialect)
    tf = TextField()
    tf.name = "body"
    assert tf.to_python(1) == "1"
    assert tf.db_type(dialect) == "TEXT"
    bf = BooleanField()
    bf.name = "flag"
    assert bf.to_db(True, dialect) == 1
    assert bf.to_db(None, dialect) is None
    with pytest.raises(ValidationError):
        bf.to_python(object())
    ff = FloatField()
    ff.name = "n"
    with pytest.raises(ValidationError):
        ff.to_python("no")
    df = DateTimeField()
    df.name = "ts"
    with pytest.raises(ValidationError):
        df.to_python("not-a-date")
    with pytest.raises(ValidationError):
        df.to_python(1.5)
    assert df.to_db(None, dialect) is None
    integer = IntegerField(unique=True, index=True)
    integer.name = "age"
    integer.db_column = "age"
    ddl = integer.ddl(dialect)
    assert "UNIQUE" in ddl


def test_sql_for_every_operation() -> None:
    d = SQLiteDialect()
    create = Operation("CreateTable", {"table": "t", "create_sql": 'CREATE TABLE "t" (id INTEGER)'})
    assert "CREATE" in _sql_for(create, d, down=False)[0]
    assert "DROP TABLE" in _sql_for(create, d, down=True)[0]
    drop = Operation("DropTable", {"table": "t", "create_sql": 'CREATE TABLE "t" (id INTEGER)'})
    assert "DROP" in _sql_for(drop, d, down=False)[0]
    assert "CREATE" in _sql_for(drop, d, down=True)[0]
    add = Operation("AddColumn", {"table": "t", "column": "n", "ddl": '"n" INTEGER'})
    assert "ADD COLUMN" in _sql_for(add, d, down=False)[0]
    assert "DROP COLUMN" in _sql_for(add, d, down=True)[0]
    dropc = Operation("DropColumn", {"table": "t", "column": "n", "ddl": '"n" INTEGER'})
    assert "DROP COLUMN" in _sql_for(dropc, d, down=False)[0]
    assert "ADD COLUMN" in _sql_for(dropc, d, down=True)[0]
    rename = Operation("RenameColumn", {"table": "t", "old_column": "a", "new_column": "b"})
    assert "RENAME" in _sql_for(rename, d, down=False)[0]
    assert "TO" in _sql_for(rename, d, down=True)[0]
    ix = Operation("AddIndex", {"table": "t", "columns": ["a"], "name": "ix_t_a"})
    assert "CREATE INDEX" in _sql_for(ix, d, down=False)[0]
    assert "DROP INDEX" in _sql_for(ix, d, down=True)[0]
    dix = Operation("DropIndex", {"table": "t", "columns": ["a"], "name": "ix_t_a"})
    assert "DROP INDEX" in _sql_for(dix, d, down=False)[0]
    assert "CREATE INDEX" in _sql_for(dix, d, down=True)[0]
    with pytest.raises(MigrationError):
        _sql_for(Operation("Nope", {}), d, down=False)


def test_pool_close_then_acquire() -> None:
    created = []

    def factory():
        eng = create_engine("sqlite:///:memory:")
        conn = eng.acquire()
        created.append(eng)
        return conn

    pool = ConnectionPool(factory, max_size=1)
    conn = pool.acquire()
    pool.release(conn)
    pool.close()
    with pytest.raises(ConfigurationError):
        pool.acquire()
    for eng in created:
        eng.close()


def test_identity_map_helpers(engine) -> None:
    session = Session(engine)
    session.add(User(name="Id", age=1, email="idmap@x.com"))
    session.commit()
    obj = session.query(User).filter(email="idmap@x.com").first()
    assert obj in session.identity_map
    session.identity_map.remove(obj)
    assert session.identity_map.get(User, obj.pk) is None
    session.close()


def test_session_rollback_after_begin(engine) -> None:
    session = Session(engine)
    session.add(User(name="Rb", age=1, email="rb@x.com"))
    session.commit()
    u = session.query(User).filter(email="rb@x.com").first()
    u.name = "changed"
    session.rollback()
    session.close()


def test_add_existing_tracked_instance(engine) -> None:
    session = Session(engine)
    session.add(User(name="Tr", age=1, email="tr@x.com"))
    session.commit()
    u = session.query(User).filter(email="tr@x.com").first()
    session.add(u)
    session.commit()
    session.close()


def test_manager_and_index(engine) -> None:
    with Session(engine) as session:
        session.add(User(name="Idx", age=4, email="idx@x.com"))
    assert User.objects.filter(email="idx@x.com").first().name == "Idx"
    session = Session(engine)
    first = session.query(User).filter(email="idx@x.com").order_by("age")[0]
    assert first.email == "idx@x.com"
    cached = session.query(User).filter(email="idx@x.com")
    cached.all()
    assert cached.first().email == "idx@x.com"
    session.close()


def test_engine_fetch_helpers(engine) -> None:
    row = engine.fetchone("SELECT 1")
    assert row[0] == 1
    rows = engine.fetchall("SELECT 1")
    assert rows[0][0] == 1
    User.query()  # classmethod QuerySet
