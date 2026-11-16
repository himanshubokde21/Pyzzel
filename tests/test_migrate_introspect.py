import subprocess
import sys

import pytest

from pyzzel import Base
from pyzzel.errors import MigrationError
from pyzzel.migrate.compare import check
from pyzzel.migrate.diff import (
    AddColumn, AddForeignKey, AddIndex, AddUnique, AlterType, CreateTable,
    DropColumn, DropDefault, DropForeignKey, DropIndex, DropNotNull,
    DropTable, DropUnique, SetDefault, SetNotNull, describe, diff,
)
from pyzzel.migrate.introspect import build_snapshot
from pyzzel.migrate.schema import normalize_default
from pyzzel.migrate.__main__ import main


def test_build_snapshot_basic_and_filtering():
    catalog = {
        "tables": [(2, "z_users"), (1, "a_orders"), (3, "skip_me")],
        "columns": [
            (2, 1, "id", "integer", True, "a", None),
            (2, 2, "name", "character varying(30)", True, "", "'Rahul'::character varying"),
            (1, 1, "id", "integer", True, "d", None),
            (1, 2, "user_id", "integer", True, "", None),
            (1, 3, "code", "text", False, "", None),
            (3, 1, "id", "integer", True, "", None),
        ],
        "constraints": [
            (2, "users_pk", "p", [1], None, [], "a"),
            (1, "orders_pk", "p", [1], None, [], "a"),
            (1, "orders_user_fk", "f", [2], "z_users", ["id"], "c"),
            (1, "orders_code_uq", "u", [3], None, [], "a"),
        ],
        "indexes": [(1, "orders_user_id_idx", False, False, False, False, 1, [2], "btree", False),
                    (1, "orders_code_uq_idx", False, True, False, False, 1, [3], "btree", True)],
    }
    snapshot, warnings = build_snapshot(catalog, exclude=("skip_me",))
    assert warnings == []
    assert list(snapshot["tables"]) == ["a_orders", "z_users"]
    assert snapshot["tables"]["z_users"]["columns"]["id"]["identity"] is True
    assert snapshot["tables"]["z_users"]["columns"]["id"]["nullable"] is False
    assert snapshot["tables"]["a_orders"]["columns"]["id"]["identity"] is True
    assert snapshot["tables"]["a_orders"]["columns"]["user_id"]["references"] == ["z_users", "id"]
    assert snapshot["tables"]["a_orders"]["columns"]["user_id"]["on_delete"] == "CASCADE"
    assert snapshot["tables"]["a_orders"]["columns"]["code"]["unique"] is True
    assert snapshot["tables"]["a_orders"]["indexes"] == ["user_id"]
    assert build_snapshot(catalog, ignore=("a_orders", "skip_me"))[0]["tables"].keys() == {"z_users"}


@pytest.mark.parametrize("kind,label", [("p", "primary key"), ("u", "unique"), ("f", "foreign key")])
def test_composite_constraints_warn(kind, label):
    c = {"tables": [(1, "t")], "columns": [(1, 1, "a", "integer", True, "", None)],
         "constraints": [(1, "multi", kind, [1, 2], "p", ["x", "y"], "a")], "indexes": []}
    assert build_snapshot(c)[1] == [f"composite {label} on t is not supported"]


def test_unsupported_types_and_ignored_index_kinds_warn_once_each():
    columns = [(1, n, f"c{n}", "integer", False, "", None) for n in range(1, 8)]
    columns.append((1, 8, "arr", "integer[]", False, "", None))
    indexes = [
        (1, "multi", False, False, False, False, 2, [1, 2], "btree", False),
        (1, "partial", False, False, True, False, 1, [3], "btree", False),
        (1, "expression", False, False, False, True, 1, [0], "btree", False),
        (1, "unique_plain", False, True, False, False, 1, [4], "btree", False),
        (1, "hash_plain", False, False, False, False, 1, [5], "hash", False),
        (1, "constraint", False, True, False, False, 1, [6], "btree", True),
        (1, "primary", True, True, False, False, 1, [7], "btree", True),
    ]
    snapshot, warnings = build_snapshot({"tables": [(1, "t")], "columns": columns,
                                         "constraints": [], "indexes": indexes})
    assert warnings == ["unsupported type integer[] on t.arr"] + [
        f"ignored index {name} on t" for name in ("multi", "partial", "expression", "unique_plain", "hash_plain")]
    assert snapshot["tables"]["t"]["indexes"] == []


