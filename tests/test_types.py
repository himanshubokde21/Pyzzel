import subprocess
import sys

import pytest

from pyzzel import (
    Base,
    BigInteger,
    Boolean,
    Column,
    ColumnType,
    Date,
    DateTime,
    Float,
    Integer,
    JSON,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Time,
    UUID,
)
from pyzzel.errors import ModelDefinitionError
from pyzzel.sql import ColumnSpec, create_table


@pytest.fixture(autouse=True)
def restore_registry():
    original = Base._registry.copy()
    yield
    Base._registry.clear()
    Base._registry.update(original)


def test_all_column_types_have_postgresql_type_names():
    assert SmallInteger().sql_type == "SMALLINT"
    assert Integer().sql_type == "INTEGER"
    assert BigInteger().sql_type == "BIGINT"
    assert Float().sql_type == "DOUBLE PRECISION"
    assert Numeric().sql_type == "NUMERIC"
    assert Numeric(10).sql_type == "NUMERIC(10)"
    assert Numeric(10, 2).sql_type == "NUMERIC(10,2)"
    assert String().sql_type == "TEXT"
    assert String(50).sql_type == "VARCHAR(50)"
    assert Boolean().sql_type == "BOOLEAN"
    assert Date().sql_type == "DATE"
    assert Time().sql_type == "TIME"
    assert DateTime().sql_type == "TIMESTAMPTZ"
    assert DateTime(timezone=False).sql_type == "TIMESTAMP"
    assert UUID().sql_type == "UUID"
    assert JSON().sql_type == "JSONB"
    assert LargeBinary().sql_type == "BYTEA"
    assert isinstance(Numeric(), ColumnType)


@pytest.mark.parametrize("args", [(0,), (-1,), (True,), (10, 11), (None, 0), (10, True)])
def test_numeric_rejects_invalid_precision_or_scale(args):
    with pytest.raises(ValueError):
        Numeric(*args)


def test_column_accepts_type_classes_and_instances():
    types = [
        (SmallInteger, SmallInteger()),
        (Integer, Integer()),
        (BigInteger, BigInteger()),
        (Float, Float()),
        (Numeric, Numeric(10, 2)),
        (String, String(20)),
        (Boolean, Boolean()),
        (Date, Date()),
        (Time, Time()),
        (DateTime, DateTime(timezone=False)),
        (UUID, UUID()),
        (JSON, JSON()),
        (LargeBinary, LargeBinary()),
    ]
    for type_class, instance in types:
        assert isinstance(Column(type_class).type, type_class)
        assert isinstance(Column(instance).type, type_class)


def test_expected_model_ddl():
    class Item(Base):
        __tablename__ = "items"
        id = Column(BigInteger, primary_key=True)
        small = Column(SmallInteger)
        quantity = Column(Integer, nullable=False, server_default="0")
        price = Column(Numeric(10, 2))
        ratio = Column(Float)
        name = Column(String(50), nullable=False)
        notes = Column(String)
        active = Column(Boolean, server_default="TRUE")
        born = Column(Date)
        at = Column(Time)
        created = Column(DateTime, server_default="now()")
        local = Column(DateTime(timezone=False))
        token = Column(UUID)
        data = Column(JSON)
        blob = Column(LargeBinary)

    assert create_table(Item.__tablename__, Item._column_specs()) == (
        'CREATE TABLE IF NOT EXISTS "items" '
        '("id" BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "small" SMALLINT, '
        '"quantity" INTEGER NOT NULL DEFAULT 0, "price" NUMERIC(10,2), '
        '"ratio" DOUBLE PRECISION, "name" VARCHAR(50) NOT NULL, "notes" TEXT, '
        '"active" BOOLEAN DEFAULT TRUE, "born" DATE, "at" TIME, '
        '"created" TIMESTAMPTZ DEFAULT now(), "local" TIMESTAMP, "token" UUID, '
        '"data" JSONB, "blob" BYTEA, "version" INTEGER NOT NULL DEFAULT 1)'
    )


@pytest.mark.parametrize("pk_type", [SmallInteger, Integer, BigInteger])
def test_integer_primary_key_types_are_accepted(pk_type):
    class Item(Base):
        __tablename__ = f"pk_{pk_type.__name__.lower()}"
        id = Column(pk_type, primary_key=True)

    assert Item._pk_name == "id"


@pytest.mark.parametrize("pk_type", [Float, String, UUID, Boolean])
def test_non_integer_primary_keys_are_rejected(pk_type):
    with pytest.raises(ModelDefinitionError, match="integer type"):
        class Item(Base):
            __tablename__ = f"bad_pk_{pk_type.__name__.lower()}"
            id = Column(pk_type, primary_key=True)


