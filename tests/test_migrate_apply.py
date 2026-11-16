import os
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import MigrationError
from pyzzel.migrate import apply, generate, status


DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)
HISTORY = "pyzzel_test_migrations"


def _prepare(prepare_test_table, *tables):
    for name in tables:
        prepare_test_table(name)


def _write(directory, name, sql):
    path = Path(directory) / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sql, encoding="utf-8", newline="\n")
    return path


def test_generated_migrations_apply_real_schema_and_are_idempotent(database, prepare_test_table, tmp_path):
    _prepare(prepare_test_table, HISTORY, "pyzzel_test_users", "pyzzel_test_orders", "pyzzel_test_coupons")

    class User(Base):
        __tablename__ = "pyzzel_test_users"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)

    class Order(Base):
        __tablename__ = "pyzzel_test_orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="pyzzel_test_users.id", on_delete="CASCADE")
        total = Column(Numeric(10, 2), server_default="0")
        note = Column(String, index=True)
        code = Column(String(20), unique=True)

    assert generate("init", tmp_path)
    Base._registry.clear()

    class UserChanged(Base):
        __tablename__ = "pyzzel_test_users"
        id = Column(Integer, primary_key=True)
        name = Column(String(50), nullable=False)

    class OrderChanged(Base):
        __tablename__ = "pyzzel_test_orders"
        id = Column(Integer, primary_key=True)
        user_id = Column(Integer, nullable=False, foreign_key="pyzzel_test_users.id", on_delete="CASCADE")
        total = Column(Numeric(12, 2), server_default="0")
        status = Column(String(10), nullable=False, server_default="'new'")
        code = Column(String(20))

    class Coupon(Base):
        __tablename__ = "pyzzel_test_coupons"
        id = Column(Integer, primary_key=True)
        code = Column(String(20), unique=True)

    assert generate("changes", tmp_path)
    assert apply(tmp_path, allow_destructive=True, history_table=HISTORY) == ["0001_init.sql", "0002_changes.sql"]
    assert apply(tmp_path, allow_destructive=True, history_table=HISTORY) == []
    assert [row.state for row in status(tmp_path, history_table=HISTORY)] == ["applied", "applied"]
    columns = Base.execute(
        "SELECT table_name, column_name, data_type, is_nullable, column_default, character_maximum_length, numeric_precision, numeric_scale "
        "FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name IN (%s, %s, %s) ORDER BY table_name, ordinal_position",
        ("pyzzel_test_users", "pyzzel_test_orders", "pyzzel_test_coupons"),
    )
    data = {(t, c): (typ, nullable, default, charlen, precision, scale)
            for t, c, typ, nullable, default, charlen, precision, scale in columns}
    assert data[("pyzzel_test_users", "name")][:4] == ("character varying", "NO", None, 50)
    assert data[("pyzzel_test_orders", "total")][0:1] == ("numeric",)
    assert data[("pyzzel_test_orders", "total")][4:] == (12, 2)
    assert data[("pyzzel_test_orders", "status")][1] == "NO"
    assert "note" not in {column for table, column in data if table == "pyzzel_test_orders"}
    constraints = Base.execute(
        "SELECT conname, contype, confdeltype FROM pg_constraint "
        "WHERE conrelid IN ('pyzzel_test_orders'::regclass, 'pyzzel_test_coupons'::regclass)"
    )
    assert not any(name == "pyzzel_test_orders_code_key" for name, *_ in constraints)
    assert any(kind == "f" and delete_rule == "c" for _name, kind, delete_rule in constraints)
    indexes = {row[0] for row in Base.execute(
        "SELECT indexname FROM pg_indexes WHERE schemaname = current_schema() "
        "AND tablename = 'pyzzel_test_orders'"
    )}
    assert "ix_pyzzel_test_orders_user_id" in indexes
    assert "ix_pyzzel_test_orders_note" not in indexes


def test_existing_base_table_and_history_absent_status_and_dry_run(database, prepare_test_table, tmp_path):
    _prepare(prepare_test_table, HISTORY, "pyzzel_test_baseline", "pyzzel_test_dry")
    class Baseline(Base):
        __tablename__ = "pyzzel_test_baseline"
        id = Column(Integer, primary_key=True)
    Base.create_tables()
    folder = tmp_path / "empty_history"
    _write(folder, "0001_one.sql", "CREATE TABLE pyzzel_test_dry (id INTEGER);")
    _write(folder, "0002_baseline.sql", "CREATE TABLE IF NOT EXISTS pyzzel_test_baseline (id INTEGER PRIMARY KEY);")
    assert [item.state for item in status(folder, history_table=HISTORY)] == ["pending", "pending"]
    assert Base.execute("SELECT to_regclass(%s)", (f'"{HISTORY}"',)) == [(None,)]
    assert apply(folder, dry_run=True, history_table=HISTORY) == ["0001_one.sql", "0002_baseline.sql"]
    assert Base.execute("SELECT to_regclass(%s)", (f'"{HISTORY}"',)) == [(None,)]
    assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_dry"',)) == [(None,)]
    assert apply(folder, history_table=HISTORY) == ["0001_one.sql", "0002_baseline.sql"]


