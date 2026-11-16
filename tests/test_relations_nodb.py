import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import ModelDefinitionError, QueryError, TooManyRowsError
from pyzzel.sql import ColumnSpec, create_index, create_table


@pytest.fixture(autouse=True)
def restore_registry():
    original = Base._registry.copy()
    yield
    Base._registry.clear()
    Base._registry.update(original)


def test_foreign_key_column_validation_and_effective_indexes():
    for bad in ("users", "users.id.more", "users .id", ".id", "users.", 3):
        with pytest.raises(ModelDefinitionError):
            Column(Integer, foreign_key=bad)
    with pytest.raises(ModelDefinitionError):
        Column(Integer, primary_key=True, foreign_key="users.id")
    for action in ("DELETE", "set-null"):
        with pytest.raises(ModelDefinitionError):
            Column(Integer, foreign_key="users.id", on_delete=action)
    with pytest.raises(ModelDefinitionError):
        Column(Integer, on_delete="CASCADE")
    with pytest.raises(ModelDefinitionError):
        Column(Integer, nullable=False, foreign_key="users.id", on_delete="SET NULL")
    assert Column(Integer, foreign_key="users.id", on_delete="cascade").on_delete == "CASCADE"
    assert Column(Integer, foreign_key="users.id").index is True
    assert Column(Integer, foreign_key="users.id", index=False).index is False
    assert Column(Integer, unique=True, index=True).index is False
    assert Column(Integer, primary_key=True, index=True).index is False


def test_exact_foreign_key_ddl_and_indexes():
    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="users.id", on_delete="CASCADE")
        coupon_id = Column(Integer, foreign_key="coupons.id", on_delete="set null")
        region_id = Column(Integer, foreign_key="regions.id")
        total = Column(Numeric(10, 2))
        note = Column(String, index=True)
        code = Column(String(20), unique=True, index=True)

    assert create_table("orders", Order._column_specs()) == (
        'CREATE TABLE IF NOT EXISTS "orders" ('
        '"id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, '
        '"user_id" INTEGER NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE, '
        '"coupon_id" INTEGER REFERENCES "coupons" ("id") ON DELETE SET NULL, '
        '"region_id" INTEGER REFERENCES "regions" ("id"), '
        '"total" NUMERIC(10,2), "note" TEXT, "code" VARCHAR(20) UNIQUE, '
        '"version" INTEGER NOT NULL DEFAULT 1)'
    )
    assert Order._index_statements() == [
        'CREATE INDEX IF NOT EXISTS "ix_orders_user_id" ON "orders" ("user_id")',
        'CREATE INDEX IF NOT EXISTS "ix_orders_coupon_id" ON "orders" ("coupon_id")',
        'CREATE INDEX IF NOT EXISTS "ix_orders_region_id" ON "orders" ("region_id")',
        'CREATE INDEX IF NOT EXISTS "ix_orders_note" ON "orders" ("note")',
    ]
    assert create_index("orders", "user_id") == Order._index_statements()[0]


def test_long_index_name_is_valid_and_deterministic():
    table = "t" * 60
    column = "c" * 60
    sql = create_index(table, column)
    name = sql.split('"')[1]
    assert len(name) <= 63
    assert name.startswith("ix_" + (table + "_" + column)[:40] + "_")
    assert create_index(table, column) == sql


def test_index_and_table_builder_reject_invalid_identifiers_and_actions():
    for bad in ("bad-name", "x" * 64):
        with pytest.raises(ValueError):
            create_index(bad, "col")
        with pytest.raises(ValueError):
            create_index("table", bad)
    with pytest.raises(ValueError):
        create_table("orders", [
            ColumnSpec("id", "INTEGER", primary_key=True, foreign_key=("users", "id"), on_delete="DROP"),
        ])
    with pytest.raises(ValueError):
        create_table("orders", [
            ColumnSpec("id", "INTEGER", primary_key=True),
            ColumnSpec("ref", "INTEGER", foreign_key=("bad-name", "id")),
        ])


def test_table_ordering_and_reference_errors():
    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)

    assert [model.__tablename__ for model in Base._ordered_models()] == ["users", "orders"]

    class SelfRef(Base):
        __tablename__ = "self_ref"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="self_ref.id")

    assert [model.__tablename__ for model in Base._ordered_models()] == ["users", "orders", "self_ref"]

    class MissingTable(Base):
        __tablename__ = "missing_table_child"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="unknown.id")

    with pytest.raises(ModelDefinitionError, match="missing_table_child.parent_id.*unknown"):
        Base._ordered_models()


def test_table_ordering_rejects_bad_columns_and_cycles():
    class Parent(Base):
        __tablename__ = "parent"
        id = Column(Integer, primary_key=True)
        label = Column(String)

    class BadColumn(Base):
        __tablename__ = "bad_column"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="parent.missing")

    with pytest.raises(ModelDefinitionError, match="bad_column.parent_id.*parent.missing"):
        Base._ordered_models()

    Base._registry.pop("bad_column")

    class NonUnique(Base):
        __tablename__ = "non_unique"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="parent.label")

    with pytest.raises(ModelDefinitionError, match="non_unique.parent_id.*parent.label"):
        Base._ordered_models()

    Base._registry.pop("non_unique")

    class First(Base):
        __tablename__ = "first"
        id = Column(Integer, primary_key=True)
        second_id = Column(Integer, foreign_key="second.id")

    class Second(Base):
        __tablename__ = "second"
        id = Column(Integer, primary_key=True)
        first_id = Column(Integer, foreign_key="first.id")

    with pytest.raises(ModelDefinitionError, match="Circular.*first.*second"):
        Base._ordered_models()


