import json

import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import MigrationError
from pyzzel.migrate import create, generate
from pyzzel.migrate.diff import (
    AddColumn, AddForeignKey, AddIndex, AddUnique, AlterType, DropColumn,
    DropDefault, DropForeignKey, DropIndex, DropNotNull, DropTable,
    DropUnique, SetDefault, SetNotNull,
)
from pyzzel.migrate.sqlgen import render
from pyzzel.sql import ColumnSpec, column_definition, create_table, index_name


@pytest.fixture
def registry():
    old = Base._registry.copy()
    Base._registry.clear()
    yield
    Base._registry.clear()
    Base._registry.update(old)


def define_initial():
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)

    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="users.id", on_delete="CASCADE")
        total = Column(Numeric(10, 2), server_default="0")
        note = Column(String, index=True)
        code = Column(String(20), unique=True)
    return User, Order


def test_builder_refactors_preserve_exact_output():
    spec = ColumnSpec("x", "INTEGER", nullable=False, default="1")
    assert column_definition(spec) == '"x" INTEGER NOT NULL DEFAULT 1'
    assert create_table("t", [ColumnSpec("id", "INTEGER", primary_key=True)]) == 'CREATE TABLE IF NOT EXISTS "t" ("id" INTEGER PRIMARY KEY)'
    assert create_table("t", [spec, ColumnSpec("id", "INTEGER", primary_key=True)]) == 'CREATE TABLE IF NOT EXISTS "t" ("x" INTEGER NOT NULL DEFAULT 1, "id" INTEGER PRIMARY KEY)'
    assert index_name("orders", "note") == "ix_orders_note"
    long = index_name("t" * 40, "c" * 40)
    assert len(long) <= 63 and long.startswith("ix_")


def test_render_statement_coverage_and_safety():
    ops = [
        DropTable("t"), AddColumn("t", "x", {"type": "integer", "nullable": False, "default": None}),
        DropColumn("t", "x"), AlterType("t", "x", "integer", "numeric(10,2)"),
        SetNotNull("t", "x"), DropNotNull("t", "x"), SetDefault("t", "x", "0"),
        DropDefault("t", "x"), AddUnique("t", "x"), DropUnique("t", "x"),
        AddForeignKey("t", "x", ["p", "id"], "NO ACTION"),
        AddForeignKey("t", "y", ["p", "id"], "CASCADE"), DropForeignKey("t", "x"),
        AddIndex("t", "x"), DropIndex("t", "x"),
    ]
    sql = render(ops)
    for expected in (
        "-- pyzzel:destructive\nDROP TABLE IF EXISTS \"t\";",
        "-- pyzzel:warning NOT NULL column without a default fails if the table already has rows\nALTER TABLE \"t\" ADD COLUMN IF NOT EXISTS \"x\" INTEGER NOT NULL;",
        "-- pyzzel:warning fails if the column contains NULL values\nALTER TABLE \"t\" ALTER COLUMN \"x\" SET NOT NULL;",
        'REFERENCES "p" ("id");', 'REFERENCES "p" ("id") ON DELETE CASCADE;',
    ):
        assert expected in sql
    assert "TYPE NUMERIC(10,2);" in sql and "SET DEFAULT 0;" in sql
    with pytest.raises(ValueError):
        render([SetDefault("t", "x", "0; DROP TABLE t")])
    with pytest.raises(ValueError):
        render([DropTable('bad"name')])
    assert len(render([AddUnique("t" * 40, "c" * 40)]).split('"')[1]) <= 63


def test_exact_generate_files_and_second_diff(registry, tmp_path):
    _, Order = define_initial()
    first = generate("init", tmp_path)
    assert first.name == "0001_init.sql"
    expected_first = '''-- pyzzel migration 0001_init

CREATE TABLE IF NOT EXISTS "users" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "name" CHARACTER VARYING(50) NOT NULL, "version" INTEGER NOT NULL DEFAULT 1);

CREATE TABLE IF NOT EXISTS "orders" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "user_id" INTEGER NOT NULL REFERENCES "users" ("id") ON DELETE CASCADE, "total" NUMERIC(10,2) DEFAULT 0, "note" TEXT, "code" CHARACTER VARYING(20) UNIQUE, "version" INTEGER NOT NULL DEFAULT 1);

CREATE INDEX IF NOT EXISTS "ix_orders_user_id" ON "orders" ("user_id");

CREATE INDEX IF NOT EXISTS "ix_orders_note" ON "orders" ("note");
'''
    assert first.read_text() == expected_first
    saved = json.loads((tmp_path / "snapshot.json").read_text())
    assert saved["tables"]["orders"]["indexes"] == ["user_id", "note"]
    Base._registry.clear()
    class User(Base):
        __tablename__ = "users"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)
    class Order(Base):
        __tablename__ = "orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="users.id", on_delete="CASCADE")
        total = Column(Numeric(12, 2), server_default="0")
        status = Column(String(10), nullable=False, server_default="'new'")
        code = Column(String(20))
    class Coupon(Base):
        __tablename__ = "coupons"
        id = Column(Integer, primary_key=True)
        code = Column(String(20), unique=True)
    second = generate("changes", tmp_path)
    expected_second = '''-- pyzzel migration 0002_changes

ALTER TABLE "orders" DROP CONSTRAINT IF EXISTS "orders_code_key";

-- pyzzel:destructive
ALTER TABLE "orders" DROP COLUMN IF EXISTS "note";

CREATE TABLE IF NOT EXISTS "coupons" ("id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, "code" CHARACTER VARYING(20) UNIQUE, "version" INTEGER NOT NULL DEFAULT 1);

ALTER TABLE "orders" ADD COLUMN IF NOT EXISTS "status" CHARACTER VARYING(10) NOT NULL DEFAULT 'new';

-- pyzzel:destructive
ALTER TABLE "orders" ALTER COLUMN "total" TYPE NUMERIC(12,2);
'''
    assert second.read_text() == expected_second
    assert generate("unchanged", tmp_path) is None
    assert sorted(path.name for path in tmp_path.glob("*.sql")) == ["0001_init.sql", "0002_changes.sql"]


def test_numbering_create_validation_and_failed_write(registry, tmp_path, monkeypatch):
    define_initial()
    (tmp_path / "0001_old.sql").touch()
    (tmp_path / "0003_gap.sql").touch()
    blank = create("manual", tmp_path)
    assert blank.name == "0004_manual.sql"
    assert blank.read_text() == "-- pyzzel migration 0004_manual\n"
    for name in ("", "A", "bad-name", "x" * 52):
        with pytest.raises(MigrationError):
            create(name, tmp_path)
    existing = tmp_path / "0004_manual.sql"
    from pathlib import Path
    original_iterdir = Path.iterdir
    monkeypatch.setattr(Path, "iterdir", lambda path: (entry for entry in original_iterdir(path) if entry != existing))
    with pytest.raises(MigrationError):
        create("manual", tmp_path)
    monkeypatch.setattr(Path, "iterdir", original_iterdir)

    other = tmp_path / "failure"
    original = (other / "snapshot.json")
    original.parent.mkdir()
    original.write_text('{"version": 1, "tables": {}}\n')
    def fail_write(*args, **kwargs):
        raise OSError("write failed")
    monkeypatch.setattr(Path, "write_text", fail_write)
    with pytest.raises(OSError):
        generate("init", other)
    assert original.read_text() == '{"version": 1, "tables": {}}\n'