def test_checksum_missing_order_duplicate_and_crlf(database, prepare_test_table, tmp_path):
    _prepare(prepare_test_table, HISTORY, "pyzzel_test_checksum")
    folder = tmp_path / "checksums"
    path = _write(folder, "0001_first.sql", "CREATE TABLE pyzzel_test_checksum (id INTEGER);\n")
    assert apply(folder, history_table=HISTORY) == ["0001_first.sql"]
    path.write_bytes(path.read_bytes().replace(b"\n", b"\r\n"))
    assert status(folder, history_table=HISTORY)[0].state == "applied"
    path.write_text(path.read_text() + "-- changed\n")
    assert status(folder, history_table=HISTORY)[0].state == "edited"
    with pytest.raises(MigrationError, match="edited after it was applied"):
        apply(folder, history_table=HISTORY)
    path.unlink()
    assert status(folder, history_table=HISTORY)[0].state == "missing"
    with pytest.raises(MigrationError, match="missing"):
        apply(folder, history_table=HISTORY)
    order_history = "pyzzel_test_migrations_order"
    prepare_test_table(order_history)
    order_folder = tmp_path / "ordering"
    _write(order_folder, "0002_latest.sql", "SELECT 1;")
    assert apply(order_folder, history_table=order_history) == ["0002_latest.sql"]
    _write(order_folder, "0001_older.sql", "SELECT 1;")
    with pytest.raises(MigrationError, match="older than applied migration"):
        apply(order_folder, history_table=order_history)
    duplicate = tmp_path / "duplicate"
    _write(duplicate, "0001_one.sql", "SELECT 1;")
    _write(duplicate, "0001_two.sql", "SELECT 2;")
    with pytest.raises(MigrationError, match="Duplicate migration number 0001"):
        apply(duplicate, history_table=HISTORY)


def test_destructive_preflight_and_per_file_rollback(database, prepare_test_table, tmp_path):
    failure_history = "pyzzel_test_migrations_failure"
    _prepare(prepare_test_table, HISTORY, failure_history, "pyzzel_test_preflight", "pyzzel_test_a", "pyzzel_test_b", "pyzzel_test_c")
    folder = tmp_path / "destructive"
    _write(folder, "0001_first.sql", "CREATE TABLE pyzzel_test_preflight (id INTEGER);")
    _write(folder, "0002_danger.sql", "-- pyzzel:destructive\nDROP TABLE pyzzel_test_preflight;")
    with pytest.raises(MigrationError, match="allow_destructive"):
        apply(folder, history_table=HISTORY)
    assert Base.execute("SELECT to_regclass(%s)", (f'"{HISTORY}"',)) == [(None,)]
    assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_preflight"',)) == [(None,)]
    assert apply(folder, allow_destructive=True, history_table=HISTORY) == ["0001_first.sql", "0002_danger.sql"]

    failing = tmp_path / "failing"
    _write(failing, "0001_a.sql", "CREATE TABLE pyzzel_test_a (id INTEGER);")
    bad = _write(failing, "0002_b.sql", "CREATE TABLE pyzzel_test_b (id INTEGER);\nINVALID SQL;")
    _write(failing, "0003_c.sql", "CREATE TABLE pyzzel_test_c (id INTEGER);")
    with pytest.raises(MigrationError, match="Migration 0002_b.sql failed"):
        apply(failing, history_table=failure_history)
    assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_a"',)) != [(None,)]
    assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_b"',)) == [(None,)]
    assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_c"',)) == [(None,)]
    bad.write_text("CREATE TABLE pyzzel_test_b (id INTEGER);\n")
    assert apply(failing, history_table=failure_history) == ["0002_b.sql", "0003_c.sql"]


def test_advisory_lock_percent_and_history_identifier(database, prepare_test_table, tmp_path):
    _prepare(prepare_test_table, HISTORY, "pyzzel_test_percent", "pyzzel_test_lock")
    folder = tmp_path / "parallel"
    _write(folder, "0001_sleep.sql", "SELECT pg_sleep(0.5); CREATE TABLE pyzzel_test_lock (v TEXT);")
    _write(folder, "0002_percent.sql", "CREATE TABLE pyzzel_test_percent (v TEXT); INSERT INTO pyzzel_test_percent VALUES ('100%');")
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(apply, folder, history_table=HISTORY) for _ in range(2)]
        results = [future.result() for future in futures]
    assert sorted(name for result in results for name in result) == ["0001_sleep.sql", "0002_percent.sql"]
    assert Base.execute("SELECT v FROM pyzzel_test_percent") == [("100%",)]
    assert Base.execute(f'SELECT count(*) FROM "{HISTORY}"')[0][0] == 2
    with pytest.raises(ValueError):
        status(folder, history_table='invalid"name')
