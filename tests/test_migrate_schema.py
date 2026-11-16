import json
import subprocess
import sys

import pytest

from pyzzel import Base, Column, Integer, Numeric, String
from pyzzel.errors import MigrationError
from pyzzel.migrate.schema import canonical_type, load_snapshot, normalize_default, save_snapshot, schema_from_models


@pytest.fixture
def registry():
    old = Base._registry.copy()
    Base._registry.clear()
    yield
    Base._registry.clear()
    Base._registry.update(old)


def test_type_and_default_normalization():
    examples = {
        "INTEGER": "integer", "SMALLINT": "smallint", "BIGINT": "bigint",
        "DOUBLE PRECISION": "double precision", "NUMERIC": "numeric",
        "NUMERIC(10)": "numeric(10)", "NUMERIC(10,2)": "numeric(10,2)",
        "TEXT": "text", "VARCHAR(50)": "character varying(50)",
        "character varying(50)": "character varying(50)", "BOOLEAN": "boolean",
        "DATE": "date", "TIME": "time without time zone",
        "TIMESTAMPTZ": "timestamp with time zone", "TIMESTAMP": "timestamp without time zone",
        "UUID": "uuid", "JSONB": "jsonb", "BYTEA": "bytea", " Weird   Type ": "weird type",
    }
    for source, expected in examples.items():
        assert canonical_type(source) == expected
        assert canonical_type(expected) == expected
    defaults = {None: None, " TRUE ": "true", "NOW()": "now()", "current_timestamp": "now()",
                "'Active'": "'Active'", "'active'::text": "'active'", "'active'::character varying": "'active'", "0::bigint": "0", "(0)": "0", "(0) + (2)": "(0) + (2)", "'A  B'": "'A  B'", "('It''s')": "'It''s'",
                "'-1'": "-1", "'0'": "0", "'1.5'": "1.5", "(-1)": "-1", "'-1'::integer": "-1"}
    for source, expected in defaults.items():
        assert normalize_default(source) == expected


def test_snapshot_exact_round_trip(registry, tmp_path):
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

    snapshot = schema_from_models()
    assert snapshot["tables"]["orders"]["indexes"] == ["user_id", "note"]
    assert snapshot["tables"]["orders"]["columns"]["user_id"] == {
        "type": "integer", "nullable": False, "primary_key": False, "identity": False,
        "unique": False, "default": None, "references": ["users", "id"], "on_delete": "CASCADE",
    }
    assert snapshot["tables"]["orders"]["columns"]["total"]["default"] == "0"
    assert snapshot["tables"]["users"]["columns"]["name"]["type"] == "character varying(50)"
    assert snapshot["tables"]["orders"]["columns"]["version"] == {
        "type": "integer", "nullable": False, "primary_key": False, "identity": False,
        "unique": False, "default": "1", "references": None, "on_delete": None,
    }
    file = tmp_path / "snapshot.json"
    save_snapshot(file, snapshot)
    raw = file.read_text()
    assert raw.endswith("\n") and not any(line.endswith(" ") for line in raw.splitlines())
    assert list(load_snapshot(file)["tables"]["orders"]["columns"]) == list(snapshot["tables"]["orders"]["columns"])
    assert load_snapshot(tmp_path / "missing.json") == {"version": 1, "tables": {}}
    file.write_text(json.dumps({"version": 2, "tables": {}}))
    with pytest.raises(MigrationError):
        load_snapshot(file)


def test_import_isolated_in_subprocess():
    code = "import sys, pyzzel; assert 'pyzzel.migrate' not in sys.modules; import pyzzel.migrate; assert 'psycopg' not in sys.modules and 'psycopg_pool' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)