def test_with_validation_immutability_and_main_sql_only():
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    original = User.select()
    loaded = original.with_(orders=Order.user_id)
    assert loaded is not original and original.relations == ()
    assert loaded.with_(again=Order.user_id).relations[1][0] == "again"
    assert 'FROM "users"' in loaded.to_sql().sql and '"orders"' not in loaded.to_sql().sql
    for value in (User.name, 3):
        with pytest.raises(QueryError):
            original.with_(bad=value)
    with pytest.raises(QueryError, match="conflicts"):
        User.select(User.name).with_(name=Order.user_id)
    with pytest.raises(QueryError, match="Add 'id'"):
        User.select(User.name).with_(orders=Order.user_id)
    with pytest.raises(QueryError, match="conflicts"):
        loaded.with_(orders=Order.user_id)

    class Other(Base):
        __tablename__ = "other"
        id = Column(Integer, primary_key=True)

    with pytest.raises(QueryError, match="does not link"):
        Other.select().with_(orders=Order.user_id)


def test_with_one_to_many_groups_and_uses_guarded_related_query(monkeypatch):
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")
        total = Column(Numeric)

    calls = []

    def execute(cls, sql, params=None):
        calls.append((sql, params))
        if 'FROM "users"' in sql:
            return [(1, "one"), (2, "two")]
        return [(3, 1, 10), (4, 1, 11)]

    monkeypatch.setattr(User, "execute", classmethod(execute))
    monkeypatch.setattr(Order, "execute", classmethod(execute))
    rows = User.select().with_(orders=Order.user_id).all()
    assert [row.id for row in rows] == [1, 2]
    assert [order.id for order in rows[0].orders] == [3, 4]
    assert rows[1].orders == []
    assert rows[0].orders[0]._fields == ("id", "user_id", "total")
    assert calls[1] == (
        'SELECT "id", "user_id", "total" FROM "orders" WHERE "user_id" = ANY(%s) '
        'ORDER BY "id" ASC LIMIT %s',
        ([1, 2], 10_001),
    )


def test_with_no_keys_and_many_to_one(monkeypatch):
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        name = Column(String)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=True, foreign_key="users.id")

    calls = []

    def user_execute(cls, sql, params=None):
        calls.append((sql, params))
        return [(1, "one")]

    def order_execute(cls, sql, params=None):
        calls.append((sql, params))
        return [(1, 1), (2, None)]

    monkeypatch.setattr(User, "execute", classmethod(user_execute))
    monkeypatch.setattr(Order, "execute", classmethod(order_execute))
    got = Order.select().with_(user=Order.user_id).all()
    assert got[0].user.name == "one" and got[1].user is None
    assert calls[1][1] == ([1], 10_001)

    calls.clear()
    monkeypatch.setattr(User, "execute", classmethod(lambda cls, sql, params=None: calls.append((sql, params)) or []))
    empty = User.select().with_(orders=Order.user_id).all()
    assert empty == [] and len(calls) == 1


def test_with_rejects_unknown_owner_parent_missing_keys_and_self_reference():
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    with pytest.raises(QueryError, match="exactly one model owning"):
        User.select().with_(missing=Column(Integer, foreign_key="users.id"))

    class UnknownParent(Base):
        __tablename__ = "unknown_parent_child"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="unregistered.id")

    with pytest.raises(QueryError, match="not registered"):
        UnknownParent.select().with_(parent=UnknownParent.parent_id)
    with pytest.raises(QueryError, match="Add 'user_id'"):
        Order.select(Order.id).with_(user=Order.user_id)

    class Node(Base):
        __tablename__ = "nodes"
        id = Column(Integer, primary_key=True)
        parent_id = Column(Integer, foreign_key="nodes.id")

    with pytest.raises(QueryError, match="self-referencing"):
        Node.select().with_(parent=Node.parent_id)


def test_with_deduplicates_keys_and_two_relations_use_three_queries(monkeypatch):
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    class Comment(Base):
        __tablename__ = "comments"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    calls = []

    def execute(cls, sql, params=None):
        calls.append((sql, params))
        if 'FROM "users"' in sql:
            return [(1,), (1,)]
        if 'FROM "orders"' in sql:
            return [(5, 1)]
        return [(7, 1)]

    for model in (User, Order, Comment):
        monkeypatch.setattr(model, "execute", classmethod(execute))
    rows = User.select().with_(orders=Order.user_id, comments=Comment.user_id).all()
    assert len(rows) == 2 and len(calls) == 3
    assert calls[1][1] == ([1], 10_001)
    assert calls[2][1] == ([1], 10_001)
    assert [row.id for row in rows[0].orders] == [5]
    assert [row.id for row in rows[0].comments] == [7]


def test_with_child_guard_count_and_first(monkeypatch):
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, foreign_key="users.id")

    monkeypatch.setattr(Order, "max_rows", 2)
    monkeypatch.setattr(User, "execute", classmethod(lambda cls, sql, params=None: [(1,)]))
    monkeypatch.setattr(Order, "execute", classmethod(lambda cls, sql, params=None: [(i, 1) for i in range(3)]))
    with pytest.raises(TooManyRowsError):
        User.select().with_(orders=Order.user_id).all()
    monkeypatch.setattr(Order, "max_rows", 10)
    result = User.select().with_(orders=Order.user_id).first()
    assert len(result.orders) == 3
    assert User.select().with_(orders=Order.user_id).count() == 1