def test_every_describe_format():
    classes = [
        (CreateTable("t", {}, []), "table t: only in new"), (DropTable("t"), "table t: only in old"),
        (AddColumn("t", "c", {}), "column t.c: only in new"), (DropColumn("t", "c"), "column t.c: only in old"),
        (AlterType("t", "c", "integer", "bigint"), "column t.c: type differs (old integer, new bigint)"),
        (SetNotNull("t", "c"), "column t.c: NOT NULL in new but nullable in old"),
        (DropNotNull("t", "c"), "column t.c: nullable in new but NOT NULL in old"),
        (SetDefault("t", "c", "0"), "column t.c: default differs (new has 0)"),
        (DropDefault("t", "c"), "column t.c: default only in old"),
        (AddUnique("t", "c"), "column t.c: unique only in new"), (DropUnique("t", "c"), "column t.c: unique only in old"),
        (AddForeignKey("t", "c", ["p", "id"], "CASCADE"), "column t.c: foreign key to p.id (ON DELETE CASCADE) only in new"),
        (DropForeignKey("t", "c"), "column t.c: foreign key only in old"),
        (AddIndex("t", "c"), "column t.c: index only in new"), (DropIndex("t", "c"), "column t.c: index only in old"),
    ]
    assert [describe(op, "old", "new") for op, _ in classes] == [text for _, text in classes]


def test_changed_foreign_key_has_drop_then_add():
    col = {"type": "integer", "nullable": False, "primary_key": False, "identity": False,
           "unique": False, "default": None, "references": ["parent", "id"], "on_delete": "CASCADE"}
    new = {**col, "on_delete": "RESTRICT"}
    ops = diff({"version": 1, "tables": {"child": {"columns": {"parent_id": col}, "indexes": []}}},
               {"version": 1, "tables": {"child": {"columns": {"parent_id": new}, "indexes": []}}})
    assert [describe(op, "old", "new") for op in ops] == [
        "column child.parent_id: foreign key only in old",
        "column child.parent_id: foreign key to parent.id (ON DELETE RESTRICT) only in new",
    ]


def test_default_numeric_normalization():
    for source, expected in [("'-1'", "-1"), ("'0'", "0"), ("'1.5'", "1.5"),
                             ("(-1)", "-1"), ("'-1'::integer", "-1")]:
        assert normalize_default(source) == expected


def test_imports_stay_driver_free_and_core_does_not_import_migrations():
    code = ("import sys, pyzzel; assert 'pyzzel.migrate' not in sys.modules; "
            "import pyzzel.migrate.introspect, pyzzel.migrate.compare; "
            "assert 'psycopg' not in sys.modules and 'psycopg_pool' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True)


def test_cli_requires_models_and_database_url(monkeypatch, capsys):
    monkeypatch.delenv("PYZZEL_DATABASE_URL", raising=False)
    assert main(["check"]) == 1
    assert "--models" in capsys.readouterr().err
    assert main(["check", "--models", "pyzzel"]) == 1
    assert "Database URL required" in capsys.readouterr().err
    assert main(["pull"]) == 1
    assert "Database URL required" in capsys.readouterr().err
    with pytest.raises(SystemExit) as error:
        main(["pull", "--unknown"])
    assert error.value.code == 2


def test_check_requires_registered_models():
    registry = Base._registry.copy()
    Base._registry.clear()
    try:
        with pytest.raises(MigrationError, match="No models are registered"):
            check()
    finally:
        Base._registry.update(registry)
