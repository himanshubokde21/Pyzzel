import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import ModelDefinitionError, QueryError, TooManyRowsError
from pyzzel.sql import (
    ColumnRef, Join, OrderBy, Statement, count, select,
)


@pytest.fixture(autouse=True)
def restore_registry():
    original = Base._registry.copy()
    Base._registry.clear()
    yield
    Base._registry.clear()
    Base._registry.update(original)


@pytest.fixture
def models():
    class Common(Base):
        __abstract__ = True
        id = Column(Integer, primary_key=True)
        label = Column(String, nullable=False, default="x", unique=True)

    class User(Common):
        __tablename__ = "users"
        name = Column(String)
        age = Column(Integer)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")
        total = Column(Numeric)

    class Item(Base):
        __tablename__ = "items"
        id = Column(Integer, primary_key=True)
        order_id = Column(Integer, foreign_key="orders.id")
        sku = Column(String)

    class Message(Base):
        __tablename__ = "messages"
        id = Column(Integer, primary_key=True)
        sender_id = Column(Integer, foreign_key="users.id")
        receiver_id = Column(Integer, foreign_key="users.id")
        body = Column(String)

    return Common, User, Order, Item, Message


def test_abstract_columns_are_cloned_per_model_and_track_table(models):
    Common, User, Order, _, _ = models

    class Other(Base):
        __tablename__ = "other"
        id = Column(Integer, primary_key=True)
        label = Column(String)

    assert User.id is not Common.id and User.label is not Common.label
    assert User.id is not Order.id
    assert User.id.table == "users" and User.label.table == "users"
    assert Other.label.table == "other" and Other.label is not User.label
    assert User.version.table == "users"

    inherited = Column(
        String, nullable=True, unique=False, default=lambda: "default",
        server_default="now()", foreign_key="users.id", on_delete="SET NULL",
        index=False,
    )
    inherited.name = "reference"
    inherited.table = "users"
    clone = inherited._clone()
    assert clone is not inherited and clone.name == inherited.name
    assert clone.type is inherited.type
    assert (clone.primary_key, clone.nullable, clone.unique, clone.default,
            clone.server_default, clone.foreign_key, clone.on_delete, clone.index,
            clone.table) == (
        inherited.primary_key, inherited.nullable, inherited.unique, inherited.default,
        inherited.server_default, inherited.foreign_key, inherited.on_delete,
        inherited.index, inherited.table,
    )
    assert (User.age > 18).render(qualify=True) == ('"users"."age" > %s', (18,))


def test_qualified_condition_rendering_and_column_pairs(models):
    _, User, Order, _, _ = models
    assert (User.age > 18).render(qualify=True) == ('"users"."age" > %s', (18,))
    combined = (User.age > 18) & (Order.total == 100)
    assert combined.render(qualify=True) == (
        '("users"."age" > %s AND "orders"."total" = %s)', (18, 100)
    )
    assert (~(User.age > 18)).render(qualify=True) == ('NOT ("users"."age" > %s)', (18,))
    assert Order.total.is_null().render(qualify=True) == ('"orders"."total" IS NULL', ())
    assert User.id.in_([1, 2]).render(qualify=True) == ('"users"."id" = ANY(%s)', ([1, 2],))
    assert User.name.like("A%").render(qualify=True) == ('"users"."name" LIKE %s', ("A%",))
    assert ((User.age > 18) | (Order.total == 100)).render(qualify=True) == (
        '("users"."age" > %s OR "orders"."total" = %s)', (18, 100)
    )
    assert (User.age > 18).render() == ('"age" > %s', (18,))
    assert combined.columns() == [("users", "age"), ("orders", "total")]
    assert Order.total.desc() == OrderBy("total", "DESC", "orders")
    assert OrderBy("total", "DESC").table is None