@pytest.mark.parametrize("expression", ["0", "now()", "'active'"])
def test_server_default_accepts_trusted_expressions(expression):
    assert Column(String, server_default=expression).server_default == expression


@pytest.mark.parametrize("expression", [";", "--", "/*", "", "   ", 1])
def test_server_default_rejects_invalid_expressions(expression):
    with pytest.raises(ModelDefinitionError):
        Column(String, server_default=expression)


def test_create_table_revalidates_column_spec_default():
    columns = [
        ColumnSpec("id", "INTEGER", primary_key=True, identity=True),
        ColumnSpec("name", "TEXT", default="0; DROP TABLE items"),
    ]
    with pytest.raises(ValueError):
        create_table("items", columns)


def test_python_defaults_are_applied_without_tracking_and_row_loading_skips_them():
    next_values = iter(("first", "second"))

    class Item(Base):
        __tablename__ = "default_items"
        id = Column(Integer, primary_key=True)
        name = Column(String, default=lambda: next(next_values))
        tags = Column(JSON, default=list)

    first = Item()
    second = Item(name="explicit", tags=None)
    third = Item()
    assert first.name == "first" and second.name == "explicit"
    assert first.tags == [] and second.tags is None
    assert first.tags is not third.tags
    assert "_changed" not in first.__dict__
    assert "_changed" not in second.__dict__
    loaded = Item._from_row((1, "loaded", ["x"], 1))
    assert loaded.name == "loaded" and loaded.tags == ["x"]


def test_mutable_defaults_and_primary_key_defaults_are_rejected():
    for value in ([], {}, set(), bytearray()):
        with pytest.raises(ModelDefinitionError, match="default=list"):
            Column(JSON, default=value)
    with pytest.raises(ModelDefinitionError, match="primary key"):
        Column(Integer, primary_key=True, default=1)
    assert Column(JSON, default=list).default is list


def test_create_many_applies_defaults_on_copies_and_calls_factory_per_row(monkeypatch):
    calls = []

    def make_token():
        value = f"token-{len(calls)}"
        calls.append(value)
        return value

    class Connection:
        def execute(self, sql, params):
            self.sql, self.params = sql, params

            class Result:
                def fetchall(self):
                    return [(1,), (2,)]

            return Result()

    connection = Connection()

    class Item(Base):
        __tablename__ = "many_defaults"
        id = Column(Integer, primary_key=True)
        name = Column(String)
        token = Column(String, default=make_token)

    monkeypatch.setattr(Item, "_run_tx", classmethod(lambda cls, fn: fn(connection)))
    rows = [{"name": "one"}, {"name": "two"}]
    assert Item.create_many(rows) == [1, 2]
    assert rows == [{"name": "one"}, {"name": "two"}]
    assert calls == ["token-0", "token-1"]
    assert connection.params == ("one", "token-0", "two", "token-1")


def test_create_many_with_only_database_defaults_uses_regular_execute(monkeypatch):
    class Connection:
        def __init__(self):
            self.calls = 0

        def execute(self, sql, params):
            self.calls += 1
            assert sql == 'INSERT INTO "default_only" DEFAULT VALUES RETURNING "id"'
            assert params == ()

            class Result:
                def fetchone(self):
                    return (self_id,)

            self_id = self.calls
            return Result()

    class DefaultOnly(Base):
        __tablename__ = "default_only"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    connection = Connection()
    monkeypatch.setattr(
        DefaultOnly, "_run_tx", classmethod(lambda cls, fn: fn(connection))
    )
    assert DefaultOnly.create_many([{}, {}]) == [1, 2]


def test_database_conversions_and_json_conditions():
    data_column = Column(JSON)
    text_column = Column(String)
    assert data_column.to_db(None) is None
    assert Column(Integer).to_db(3) == 3
    adapted = data_column.to_db({"a": 1})
    assert adapted.obj == {"a": 1}

    class Item(Base):
        __tablename__ = "conversion_items"
        id = Column(Integer, primary_key=True)
        data = Column(JSON)
        name = Column(String)

    params = (Item.data == {"a": 1}).render()[1]
    assert params[0].obj == {"a": 1}
    assert (Item.name == "x").render()[1] == ("x",)
    assert text_column.to_db("x") == "x"


def test_fresh_import_does_not_import_optional_value_libraries_or_drivers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pyzzel; forbidden = {'psycopg', 'psycopg_pool', 'decimal', 'uuid'}; "
            "assert not (forbidden & sys.modules.keys()), forbidden & sys.modules.keys()",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
