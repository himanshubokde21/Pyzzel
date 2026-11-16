import pytest

from pyzzel.errors import MigrationError
from pyzzel.migrate.diff import (
    AddColumn, AddForeignKey, AddIndex, AddUnique, AlterType, CreateTable,
    DropColumn, DropDefault, DropForeignKey, DropIndex, DropNotNull,
    DropTable, DropUnique, SetDefault, SetNotNull, diff,
)


def snap(tables):
    return {"version": 1, "tables": tables}


def table(columns=None, indexes=None):
    return {"columns": columns or {}, "indexes": indexes or []}


def col(**kw):
    return {"type": "integer", "nullable": True, "primary_key": False, "identity": False,
            "unique": False, "default": None, "references": None, "on_delete": None, **kw}


def test_identical_empty_and_every_operation():
    assert diff(snap({}), snap({})) == []
    old = snap({"t": table({"id": col(), "gone": col(unique=True, references=["p", "id"], on_delete="CASCADE"),
                                 "kept": col(unique=True, references=["p", "id"], on_delete="CASCADE")}, ["gone", "kept"])})
    new = snap({"t": table({
        "id": col(type="bigint", nullable=False, default="1"),
        "kept": col(references=["p", "id"], on_delete="RESTRICT"),
        "added": col(default="2"),
    }, ["id"])})
    got = diff(old, new)
    assert got == [
        DropForeignKey("t", "kept"), DropUnique("t", "kept"), DropIndex("t", "kept"),
        DropColumn("t", "gone"), AddColumn("t", "added", new["tables"]["t"]["columns"]["added"]),
        AlterType("t", "id", "integer", "bigint"), SetDefault("t", "id", "1"),
        SetNotNull("t", "id"), AddForeignKey("t", "kept", ["p", "id"], "RESTRICT"),
        AddIndex("t", "id"),
    ]
    operations = diff(snap({"old": table({"x": col()})}), snap({"new": table({"x": col()})}))
    assert operations[0] == CreateTable("new", {"x": col()}, [])
    assert operations[-1] == DropTable("old")


def test_drop_defaults_nulls_and_renames():
    old = snap({"t": table({"before": col(nullable=False, default="1"), "x": col()})})
    new = snap({"t": table({"after": col(), "x": col(nullable=False, default="2")})})
    ops = diff(old, new)
    assert ops == [DropColumn("t", "before"), AddColumn("t", "after", col()),
                   SetDefault("t", "x", "2"), SetNotNull("t", "x")]
    assert diff(snap({"t": table({"x": col(nullable=False, default="1")})}), snap({"t": table({"x": col()})})) == [DropDefault("t", "x"), DropNotNull("t", "x")]
    assert diff(snap({"t": table({"x": col(default="'active'")})}), snap({"t": table({"x": col(default="'active'::text")})})) == []


def test_new_table_dependency_order_and_self_reference():
    parent = table({"id": col(primary_key=True)})
    child = table({"id": col(primary_key=True), "parent_id": col(references=["parents", "id"])})
    assert [op.table for op in diff(snap({}), snap({"children": child, "parents": parent})) if isinstance(op, CreateTable)] == ["parents", "children"]
    self_ref = table({"id": col(primary_key=True), "parent_id": col(references=["self", "id"])})
    assert len(diff(snap({}), snap({"self": self_ref}))) == 1
    a = table({"id": col(primary_key=True), "b_id": col(references=["b", "id"])})
    b = table({"id": col(primary_key=True), "a_id": col(references=["a", "id"])})
    with pytest.raises(MigrationError):
        diff(snap({}), snap({"a": a, "b": b}))
    with pytest.raises(MigrationError):
        diff(snap({"t": table({"id": col()})}), snap({"t": table({"id": col(primary_key=True)})}))
    with pytest.raises(MigrationError):
        diff(snap({"t": table({"id": col(primary_key=True)})}), snap({"t": table({"id": col(primary_key=True), "next": col(primary_key=True)})}))
    with pytest.raises(MigrationError):
        diff(snap({"t": table({"id": col()})}), snap({"t": table({"id": col(identity=True)})}))


def test_drop_table_does_not_emit_column_operations():
    ops = diff(snap({"child": table({"id": col(primary_key=True), "x": col(unique=True)}, ["x"])}), snap({}))
    assert ops == [DropTable("child")]
