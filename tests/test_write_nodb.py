import pytest

from pyzzel import Base, Boolean, Column, Integer, String
from pyzzel.errors import ModelDefinitionError, QueryError
from pyzzel.sql import Statement, delete_where, insert_many, update_where
from pyzzel.sql.conditions import Comparison


@pytest.fixture
def models():
    original = Base._registry.copy()
    Base._registry.clear()

    class User(Base):
        __tablename__ = "write_users"
        id = Column(Integer, primary_key=True)
        name = Column(String)
        age = Column(Integer)
        active = Column(Boolean)

    try:
        yield User
    finally:
        Base._registry.clear()
        Base._registry.update(original)


def test_conditional_write_builders_render_exact_parameterized_sql():
    assert update_where(
        "users", {"active": False, "age": 30}, Comparison("age", "<", 18), "version"
    ) == Statement(
        'UPDATE "users" SET "active" = %s, "age" = %s, "version" = "version" + 1 WHERE "age" < %s',
        (False, 30, 18),
    )
    assert delete_where("users", Comparison("age", "<", 18)) == Statement(
        'DELETE FROM "users" WHERE "age" < %s', (18,)
    )


def test_conditional_write_builder_validation_and_parameterization():
    condition = Comparison("age", "<", 18)
    for operation in (
        lambda: update_where("users", {}, condition),
        lambda: update_where("users", {"name": "x"}, None),
        lambda: update_where("users", {"version": 2}, condition),
        lambda: delete_where("users", None),
    ):
        with pytest.raises(ValueError):
            operation()

    with pytest.raises(ValueError):
        update_where("users", {'name"; DROP TABLE users;--': "x"}, condition)
    with pytest.raises(ValueError):
        update_where("users", {"name": "x"}, Comparison('age"--', "<", 18))

    malicious = "'; DROP TABLE users;--"
    statement = update_where("users", {"name": malicious}, condition)
    assert malicious not in statement.sql
    assert statement.params == (malicious, 18)


def test_create_many_validation_and_empty_input(models, monkeypatch):
    User = models
    monkeypatch.setattr(
        User,
        "_run_tx",
        classmethod(lambda cls, fn: pytest.fail("empty create_many touched the database")),
    )
    assert User.create_many([]) == []

    for rows in (
        [{"name": "A"}, {"age": 1}],
        [{"missing": 1}],
        [{"id": 1}],
        [{"version": 1}],
        ["not a dict"],
    ):
        with pytest.raises((QueryError, TypeError)):
            User.create_many(rows)


def test_create_many_uses_multirow_insert_and_preserves_key_order(models, monkeypatch):
    User = models
    calls = []

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))

            class Result:
                def fetchall(self):
                    return [(41,), (42,)]

            return Result()

    monkeypatch.setattr(User, "_run_tx", classmethod(lambda cls, fn: fn(Connection())))
    assert User.create_many(
        [{"name": "A", "age": 10}, {"age": 20, "name": "B"}]
    ) == [41, 42]
    assert calls == [
        (
            'INSERT INTO "write_users" ("name", "age") VALUES (%s, %s), (%s, %s) RETURNING "id"',
            ("A", 10, "B", 20),
        )
    ]


def test_insert_many_builder_parameterizes_values_and_validates_rows():
    statement = insert_many(
        "users", ["name", "age"], [("Amit", 20), ("Rahul", 30)], returning=["id"]
    )
    assert statement.sql == (
        'INSERT INTO "users" ("name", "age") VALUES (%s, %s), (%s, %s) '
        'RETURNING "id"'
    )
    assert statement.params == ("Amit", 20, "Rahul", 30)
    with pytest.raises(ValueError):
        insert_many("users", ["name"], [("A",), ("B", "extra")])


def test_conditional_model_writes_execute_built_statements(models, monkeypatch):
    User = models
    calls = []

    class Result:
        rowcount = 7

    class Connection:
        def execute(self, sql, params):
            calls.append((sql, params))
            return Result()

    monkeypatch.setattr(User, "_run", classmethod(lambda cls, fn: fn(Connection())))
    assert User.update_where(User.age < 18, active=False) == 7
    assert calls[-1] == (
        'UPDATE "write_users" SET "active" = %s, "version" = "version" + 1 WHERE "age" < %s',
        (False, 18),
    )
    assert User.delete_where(User.id.in_({1, 2})) == 7
    assert calls[-1][0] == 'DELETE FROM "write_users" WHERE "id" = ANY(%s)'
    assert set(calls[-1][1][0]) == {1, 2}


def test_write_methods_validate_conditions_and_values(models):
    User = models
    with pytest.raises(QueryError, match="at least one condition"):
        User.update_where(name="x")
    with pytest.raises(TypeError, match="conditions"):
        User.update_where("age < 18", name="x")
    with pytest.raises(QueryError, match="at least one value"):
        User.update_where(User.age < 18)
    for name in ("id", "version", "missing"):
        with pytest.raises(QueryError, match=name):
            User.update_where(User.age < 18, **{name: 1})
    with pytest.raises(QueryError, match="Unknown column"):
        User.update_where(Comparison("missing", "<", 1), name="x")

    with pytest.raises(QueryError, match="at least one condition"):
        User.delete_where()
    with pytest.raises(TypeError, match="conditions"):
        User.delete_where(1)
    with pytest.raises(QueryError, match="Unknown column"):
        User.delete_where(Comparison("missing", "<", 1))


@pytest.mark.parametrize("name", ["create_many", "update_where", "delete_where"])
def test_bulk_write_methods_are_reserved_column_names(name):
    original = Base._registry.copy()
    Base._registry.clear()
    try:
        with pytest.raises(ModelDefinitionError, match=name):
            type(
                "BadModel",
                (Base,),
                {
                    "__tablename__": f"write_reserved_{name}",
                    "id": Column(Integer, primary_key=True),
                    name: Column(String),
                },
            )
    finally:
        Base._registry.clear()
        Base._registry.update(original)
