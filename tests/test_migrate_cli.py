import os
import sys

import pytest

from pyzzel import Base, connect, disconnect
from pyzzel.errors import NotConnectedError
from pyzzel.migrate.__main__ import main


@pytest.fixture
def registry():
    old = Base._registry.copy()
    Base._registry.clear()
    yield
    Base._registry.clear()
    Base._registry.update(old)


def test_generate_create_models_and_usage(tmp_path, monkeypatch, capsys, registry):
    module = tmp_path / "tiny_models.py"
    module.write_text(
        "from pyzzel import Base, Column, Integer, String\n"
        "class Tiny(Base):\n"
        "    __tablename__ = 'cli_tiny'\n"
        "    id = Column(Integer, primary_key=True)\n"
        "    name = Column(String)\n"
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    directory = tmp_path / "migrations"
    assert main(["generate", "init", "--dir", str(directory), "--models", "tiny_models"]) == 0
    first_output = capsys.readouterr().out.strip()
    assert first_output == str(directory / "0001_init.sql")
    assert (directory / "0001_init.sql").exists()
    assert main(["generate", "again", "--dir", str(directory), "--models", "tiny_models"]) == 0
    assert capsys.readouterr().out.strip() == "No changes."
    assert main(["create", "manual", "--dir", str(directory)]) == 0
    assert capsys.readouterr().out.strip() == str(directory / "0002_manual.sql")
    assert (directory / "0002_manual.sql").read_text() == "-- pyzzel migration 0002_manual\n"
    for argv in (["unknown"], ["generate"]):
        with pytest.raises(SystemExit) as error:
            main(list(argv))
        assert error.value.code == 2
    assert main(["generate", "ok", "--dir", str(directory), "--models", "module_that_does_not_exist_xyz"]) == 1
    assert "could not import module_that_does_not_exist_xyz:" in capsys.readouterr().err
    monkeypatch.delenv("PYZZEL_DATABASE_URL", raising=False)
    for command in ("apply", "status"):
        assert main([command, "--dir", str(directory)]) == 1
        assert "Database URL required" in capsys.readouterr().err
    sys.modules.pop("tiny_models", None)


DATABASE_URL = os.environ.get("PYZZEL_TEST_DATABASE_URL")
_DATABASE_REQUIRED = pytest.mark.skipif(
    not DATABASE_URL,
    reason="Set PYZZEL_TEST_DATABASE_URL to a dedicated disposable PostgreSQL test database.",
)


def _clean_database():
    connect(DATABASE_URL)
    try:
        for table in ("pyzzel_test_cli_child", "pyzzel_test_cli_table", "pyzzel_test_migrations"):
            Base.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')
    finally:
        disconnect()


@pytest.fixture
def clean_cli_database():
    _clean_database()
    yield
    _clean_database()


@_DATABASE_REQUIRED
def test_cli_apply_status_destructive_and_disconnect(tmp_path, capsys, clean_cli_database):
    directory = tmp_path / "migrations"
    directory.mkdir()
    (directory / "0001_create.sql").write_text(
        "CREATE TABLE pyzzel_test_cli_table (id INTEGER);\n", encoding="utf-8"
    )
    assert main(["apply", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations"]) == 0
    assert capsys.readouterr().out.strip() == "applied 0001_create.sql"
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")
    assert main(["apply", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations"]) == 0
    assert capsys.readouterr().out.strip() == "Nothing to apply."
    (directory / "0002_child.sql").write_text(
        "-- pyzzel:destructive\nCREATE TABLE pyzzel_test_cli_child (id INTEGER);\n", encoding="utf-8"
    )
    assert main(["apply", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations", "--dry-run"]) == 0
    assert capsys.readouterr().out.strip() == "would apply 0002_child.sql"
    connect(DATABASE_URL)
    try:
        assert Base.execute("SELECT to_regclass(%s)", ('"pyzzel_test_cli_child"',)) == [(None,)]
    finally:
        disconnect()
    assert main(["apply", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations"]) == 1
    assert "allow_destructive" in capsys.readouterr().err
    assert main(["apply", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations", "--allow-destructive"]) == 0
    assert capsys.readouterr().out.strip() == "applied 0002_child.sql"
    assert main(["status", "--dir", str(directory), "--url", DATABASE_URL,
                 "--history-table", "pyzzel_test_migrations"]) == 0
    output = capsys.readouterr().out
    assert "applied  0001_create.sql" in output and "applied  0002_child.sql" in output
    assert DATABASE_URL not in output and DATABASE_URL not in capsys.readouterr().err
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")
