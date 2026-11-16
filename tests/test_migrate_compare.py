import os

import pytest

from pyzzel import Base, Column, Integer, String
from pyzzel.migrate import check, generate, pull
from pyzzel.migrate.introspect import schema_from_database
from pyzzel.migrate.schema import schema_from_models
from pyzzel.migrate.diff import diff


pytestmark = pytest.mark.skipif(
    not os.environ.get("PYZZEL_TEST_DATABASE_URL"),
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)


def test_pull_round_trip_and_model_check(database, prepare_test_table, tmp_path):
    table = "pyzzel_test_pull_users"
    history = "pyzzel_test_migrations"
    prepare_test_table(table)
    prepare_test_table(history)

    class User(Base):
        __tablename__ = table
        id = Column(Integer, primary_key=True)
        name = Column(String(40), nullable=False, server_default="'active'")

    Base.create_tables()
    other_tables = [row[0] for row in Base.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind='r' AND c.relname<>%s",
        (table,),
    )]
    first = pull(tmp_path, history_table=history, ignore=other_tables)
    assert any("only in database" in line for line in first.changes)
    assert not (tmp_path / "snapshot.json").exists()
    saved = pull(tmp_path, write=True, history_table=history, ignore=other_tables)
    assert saved.written is True
    assert not saved.warnings
    actual = schema_from_database(ignore=other_tables, exclude=(history,))[0]
    assert diff(schema_from_models(), actual) == []
    report = check(tmp_path, history_table=history, ignore=other_tables)
    assert report.ok and report.drift_checked and report.warnings == []


def test_migration_status_blocks_drift_until_applied(database, prepare_test_table, tmp_path):
    table = "pyzzel_test_pending"
    history = "pyzzel_test_migrations"
    prepare_test_table(table)
    prepare_test_table(history)

    class Pending(Base):
        __tablename__ = table
        id = Column(Integer, primary_key=True)
        value = Column(String)

    generate("initial", tmp_path)
    report = check(tmp_path, history_table=history)
    assert report.migration_problems == ["0001_initial.sql: not applied yet"]
    assert report.drift_checked is False
    assert report.warnings == ["drift not checked: apply or fix the migration files first"]

# Phase 13D real-database comparisons.
import os
from pathlib import Path

from pyzzel import (
    BigInteger, Boolean, Date, DateTime, Float, JSON, LargeBinary, Numeric,
    SmallInteger, Time, UUID, connect, disconnect,
)
from pyzzel.migrate import apply, generate, pull
from pyzzel.migrate.diff import describe
from pyzzel.migrate.introspect import schema_from_database
from pyzzel.migrate.schema import load_snapshot, normalize_default, schema_from_models

DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
HISTORY = "pyzzel_test_migrations"
TABLE = "pyzzel_test_compare"
PARENT = "pyzzel_test_compare_parent"

def _prepare(prepare_test_table, *tables):
    for name in tables:
        prepare_test_table(name)


def _model(table, fields):
    attrs = {"__tablename__": table, **fields}
    return type("TestModel", (Base,), attrs)


def _parent():
    return _model(PARENT, {"id": Column(Integer, primary_key=True), "name": Column(String)})


def _baseline():
    _parent()
    return _model(TABLE, {
        "id": Column(Integer, primary_key=True),
        "small": Column(SmallInteger, nullable=False),
        "integer_value": Column(Integer, server_default="0"),
        "big": Column(BigInteger),
        "floating": Column(Float),
        "amount": Column(Numeric(10, 2)),
        "plain_text": Column(String),
        "bounded_text": Column(String(50)),
        "flag": Column(Boolean),
        "day": Column(Date),
        "clock": Column(Time),
        "aware": Column(DateTime),
        "naive": Column(DateTime(timezone=False)),
        "token": Column(UUID),
        "payload": Column(JSON, server_default="'{}'"),
        "binary": Column(LargeBinary),
        "parent_no_action": Column(Integer, foreign_key=f"{PARENT}.id", on_delete="NO ACTION"),
        "parent_restrict": Column(Integer, foreign_key=f"{PARENT}.id", on_delete="RESTRICT"),
        "parent_cascade": Column(Integer, foreign_key=f"{PARENT}.id", on_delete="CASCADE"),
        "parent_set_null": Column(Integer, foreign_key=f"{PARENT}.id", on_delete="SET NULL"),
        "self_id": Column(Integer, foreign_key=f"{TABLE}.id", on_delete="CASCADE"),
        "unique_value": Column(String, unique=True),
        "indexed_value": Column(String, index=True),
        "unindexed_fk": Column(Integer, foreign_key=f"{PARENT}.id", index=False),
        "nullable_value": Column(String, nullable=True),
        "required_value": Column(String, nullable=False),
        "default_now": Column(DateTime, server_default="now()"),
        "default_true": Column(Boolean, server_default="TRUE"),
        "default_active": Column(String, server_default="'active'"),
        "default_negative": Column(Integer, server_default="-1"),
    })


