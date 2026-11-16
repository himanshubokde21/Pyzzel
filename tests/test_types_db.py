import datetime
from decimal import Decimal
import os
import uuid
from zoneinfo import ZoneInfo

import pytest

from pyzzel import (
    Base,
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    Float,
    JSON,
    LargeBinary,
    Numeric,
    SmallInteger,
    String,
    Time,
    UUID,
)


DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)


@pytest.fixture
def item_model(database, prepare_test_table):
    table = "pyzzel_test_items"
    prepare_test_table(table)

    class Item(Base):
        __tablename__ = table
        id = Column(BigInteger, primary_key=True)
        small = Column(SmallInteger)
        quantity = Column(BigInteger, nullable=False, server_default="0")
        price = Column(Numeric(10, 2))
        ratio = Column(Float)
        name = Column(String(50), nullable=False)
        notes = Column(String)
        active = Column(Boolean, server_default="TRUE")
        born = Column(Date)
        at = Column(Time)
        created = Column(DateTime, server_default="now()")
        local = Column(DateTime(timezone=False))
        token = Column(UUID, default=uuid.uuid4)
        data = Column(JSON)
        tags = Column(JSON, default=list)
        blob = Column(LargeBinary)

    Base.create_tables()
    return Item


def test_create_table_has_expected_postgresql_types(item_model):
    rows = Base.execute(
        "SELECT column_name, data_type, numeric_precision, numeric_scale, "
        "character_maximum_length FROM information_schema.columns "
        "WHERE table_schema = current_schema() AND table_name = %s "
        "ORDER BY ordinal_position",
        (item_model.__tablename__,),
    )
    actual = {
        name: (data_type, precision, scale, length)
        for name, data_type, precision, scale, length in rows
    }
    expected = {
        "id": ("bigint", 64, 0, None),
        "small": ("smallint", 16, 0, None),
        "quantity": ("bigint", 64, 0, None),
        "price": ("numeric", 10, 2, None),
        "ratio": ("double precision", 53, None, None),
        "name": ("character varying", None, None, 50),
        "notes": ("text", None, None, None),
        "active": ("boolean", None, None, None),
        "born": ("date", None, None, None),
        "at": ("time without time zone", None, None, None),
        "created": ("timestamp with time zone", None, None, None),
        "local": ("timestamp without time zone", None, None, None),
        "token": ("uuid", None, None, None),
        "data": ("jsonb", None, None, None),
        "tags": ("jsonb", None, None, None),
        "blob": ("bytea", None, None, None),
        "version": ("integer", 32, 0, None),
    }
    assert actual == expected


def test_typed_values_round_trip_through_create_get_and_select(item_model):
    Item = item_model
    expected_created = datetime.datetime(2025, 5, 6, 7, 8, 9, tzinfo=ZoneInfo("Asia/Kolkata"))
    expected_local = datetime.datetime(2025, 5, 6, 7, 8, 9, 123456)
    expected_born = datetime.date(2020, 2, 3)
    expected_time = datetime.time(12, 34, 56, 789123)
    expected_uuid = uuid.uuid4()
    expected_data = {"nested": {"answer": 42}, "values": [True, None, "x"]}
    expected_tags = ["one", {"two": 2}]
    expected_blob = b"\x00\xff\x80binary"

    item = Item.create(
        name="Typed",
        small=12,
        quantity=2**31 + 9,
        price=Decimal("12.34"),
        ratio=1.5,
        notes="round trip",
        active=False,
        born=expected_born,
        at=expected_time,
        created=expected_created,
        local=expected_local,
        token=expected_uuid,
        data=expected_data,
        tags=expected_tags,
        blob=expected_blob,
    )
    loaded = Item.get(item.id)
    assert isinstance(loaded.id, int) and loaded.id == item.id
    assert loaded.small == 12 and loaded.quantity == 2**31 + 9
    assert loaded.price == Decimal("12.34") and isinstance(loaded.price, Decimal)
    assert loaded.ratio == 1.5 and loaded.name == "Typed"
    assert loaded.active is False and loaded.born == expected_born
    assert loaded.at == expected_time
    assert loaded.created == expected_created
    assert loaded.local == expected_local
    assert loaded.token == expected_uuid
    assert loaded.data == expected_data and loaded.tags == expected_tags
    assert loaded.blob == expected_blob
    assert Item.select().where(Item.id == item.id).first().price == Decimal("12.34")
    assert Item.create(name="Small decimal", price=Decimal("0.1")).price == Decimal("0.1")


def test_nulls_server_defaults_and_per_object_python_defaults(item_model):
    Item = item_model
    nulls = Item.create(
        name="Nulls", small=None, price=None, ratio=None, notes=None, active=None,
        born=None, at=None, created=None, local=None, token=None, data=None, tags=None,
        blob=None,
    )
    loaded_nulls = Item.get(nulls.id)
    for name in (
        "small", "price", "ratio", "notes", "active", "born", "at", "created",
        "local", "token", "data", "tags", "blob",
    ):
        assert getattr(loaded_nulls, name) is None

    first = Item.create(name="Defaults one")
    second = Item.create(name="Defaults two")
    assert first.quantity == 0 and first.active is True
    assert first.created.tzinfo is not None
    assert isinstance(first.token, uuid.UUID) and first.token != second.token
    assert first.tags == [] and first.tags is not second.tags
    assert Item.get(first.id).token == first.token


def test_save_converts_json_decimal_and_date_but_not_in_place_list_changes(item_model):
    Item = item_model
    item = Item.create(
        name="Before", data={"state": "old"}, tags=["initial"],
        price=Decimal("1.25"), born=datetime.date(2020, 1, 1),
    )
    item.tags.append("in place")
    assert item._changes() == {}
    item.save()
    assert Item.get(item.id).tags == ["initial"]

    item.data = {"state": "new", "items": [1, 2]}
    item.price = Decimal("8.75")
    item.born = datetime.date(2024, 6, 7)
    item.save()
    loaded = Item.get(item.id)
    assert loaded.data == {"state": "new", "items": [1, 2]}
    assert loaded.price == Decimal("8.75")
    assert loaded.born == datetime.date(2024, 6, 7)


def test_bulk_and_conditional_writes_convert_json_and_numeric_values(item_model):
    Item = item_model
    ids = Item.create_many(
        [
            {"name": "JSON one", "data": {"kind": "match"}, "price": Decimal("1.20")},
            {"name": "JSON two", "data": {"kind": "other"}, "price": Decimal("2.30")},
        ]
    )
    assert Item.select().where(Item.data == {"kind": "match"}).first().id == ids[0]
    assert Item.update_where(Item.id == ids[0], data={"kind": "updated"}, price=Decimal("3.40")) == 1
    loaded = Item.get(ids[0])
    assert loaded.data == {"kind": "updated"} and loaded.price == Decimal("3.40")
    assert loaded.version == 2


def test_postgresql_type_range_errors_propagate_unchanged(item_model):
    import psycopg

    Item = item_model
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        Item.create(name="Too small", small=40000)
    with pytest.raises(psycopg.errors.StringDataRightTruncation):
        Item.create(name="x" * 51)


def test_typed_writes_participate_in_transactions(item_model):
    Item = item_model
    with pytest.raises(RuntimeError):
        with Base.transaction():
            item = Item.create(name="Transaction", data={"live": True}, price=Decimal("9.99"))
            assert Item.get(item.id).data == {"live": True}
            raise RuntimeError("rollback")
    assert Item.select().where(Item.name == "Transaction").count() == 0
