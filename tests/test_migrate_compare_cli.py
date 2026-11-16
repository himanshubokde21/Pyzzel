"""Database-backed migration CLI checks (Phase 13D)."""

import os

import pytest

from pyzzel import Base, Column, Integer, String, connect, disconnect
from pyzzel.errors import NotConnectedError
from pyzzel.migrate import apply, check, generate
from pyzzel.migrate.__main__ import main

DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)
HISTORY = "pyzzel_test_migrations"
TABLE = "pyzzel_test_compare"


@pytest.fixture
def cli_database(database):
    yield
    disconnect()
    connect(DATABASE_URL)


def _prepare(prepare_test_table, *tables):
    for name in tables:
        prepare_test_table(name)


def _run_cli(argv):
    disconnect()
    return main(argv)


def _run_sql(sql, params=()):
    disconnect()
    connect(DATABASE_URL)
    return Base.execute(sql, params)


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


def _ignore_cli_args(*keep):
    return [arg for name in _ignored_existing_test_tables(*keep) for arg in ("--ignore", name)]


def test_ignore_tables_and_history_table_for_check_and_cli(
    cli_database, prepare_test_table, tmp_path, capsys
):
    _prepare(prepare_test_table, HISTORY, TABLE, "pyzzel_test_extra")
    class Item(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
        value = Column(String)
    Base.create_tables()
    _run_sql('CREATE TABLE "pyzzel_test_extra" ("id" INTEGER)')
    generate("initial", tmp_path)
    apply(tmp_path, history_table=HISTORY)
    without = check(tmp_path, history_table=HISTORY)
    assert "table pyzzel_test_extra: only in database" in without.drift
    assert all(HISTORY not in line for line in without.drift)
    with_ignore = check(tmp_path, history_table=HISTORY, ignore=("pyzzel_test_extra", *_ignored_existing_test_tables(TABLE)))
    assert with_ignore.drift == []
    assert _run_cli([
        "check", "--dir", str(tmp_path), "--url", DATABASE_URL, "--models", "pyzzel",
        "--history-table", HISTORY, "--ignore", "pyzzel_test_extra", *_ignore_cli_args(TABLE)
    ]) == 0
    assert "OK: models, migrations and database agree." in capsys.readouterr().out
    pull_dir = tmp_path / "ignored_pull"
    assert _run_cli(["pull", "--dir", str(pull_dir), "--url", DATABASE_URL,
                     "--history-table", HISTORY, "--ignore", "pyzzel_test_extra", *_ignore_cli_args(TABLE)]) == 0
    assert "pyzzel_test_extra" not in capsys.readouterr().out


def test_cli_check_and_pull_against_database_redacts_url_and_disconnects(
    cli_database, prepare_test_table, tmp_path, capsys
):
    _prepare(prepare_test_table, HISTORY, TABLE, "pyzzel_test_extra")
    class Item(Base):
        __tablename__ = TABLE
        id = Column(Integer, primary_key=True)
        value = Column(String)
    Base.create_tables()
    directory = tmp_path / "cli"
    generate("initial", directory)
    apply(directory, history_table=HISTORY)
    assert _run_cli([
        "check", "--dir", str(directory), "--url", DATABASE_URL, "--models", "pyzzel",
        "--history-table", HISTORY, *_ignore_cli_args(TABLE)
    ]) == 0
    output = capsys.readouterr()
    assert "OK: models, migrations and database agree." in output.out
    assert DATABASE_URL not in output.out + output.err
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")
    _run_sql('CREATE TABLE "pyzzel_test_extra" ("id" INTEGER)')
    assert _run_cli([
        "check", "--dir", str(directory), "--url", DATABASE_URL, "--models", "pyzzel",
        "--history-table", HISTORY, *_ignore_cli_args(TABLE, "pyzzel_test_extra")
    ]) == 1
    output = capsys.readouterr()
    assert "Drift (snapshot vs database):" in output.out
    assert "  - table pyzzel_test_extra: only in database" in output.out
    assert DATABASE_URL not in output.out + output.err
    pending = directory / "0002_manual.sql"
    pending.write_text("-- pyzzel migration 0002_manual\n", encoding="utf-8")
    assert _run_cli([
        "check", "--dir", str(directory), "--url", DATABASE_URL, "--models", "pyzzel",
        "--history-table", HISTORY, *_ignore_cli_args(TABLE, "pyzzel_test_extra")
    ]) == 1
    output = capsys.readouterr()
    assert "Migration files:" in output.out
    assert DATABASE_URL not in output.out + output.err
    (directory / "0002_manual.sql").unlink()
    pull_dir = tmp_path / "cli_pull"
    _run_sql('DROP TABLE "pyzzel_test_extra"')
    assert _run_cli([
        "pull", "--dir", str(pull_dir), "--url", DATABASE_URL,
        "--history-table", HISTORY, *_ignore_cli_args(TABLE)
    ]) == 0
    output = capsys.readouterr()
    assert "Differences between the snapshot and the database:" in output.out
    assert "run again with --write to replace the snapshot" in output.out
    assert _run_cli([
        "pull", "--dir", str(pull_dir), "--url", DATABASE_URL,
        "--history-table", HISTORY, "--write", *_ignore_cli_args(TABLE)
    ]) == 0
    output = capsys.readouterr()
    assert "snapshot.json updated" in output.out
    assert DATABASE_URL not in output.out + output.err
    assert _run_cli([
        "pull", "--dir", str(pull_dir), "--url", DATABASE_URL,
        "--history-table", HISTORY, *_ignore_cli_args(TABLE)
    ]) == 0
    assert "Snapshot already matches the database." in capsys.readouterr().out
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")