def test_sql_builder_join_outputs_and_single_table_compatibility(models):
    _, User, Order, _, _ = models
    joins = [Join("INNER", "orders", "orders", "user_id", "users", "id")]
    statement = select(
        "users", [ColumnRef("users", "name"), ColumnRef("orders", "total")],
        where=Order.total > 100, order_by=[Order.total.desc()], limit=5, joins=joins,
    )
    assert statement == Statement(
        'SELECT "users"."name", "orders"."total" FROM "users" INNER JOIN "orders" '
        'ON "orders"."user_id" = "users"."id" WHERE "orders"."total" > %s '
        'ORDER BY "orders"."total" DESC LIMIT %s', (100, 5)
    )
    assert select("users", [ColumnRef("users", "id", "user_id")]).sql == (
        'SELECT "id" AS "user_id" FROM "users"'
    )
    assert select("users", [ColumnRef("users", "name", "n")]).sql == (
        'SELECT "name" AS "n" FROM "users"'
    )
    assert select("users", [ColumnRef("users", "id"), ColumnRef("users", "name")], distinct=True).sql == (
        'SELECT DISTINCT "id", "name" FROM "users"'
    )
    assert count("users", Order.total > 100, joins) == Statement(
        'SELECT COUNT(*) FROM "users" INNER JOIN "orders" ON "orders"."user_id" = "users"."id" '
        'WHERE "orders"."total" > %s', (100,)
    )
    assert select("users", ["name"]) == Statement('SELECT "name" FROM "users"', ())
    assert User.select(User.name).where(User.age > 18).to_sql() == Statement(
        'SELECT "name" FROM "users" WHERE "age" > %s', (18,)
    )


def test_sql_builder_rejects_invalid_join_identifiers_and_joined_columns():
    with pytest.raises(ValueError):
        select("users", ["id"], joins=[Join("RIGHT", "orders", "orders", "uid", "users", "id")])
    with pytest.raises(ValueError):
        select("users", ["id"], joins=[Join("INNER", "orders", "orders", "uid", "users", "id")])
    with pytest.raises(ValueError):
        select("users", ["id"], joins=[Join("INNER", "bad-name", "orders", "uid", "users", "id")])
    with pytest.raises(ValueError):
        select("users", [ColumnRef("users", "id", 'id";DROP')])
    with pytest.raises(ValueError):
        select("users", ["id"], joins=[Join("LEFT", "orders", "orders", "uid", "users", "id")])
    with pytest.raises(ValueError, match="ORDER BY"):
        select("users", [ColumnRef("users", "id")], order_by=[OrderBy("id", "ASC")],
               joins=[Join("LEFT", "orders", "orders", "uid", "users", "id")])


def test_query_join_sql_direction_chain_and_labels(models):
    _, User, Order, Item, Message = models
    assert User.select(User.name, Order.total).join(Order).where(Order.total > 100).order_by(
        Order.total.desc()
    ).limit(5).to_sql() == Statement(
        'SELECT "users"."name", "orders"."total" FROM "users" INNER JOIN "orders" '
        'ON "orders"."user_id" = "users"."id" WHERE "orders"."total" > %s '
        'ORDER BY "orders"."total" DESC LIMIT %s', (100, 5)
    )
    assert User.select(User.name, Order.total).left_join(Order).to_sql().sql == (
        'SELECT "users"."name", "orders"."total" FROM "users" LEFT JOIN "orders" '
        'ON "orders"."user_id" = "users"."id"'
    )
    assert Order.select(Order.total, User.name).join(User).to_sql().sql == (
        'SELECT "orders"."total", "users"."name" FROM "orders" INNER JOIN "users" '
        'ON "orders"."user_id" = "users"."id"'
    )
    assert User.select().join(Order).where(Order.total > 100).distinct().to_sql() == Statement(
        'SELECT DISTINCT "users"."id", "users"."label", "users"."name", "users"."age" '
        'FROM "users" INNER JOIN "orders" ON "orders"."user_id" = "users"."id" '
        'WHERE "orders"."total" > %s', (100,)
    )
    assert User.select(User.id, Order.id.label("order_id")).join(Order).to_sql().sql == (
        'SELECT "users"."id", "orders"."id" AS "order_id" FROM "users" INNER JOIN "orders" '
        'ON "orders"."user_id" = "users"."id"'
    )
    assert User.select(User.name, Item.sku).join(Order).join(Item).to_sql().sql.endswith(
        'INNER JOIN "items" ON "items"."order_id" = "orders"."id"'
    )
    assert User.select(User.name, Message.body).join(Message, on=Message.sender_id).to_sql().sql.endswith(
        'INNER JOIN "messages" ON "messages"."sender_id" = "users"."id"'
    )
    original = User.select()
    assert original.join(Order).distinct() is not original
    assert not original.joins and not original._distinct