def _write(path, name, sql):
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    target = path / name
    target.write_text(sql, encoding="utf-8", newline="\n")
    return target


def _applied(directory, history=HISTORY):
    assert generate("initial", directory)
    assert apply(directory, allow_destructive=True, history_table=history) == ["0001_initial.sql"]


def _constraint_name(table, kind, column=None):
    rows = Base.execute(
        "SELECT co.conname, a.attname FROM pg_catalog.pg_constraint co "
        "LEFT JOIN pg_catalog.pg_attribute a ON a.attrelid=co.conrelid "
        "AND co.conkey=array[a.attnum] "
        "WHERE co.conrelid=%s::regclass AND co.contype=%s",
        (table, kind),
    )
    if column is not None:
        rows = [row for row in rows if row[1] == column]
    assert len(rows) == 1
    return rows[0][0]


def _index_name(table, column):
    rows = Base.execute(
        "SELECT i.relname FROM pg_catalog.pg_index x "
        "JOIN pg_catalog.pg_class t ON t.oid=x.indrelid "
        "JOIN pg_catalog.pg_class i ON i.oid=x.indexrelid "
        "JOIN pg_catalog.pg_namespace n ON n.oid=t.relnamespace "
        "WHERE n.nspname=current_schema() AND t.relname=%s "
        "AND NOT x.indisprimary AND NOT EXISTS "
        "(SELECT 1 FROM pg_catalog.pg_constraint c WHERE c.conindid=i.oid) "
        "AND x.indpred IS NULL AND x.indexprs IS NULL "
        "AND x.indnkeyatts=1 AND x.indkey[0]=("
        "SELECT a.attnum FROM pg_catalog.pg_attribute a "
        "WHERE a.attrelid=t.oid AND a.attname=%s)",
        (table, column),
    )
    assert len(rows) == 1
    return rows[0][0]



def _ignored_existing_test_tables(*keep):
    disconnect()
    connect(DATABASE_URL)
    rows = Base.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind='r' "
        "AND left(c.relname, 12) = 'pyzzel_test_'"
    )
    return tuple(name for (name,) in rows if name not in set(keep) | {HISTORY})


def _check(directory, *keep, history_table=HISTORY):
    return check(directory, ignore=_ignored_existing_test_tables(*keep), history_table=history_table)


def test_database_round_trip_all_column_types_defaults_and_relations(
    database, prepare_test_table
):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE)
    _baseline()
    Base.create_tables()
    extras = [row[0] for row in Base.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind='r' "
        "AND c.relname NOT IN (%s,%s,%s)",
        (HISTORY, PARENT, TABLE),
    )]
    actual, warnings = schema_from_database(ignore=extras)
    assert warnings == []
    assert diff(schema_from_models(), actual) == []
    columns = actual["tables"][TABLE]["columns"]
    assert columns["small"]["type"] == "smallint"
    assert columns["integer_value"]["default"] == "0"
    assert columns["amount"]["type"] == "numeric(10,2)"
    assert columns["plain_text"]["type"] == "text"
    assert columns["bounded_text"]["type"] == "character varying(50)"
    assert columns["aware"]["type"] == "timestamp with time zone"
    assert columns["naive"]["type"] == "timestamp without time zone"
    assert columns["default_now"]["default"] == "now()"
    assert columns["default_true"]["default"].lower() == "true"
    assert normalize_default(columns["default_active"]["default"]) == "'active'"
    assert normalize_default(columns["default_negative"]["default"]) == "-1"
    assert normalize_default(columns["payload"]["default"]) == "'{}'"
    assert columns["parent_set_null"]["on_delete"] == "SET NULL"
    assert columns["unindexed_fk"]["references"] == [PARENT, "id"]
    assert "unindexed_fk" not in actual["tables"][TABLE]["indexes"]


def test_generated_migration_round_trip_checks_clean(database, prepare_test_table, tmp_path):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE)
    _baseline()
    _applied(tmp_path)
    report = _check(tmp_path, TABLE, PARENT, history_table=HISTORY)
    assert report.ok is True
    assert report.drift_checked is True
    assert report.pending_model_changes == []
    assert report.migration_problems == []
    assert report.drift == []
    assert report.warnings == []


