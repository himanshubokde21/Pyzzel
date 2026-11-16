import os

import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import QueryError, TooManyRowsError


DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)


@pytest.fixture
def models(database, prepare_test_table):
    tables = (
        "pyzzel_test_users", "pyzzel_test_orders", "pyzzel_test_items",
        "pyzzel_test_messages",
    )
    for table in tables:
        prepare_test_table(table)

    class Order(Base):
        __tablename__ = "pyzzel_test_orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="pyzzel_test_users.id")
        total = Column(Numeric)

    class User(Base):
        __tablename__ = "pyzzel_test_users"
        id = Column(Integer, primary_key=True)
        name = Column(String, nullable=False)
        age = Column(Integer)

    class Item(Base):
        __tablename__ = "pyzzel_test_items"
        id = Column(Integer, primary_key=True)
        order_id = Column(Integer, foreign_key="pyzzel_test_orders.id")
        sku = Column(String)

    class Message(Base):
        __tablename__ = "pyzzel_test_messages"
        id = Column(Integer, primary_key=True)
        sender_id = Column(Integer, foreign_key="pyzzel_test_users.id")
        receiver_id = Column(Integer, foreign_key="pyzzel_test_users.id")
        body = Column(String)

    Base.create_tables()
    return User, Order, Item, Message


def test_inner_left_join_and_duplicate_parent_rows(models):
    User, Order, _, _ = models
    empty = User.create(name="empty", age=10)
    single = User.create(name="single", age=20)
    multiple = User.create(name="multiple", age=30)
    Order.create(user_id=single.id, total=5)
    Order.create(user_id=multiple.id, total=10)
    Order.create(user_id=multiple.id, total=20)
    Order.create(user_id=multiple.id, total=30)

    left = User.select(User.name, Order.total).left_join(Order).order_by(User.id).all()
    assert left[0].name == "empty" and left[0].total is None
    assert left[1].name == "single" and left[1].total == 5
    assert left[2].name == "multiple" and left[2].total == 10
    assert left[0]._fields == ("name", "total")

    inner = User.select(User.name, Order.total).join(Order).all()
    assert len(inner) == 4 and all(row.total is not None for row in inner)
    duplicates = User.select(User.id).join(Order).where(Order.total > 1).all()
    distinct = User.select(User.id).join(Order).where(Order.total > 1).distinct().all()
    assert [row.id for row in duplicates].count(multiple.id) == 3
    assert [row.id for row in distinct].count(multiple.id) == 1


def test_labels_chain_child_side_order_limit_first_and_count(models):
    User, Order, Item, _ = models
    user = User.create(name="chain", age=25)
    first = Order.create(user_id=user.id, total=5)
    second = Order.create(user_id=user.id, total=15)
    Item.create(order_id=first.id, sku="a")
    Item.create(order_id=second.id, sku="b")
    Item.create(order_id=second.id, sku="c")

    rows = User.select(User.id, Order.id.label("order_id"), Item.sku).join(Order).join(Item).order_by(
        Item.sku.desc()
    ).limit(2).offset(1).all()
    assert [row._fields for row in rows] == [("id", "order_id", "sku")] * 2
    assert [row.sku for row in rows] == ["b", "a"]
    assert User.select(User.id, Order.id.label("order_id")).join(Order).first().id == user.id
    assert Order.select(Order.total, User.name).join(User).first().name == "chain"
    assert User.select().join(Order).count() == 2
    assert User.select().join(Order).where(Order.total > 10).count() == 1
    with pytest.raises(QueryError, match="label"):
        User.select(User.id, Order.id).join(Order).all()
    with pytest.raises(QueryError, match=r"count\(\) with distinct"):
        User.select().join(Order).distinct().count()


def test_explicit_foreign_key_for_two_message_directions(models):
    User, _, _, Message = models
    sender = User.create(name="sender")
    receiver = User.create(name="receiver")
    Message.create(sender_id=sender.id, receiver_id=receiver.id, body="hi")
    with pytest.raises(QueryError, match="on="):
        User.select().join(Message)
    row = User.select(User.name, Message.body).join(Message, on=Message.receiver_id).first()
    assert row.name == "receiver" and row.body == "hi"


def test_row_guard_transaction_and_parameterized_values(models, monkeypatch):
    User, Order, _, _ = models
    user = User.create(name="three")
    for total in (1, 2, 3):
        Order.create(user_id=user.id, total=total)
    monkeypatch.setattr(User, "max_rows", 2)
    with pytest.raises(TooManyRowsError):
        User.select().join(Order).all()
    assert len(User.select().join(Order).limit(1).all()) == 1

    malicious = "x'; DROP TABLE pyzzel_test_users;--"
    query = User.select().join(Order).where(User.name == malicious)
    statement = query.to_sql()
    assert malicious not in statement.sql and statement.params == (malicious,)
    assert query.all() == []
    assert Base.execute('SELECT COUNT(*) FROM "pyzzel_test_users"')[0][0] == 1

    with Base.transaction():
        inside = User.create(name="transaction")
        Order.create(user_id=inside.id, total=7)
        result = User.select(User.name, Order.total).where(User.id == inside.id).join(Order).first()
        assert result.name == "transaction" and result.total == 7