def test_join_validation_deferred_ambiguity_and_immutability(models):
    _, User, Order, Item, Message = models
    with pytest.raises(QueryError, match="on="):
        User.select().join(Message)
    assert User.select(User.name, Message.body).join(Message, on=Message.sender_id).to_sql()
    with pytest.raises(QueryError, match="on="):
        User.select().join(Order, on=Message.sender_id)
    with pytest.raises(QueryError, match="no foreign key"):
        User.select().join(Item)
    with pytest.raises(QueryError, match="already in this query"):
        User.select().join(Order).join(Order)
    with pytest.raises(QueryError, match="registered"):
        User.select().join(str)

    base = User.select().where(Order.total > 1)
    joined = base.join(Order)
    assert base.joins == () and joined.joins
    assert joined.to_sql()
    for query in (
        User.select().where(Order.total > 1),
        User.select(Order.total),
        User.select().order_by(Order.total),
    ):
        with pytest.raises(QueryError, match="Join Order first"):
            query.to_sql()
    with pytest.raises(QueryError, match="Join Order first"):
        User.update_where(Order.total > 1, age=3)
    with pytest.raises(QueryError, match="Join Order first"):
        User.delete_where(Order.total > 1)
    with pytest.raises(QueryError, match="label"):
        User.select(User.id, Order.id).join(Order).to_sql()
    with pytest.raises(QueryError, match="Invalid column label"):
        User.id.label('x";DROP')

    with pytest.raises(QueryError, match=r"with_\(\)"):
        User.select().with_(orders=Order.user_id).join(Order)
    with pytest.raises(QueryError, match=r"with_\(\)"):
        User.select().join(Order).with_(orders=Order.user_id)

    class Unrelated(Base):
        __tablename__ = "unrelated"
        id = Column(Integer, primary_key=True)
        value = Column(String)

    with pytest.raises(QueryError, match="no foreign key"):
        User.select().join(Unrelated)


def test_query_count_and_row_building_with_join(models, monkeypatch):
    _, User, Order, _, _ = models
    calls = []

    def execute(cls, sql, params=None):
        calls.append((sql, params))
        if sql.startswith("SELECT COUNT"):
            return [(3,)]
        return [("A", 4, 12), ("A", None, None)]

    monkeypatch.setattr(User, "execute", classmethod(execute))
    query = User.select(User.name, Order.id.label("order_id"), Order.total).left_join(Order)
    rows = query.all()
    assert rows[0].name == "A" and rows[0].order_id == 4 and rows[0].total == 12
    assert rows[1].order_id is None and rows[1].total is None
    assert rows[0]._fields == ("name", "order_id", "total")
    assert query.count() == 3
    assert calls[-1] == (
        'SELECT COUNT(*) FROM "users" LEFT JOIN "orders" ON "orders"."user_id" = "users"."id"', ()
    )
    with pytest.raises(QueryError, match=r"count\(\) with distinct"):
        query.distinct().count()

    monkeypatch.setattr(User, "max_rows", 1)
    guarded_calls = []

    def guarded_execute(cls, sql, params=None):
        guarded_calls.append((sql, params))
        return [("A", 4, 12), ("B", 5, 20)]

    monkeypatch.setattr(User, "execute", classmethod(guarded_execute))
    guarded = User.select(User.name, Order.id.label("order_id"), Order.total).join(Order)
    with pytest.raises(TooManyRowsError):
        guarded.all()
    assert guarded_calls[-1][1] == (2,) and "LIMIT %s" in guarded_calls[-1][0]
    assert guarded.first().name == "A"


def test_distinct_count_and_joined_injection_remain_parameterized(models, monkeypatch):
    _, User, Order, _, _ = models
    malicious = "x'; DROP TABLE users;--"
    query = User.select().join(Order).where(Order.total == malicious)
    statement = query.to_sql()
    assert malicious not in statement.sql and statement.params == (malicious,)
    with pytest.raises(QueryError, match=r"count\(\) with distinct"):
        User.select().join(Order).distinct().count()
    calls = []
    monkeypatch.setattr(User, "execute", classmethod(
        lambda cls, sql, params=None: calls.append((sql, params)) or [(1, "x", "A", 20)]
    ))
    query.all()
    assert calls[0][1][0] == malicious and malicious not in calls[0][0]