def test_pending_model_change_reports_exact_lines_then_regenerates(
    database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, TABLE)
    class Before(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
        name = Column(String)
    assert generate("initial", tmp_path)
    apply(tmp_path, history_table=HISTORY)
    Base._registry.clear()
    class After(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
        name = Column(String)
        age = Column(Integer)
    report = _check(tmp_path, TABLE, PARENT, history_table=HISTORY)
    expected = [describe(op, "snapshot", "models") for op in diff(
        load_snapshot(tmp_path / "snapshot.json"), schema_from_models()
    )]
    assert expected == [f"column {TABLE}.age: only in models"]
    assert report.pending_model_changes == expected
    assert report.ok is False
    assert generate("add_age", tmp_path)
    assert apply(tmp_path, allow_destructive=True, history_table=HISTORY) == ["0002_add_age.sql"]
    report = _check(tmp_path, TABLE, PARENT, history_table=HISTORY)
    assert report.ok is True and report.drift_checked is True


@pytest.mark.parametrize("state", ["unapplied", "edited", "missing"])
def test_migration_file_problems_skip_drift_with_clear_warning(
    state, database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, TABLE)
    class Item(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
        value = Column(String)
    if state == "unapplied":
        generate("initial", tmp_path)
        expected = "0001_initial.sql: not applied yet"
    else:
        _applied(tmp_path)
        path = tmp_path / "0001_initial.sql"
        if state == "edited":
            path.write_text(path.read_text(encoding="utf-8") + "-- edited\n", encoding="utf-8")
            expected = "0001_initial.sql: edited after it was applied"
        else:
            path.unlink()
            expected = "0001_initial.sql: applied but the file is missing"
    report = _check(tmp_path, TABLE, PARENT, history_table=HISTORY)
    assert report.migration_problems == [expected]
    assert report.drift_checked is False
    assert report.warnings == ["drift not checked: apply or fix the migration files first"]


@pytest.mark.parametrize(
    "case,expected",
    [
        ("add_column", f"column {TABLE}.extra: only in database"),
        ("drop_column", f"column {TABLE}.nullable_value: only in snapshot"),
        ("alter_type", f"column {TABLE}.integer_value: type differs (snapshot integer, database bigint)"),
        ("drop_not_null", f"column {TABLE}.required_value: nullable in database but NOT NULL in snapshot"),
        ("set_not_null", f"column {TABLE}.nullable_value: NOT NULL in database but nullable in snapshot"),
        ("set_default", f"column {TABLE}.nullable_value: default differs (database has 5)"),
        ("drop_default", f"column {TABLE}.integer_value: default only in snapshot"),
        ("add_unique", f"column {TABLE}.nullable_value: unique only in database"),
        ("drop_unique", f"column {TABLE}.unique_value: unique only in snapshot"),
        ("drop_foreign_key", f"column {TABLE}.parent_cascade: foreign key only in snapshot"),
        ("change_on_delete", None),
        ("create_index", f"column {TABLE}.nullable_value: index only in database"),
        ("drop_index", f"column {TABLE}.indexed_value: index only in snapshot"),
        ("create_table", "table pyzzel_test_extra: only in database"),
        ("drop_table", f"table {TABLE}: only in snapshot"),
    ],
)
def test_manual_database_drift_reports_exact_lines(
    case, expected, database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE, "pyzzel_test_extra")
    _baseline()
    _applied(tmp_path)
    if case == "add_column":
        Base.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "extra" INTEGER')
    elif case == "drop_column":
        Base.execute(f'ALTER TABLE "{TABLE}" DROP COLUMN "nullable_value"')
    elif case == "alter_type":
        Base.execute(f'ALTER TABLE "{TABLE}" ALTER COLUMN "integer_value" TYPE BIGINT')
    elif case == "drop_not_null":
        Base.execute(f'ALTER TABLE "{TABLE}" ALTER COLUMN "required_value" DROP NOT NULL')
    elif case == "set_not_null":
        Base.execute(f'ALTER TABLE "{TABLE}" ALTER COLUMN "nullable_value" SET NOT NULL')
    elif case == "set_default":
        Base.execute(f'ALTER TABLE "{TABLE}" ALTER COLUMN "nullable_value" SET DEFAULT 5')
    elif case == "drop_default":
        Base.execute(f'ALTER TABLE "{TABLE}" ALTER COLUMN "integer_value" DROP DEFAULT')
    elif case == "add_unique":
        Base.execute(f'ALTER TABLE "{TABLE}" ADD UNIQUE ("nullable_value")')
    elif case == "drop_unique":
        name = _constraint_name(TABLE, "u", "unique_value")
        Base.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT "{name}"')
    elif case == "drop_foreign_key":
        name = _constraint_name(TABLE, "f", "parent_cascade")
        Base.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT "{name}"')
    elif case == "change_on_delete":
        name = _constraint_name(TABLE, "f", "parent_cascade")
        Base.execute(f'ALTER TABLE "{TABLE}" DROP CONSTRAINT "{name}"')
        Base.execute(
            f'ALTER TABLE "{TABLE}" ADD FOREIGN KEY ("parent_cascade") '
            f'REFERENCES "{PARENT}" ("id") ON DELETE RESTRICT'
        )
    elif case == "create_index":
        Base.execute(f'CREATE INDEX ON "{TABLE}" ("nullable_value")')
    elif case == "drop_index":
        name = _index_name(TABLE, "indexed_value")
        Base.execute(f'DROP INDEX "{name}"')
    elif case == "create_table":
        Base.execute('CREATE TABLE "pyzzel_test_extra" ("id" INTEGER)')
    elif case == "drop_table":
        Base.execute(f'DROP TABLE "{TABLE}"')
    report = _check(tmp_path, TABLE, PARENT, *( ("pyzzel_test_extra",) if case == "create_table" else () ), history_table=HISTORY)
    if case == "change_on_delete":
        assert report.drift == [
            f"column {TABLE}.parent_cascade: foreign key only in snapshot",
            f"column {TABLE}.parent_cascade: foreign key to {PARENT}.id (ON DELETE RESTRICT) only in database",
        ]
    else:
        assert report.drift == [expected]
    assert report.ok is False
    assert report.drift_checked is True


def test_unsupported_objects_warn_and_do_not_break_introspection(
    database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE, "pyzzel_test_composite_parent")
    _baseline()
    _applied(tmp_path)
    Base.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "array_value" INTEGER[]')
    Base.execute(f'CREATE INDEX "pyzzel_test_multi_idx" ON "{TABLE}" ("small", "integer_value")')
    Base.execute(f'CREATE INDEX "pyzzel_test_partial_idx" ON "{TABLE}" ("small") WHERE "small" > 0')
    Base.execute(f'CREATE INDEX "pyzzel_test_expression_idx" ON "{TABLE}" ((lower("plain_text")))')
    Base.execute(f'CREATE UNIQUE INDEX "pyzzel_test_unique_idx" ON "{TABLE}" ("big")')
    Base.execute(f'CREATE INDEX "pyzzel_test_gin_idx" ON "{TABLE}" USING GIN ("payload")')
    Base.execute('CREATE TABLE "pyzzel_test_composite_parent" ("a" INTEGER, "b" INTEGER, PRIMARY KEY ("a","b"))')
    for name, ddl in [
        ("pyzzel_test_composite_pk", 'CREATE TABLE "pyzzel_test_composite_pk" ("a" INTEGER, "b" INTEGER, PRIMARY KEY ("a","b"))'),
        ("pyzzel_test_composite_unique", 'CREATE TABLE "pyzzel_test_composite_unique" ("a" INTEGER, "b" INTEGER, UNIQUE ("a","b"))'),
        ("pyzzel_test_composite_fk", 'CREATE TABLE "pyzzel_test_composite_fk" ("a" INTEGER, "b" INTEGER, FOREIGN KEY ("a","b") REFERENCES "pyzzel_test_composite_parent" ("a","b"))'),
    ]:
        prepare_test_table(name)
        Base.execute(ddl)
    catalog, warnings = schema_from_database()
    assert catalog["tables"][TABLE]["columns"]["array_value"]["type"] == "integer[]"
    assert f"unsupported type integer[] on {TABLE}.array_value" in warnings
    assert any(item.startswith("ignored index pyzzel_test_multi_idx on ") for item in warnings)
    assert any(item.startswith("ignored index pyzzel_test_partial_idx on ") for item in warnings)
    assert any(item.startswith("ignored index pyzzel_test_expression_idx on ") for item in warnings)
    assert any(item.startswith("ignored index pyzzel_test_unique_idx on ") for item in warnings)
    assert any(item.startswith("ignored index pyzzel_test_gin_idx on ") for item in warnings)
    report = _check(tmp_path, TABLE, PARENT, history_table=HISTORY)
    expected_supported_warnings = [
        item for item in warnings if item.startswith(("unsupported type ", "ignored index "))
    ]
    assert report.warnings == expected_supported_warnings
    assert report.drift == [f"column {TABLE}.array_value: only in database"]
    assert "composite primary key on pyzzel_test_composite_pk is not supported" in warnings
    assert "composite unique on pyzzel_test_composite_unique is not supported" in warnings
    assert "composite foreign key on pyzzel_test_composite_fk is not supported" in warnings
    assert all("check constraint" not in warning for warning in warnings)
    Base.execute(f'ALTER TABLE "{TABLE}" ADD CONSTRAINT "pyzzel_test_check" CHECK ("small" > 0)')
    _catalog_again, check_warnings = schema_from_database()
    assert check_warnings == warnings
    assert _check(tmp_path, TABLE, PARENT, history_table=HISTORY).drift == report.drift


def test_other_schema_is_invisible_to_default_check_and_pull(
    database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, TABLE)
    class Item(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
    Base.create_tables()
    generate("initial", tmp_path)
    apply(tmp_path, history_table=HISTORY)
    Base.execute('CREATE SCHEMA "pyzzel_test_other"')
    try:
        Base.execute('CREATE TABLE "pyzzel_test_other"."pyzzel_test_other_table" ("id" INTEGER)')
        assert _check(tmp_path, TABLE, history_table=HISTORY).drift == []
        result = pull(tmp_path, history_table=HISTORY)
        assert not any("other_table" in line for line in result.changes)
        other, warnings = schema_from_database(schema="pyzzel_test_other")
        assert warnings == []
        assert "pyzzel_test_other_table" in other["tables"]
    finally:
        Base.execute('DROP SCHEMA IF EXISTS "pyzzel_test_other" CASCADE')


def test_pull_workflow_is_read_only_then_writes_and_generates_destructive_fix(
    database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE)
    _baseline()
    Base.create_tables()
    folder = tmp_path / "workflow"
    before = Base.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind='r' ORDER BY c.relname"
    )
    preview = pull(folder, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)
    assert len(preview.changes) >= 2
    assert not (folder / "snapshot.json").exists()
    assert Base.execute(
        "SELECT c.relname FROM pg_catalog.pg_class c "
        "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
        "WHERE n.nspname=current_schema() AND c.relkind='r' ORDER BY c.relname"
    ) == before
    result = pull(folder, write=True, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)
    assert result.written is True
    saved = load_snapshot(folder / "snapshot.json")
    db_snapshot, _ = schema_from_database(ignore=_ignored_existing_test_tables(TABLE, PARENT), exclude=(HISTORY,))
    assert saved == db_snapshot
    assert _check(folder, TABLE, PARENT, history_table=HISTORY).drift == []
    Base.execute(f'ALTER TABLE "{TABLE}" ADD COLUMN "manual_extra" INTEGER')
    pull(folder, write=True, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)
    migration = generate("fix_extra", folder)
    assert migration is not None
    assert "-- pyzzel:destructive" in migration.read_text(encoding="utf-8")
    assert f'DROP COLUMN IF EXISTS "manual_extra"' in migration.read_text(encoding="utf-8")
    assert apply(folder, allow_destructive=True, history_table=HISTORY) == ["0001_fix_extra.sql"]
    assert Base.execute(
        "SELECT 1 FROM pg_catalog.pg_attribute WHERE attrelid=%s::regclass AND attname=%s AND NOT attisdropped",
        (TABLE, "manual_extra"),
    ) == []
    assert _check(folder, TABLE, PARENT, history_table=HISTORY).ok is True


def test_check_and_pull_are_idempotent_and_do_not_change_catalog(
    database, prepare_test_table, tmp_path
):
    _prepare(prepare_test_table, HISTORY, PARENT, TABLE)
    _baseline()
    folder = tmp_path / "idempotent"
    _applied(folder)
    pull(folder, write=True, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)

    def catalog_snapshot():
        return (
            Base.execute(
                "SELECT c.relname, c.relkind FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid=c.relnamespace "
                "WHERE n.nspname=current_schema() ORDER BY c.relname"
            ),
            Base.execute(
                "SELECT conrelid::regclass::text, conname, contype FROM pg_catalog.pg_constraint "
                "WHERE connamespace=current_schema()::regnamespace ORDER BY conrelid,conname"
            ),
            Base.execute(
                "SELECT tablename,indexname,indexdef FROM pg_catalog.pg_indexes "
                "WHERE schemaname=current_schema() ORDER BY tablename,indexname"
            ),
        )

    before = catalog_snapshot()
    first_check = _check(folder, TABLE, PARENT, history_table=HISTORY)
    first_pull = pull(folder, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)
    middle = catalog_snapshot()
    second_check = _check(folder, TABLE, PARENT, history_table=HISTORY)
    second_pull = pull(folder, ignore=_ignored_existing_test_tables(TABLE, PARENT), history_table=HISTORY)
    after = catalog_snapshot()
    assert first_check == second_check
    assert first_pull == second_pull
    assert before == middle == after
