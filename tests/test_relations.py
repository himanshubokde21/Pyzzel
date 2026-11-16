import os

import pytest

from pyzzel import Base, Column, Integer, Numeric, String


DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)


@pytest.fixture
def models(database, prepare_test_table):
    names = (
        "pyzzel_test_orders", "pyzzel_test_users", "pyzzel_test_comments",
        "pyzzel_test_items", "pyzzel_test_tickets", "pyzzel_test_categories",
    )
    for name in names:
        prepare_test_table(name)

    class Order(Base):
        __tablename__ = "pyzzel_test_orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="pyzzel_test_users.id", on_delete="CASCADE")
        total = Column(Numeric, index=False)
        note = Column(String, index=True)

    class User(Base):
        __tablename__ = "pyzzel_test_users"
        id = Column(Integer, primary_key=True)
        name = Column(String, nullable=False)

    class Comment(Base):
        __tablename__ = "pyzzel_test_comments"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="pyzzel_test_users.id", on_delete="SET NULL")
        text = Column(String)

    class Item(Base):
        __tablename__ = "pyzzel_test_items"
        id = Column(Integer, primary_key=True)
        order_id = Column(Integer, foreign_key="pyzzel_test_orders.id", on_delete="RESTRICT")

    class Ticket(Base):
        __tablename__ = "pyzzel_test_tickets"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="pyzzel_test_users.id")

    class Category(Base):
        __tablename__ = "pyzzel_test_categories"
        id = Column(Integer, primary_key=True)
        name = Column(String)
        parent_id = Column(Integer, foreign_key="pyzzel_test_categories.id", on_delete="SET NULL")

    Base.create_tables()
    return User, Order, Comment, Item, Ticket, Category


def test_create_tables_orders_dependencies_and_installs_constraints_and_indexes(models):
    User, Order, Comment, Item, Ticket, Category = models
    Base.create_tables()
    rows = Base.execute(
        "SELECT conrelid::regclass::text, confdeltype FROM pg_constraint "
        "WHERE contype = 'f' AND conrelid::regclass::text LIKE 'pyzzel_test_%'"
    )
    rules = dict(rows)
    assert rules["pyzzel_test_orders"] == "c"
    assert rules["pyzzel_test_items"] == "r"
    assert rules["pyzzel_test_comments"] == "n"
    assert rules["pyzzel_test_categories"] == "n"
    assert rules["pyzzel_test_tickets"] == "a"

    indexes = {row[0] for row in Base.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = ANY(%s)",
        [["pyzzel_test_orders", "pyzzel_test_comments"]],
    )}
    assert "ix_pyzzel_test_orders_user_id" in indexes
    assert "ix_pyzzel_test_orders_note" in indexes
    assert "ix_pyzzel_test_comments_user_id" in indexes
    assert "ix_pyzzel_test_orders_total" not in indexes


def test_foreign_key_enforcement_delete_rules_and_self_reference(models):
    from psycopg.errors import ForeignKeyViolation

    User, Order, Comment, Item, Ticket, Category = models
    with pytest.raises(ForeignKeyViolation):
        Order.create(user_id=999999, total=1)
    user = User.create(name="cascade")
    order = Order.create(user_id=user.id, total=4)
    Item.create(order_id=order.id)
    with pytest.raises(ForeignKeyViolation):
        Order.delete_where(Order.id == order.id)
    Item.delete_where(Item.order_id == order.id)
    Order.delete_where(Order.id == order.id)

    Comment.create(user_id=user.id, text="null on delete")
    Ticket.create(user_id=user.id)
    with pytest.raises(ForeignKeyViolation):
        User.delete_where(User.id == user.id)
    Ticket.delete_where(Ticket.user_id == user.id)
    assert User.delete_where(User.id == user.id) == 1
    assert Comment.select().first().user_id is None

    parent = Category.create(name="parent")
    child = Category.create(name="child", parent_id=parent.id)
    assert Category.delete_where(Category.id == parent.id) == 1
    assert Category.get(child.id).parent_id is None


def test_with_one_to_many_many_to_one_two_relations_and_count(models, monkeypatch):
    User, Order, Comment, Item, Ticket, Category = models
    empty = User.create(name="empty")
    one = User.create(name="one")
    many = User.create(name="many")
    first_order = Order.create(user_id=one.id, total=1)
    Order.create(user_id=many.id, total=2)
    Order.create(user_id=many.id, total=3)
    Order.create(user_id=many.id, total=4)
    Comment.create(user_id=one.id, text="hello")
    Comment.create(user_id=None, text="orphan")

    selected = User.select().where(User.id.in_([empty.id, one.id, many.id])).order_by(User.id).with_(
        orders=Order.user_id
    )
    result = selected.limit(3).all()
    assert [len(row.orders) for row in result] == [0, 1, 3]
    assert [row.id for row in result[2].orders] == sorted(row.id for row in result[2].orders)
    assert result[1].orders[0]._fields == ("id", "user_id", "total", "note")
    assert selected.count() == 3
    assert selected.first().orders == []

    loaded_orders = Order.select().where(Order.id == first_order.id).with_(user=Order.user_id).all()
    assert loaded_orders[0].user.id == one.id
    comments = Comment.select().order_by(Comment.id).with_(user=Comment.user_id).all()
    assert comments[0].user.id == one.id and comments[1].user is None
    both = User.select().where(User.id == one.id).with_(orders=Order.user_id, comments=Comment.user_id).all()
    assert len(both[0].orders) == 1 and len(both[0].comments) == 1


def test_with_query_counts_empty_results_guards_transactions_and_self_reference(models, monkeypatch):
    from pyzzel.errors import QueryError, TooManyRowsError

    User, Order, Comment, Item, Ticket, Category = models
    user = User.create(name="three")
    for value in (1, 2, 3):
        Order.create(user_id=user.id, total=value)

    calls = []
    original_execute = Base.execute.__func__

    def recording_execute(cls, sql, params=None):
        calls.append((sql, params))
        return original_execute(cls, sql, params)

    monkeypatch.setattr(Base, "execute", classmethod(recording_execute))
    User.select().where(User.id == user.id).with_(orders=Order.user_id).all()
    assert len(calls) == 2
    calls.clear()
    User.select().where(User.id == -1).with_(orders=Order.user_id).all()
    assert len(calls) == 1
    calls.clear()
    User.select().where(User.id == user.id).with_(orders=Order.user_id, comments=Comment.user_id).all()
    assert len(calls) == 3

    monkeypatch.setattr(Order, "max_rows", 2)
    with pytest.raises(TooManyRowsError):
        User.select().where(User.id == user.id).with_(orders=Order.user_id).all()

    with Base.transaction():
        other = User.create(name="transaction")
        Order.create(user_id=other.id, total=9)
        assert len(User.select().where(User.id == other.id).with_(orders=Order.user_id).first().orders) == 1

    with pytest.raises(QueryError, match="self-referencing"):
        Category.select().with_(children=Category.parent_id)
