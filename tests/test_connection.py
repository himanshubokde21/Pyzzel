import subprocess
import sys
import time
import traceback

import pytest

from pyzzel import Base, connect, disconnect
from pyzzel.errors import NotConnectedError, PyzzelError
from pyzzel.pool import connection


@pytest.fixture(autouse=True)
def disconnected_pool():
    disconnect()
    yield
    disconnect()


def test_database_operations_require_a_connection():
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")
    with pytest.raises(NotConnectedError):
        with connection():
            pass


def test_disconnect_when_not_connected_is_safe():
    disconnect()


@pytest.mark.parametrize("prepare_threshold", [None, 7])
def test_connect_configures_prepared_statement_threshold(monkeypatch, prepare_threshold):
    import types
    import pyzzel.pool as pool_module

    calls = {}

    class FakeConnection:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            return False

    class FakePool:
        def __init__(self, url, **options):
            calls["pool"] = (url, options)

        def wait(self, timeout):
            calls["wait"] = timeout

        def close(self, **kwargs):
            calls["closed"] = True

    fake_psycopg = types.SimpleNamespace(
        Error=Exception,
        connect=lambda url, **options: (calls.setdefault("probe", (url, options)), FakeConnection())[1],
    )
    fake_psycopg_pool = types.SimpleNamespace(ConnectionPool=FakePool)
    monkeypatch.setitem(sys.modules, "psycopg", fake_psycopg)
    monkeypatch.setitem(sys.modules, "psycopg_pool", fake_psycopg_pool)
    monkeypatch.setattr(pool_module, "_pool", None)

    connect("postgresql://example.test/db", prepare_threshold=prepare_threshold)

    assert calls["probe"][1]["prepare_threshold"] == prepare_threshold
    assert calls["pool"][1]["kwargs"] == {
        "autocommit": True,
        "prepare_threshold": prepare_threshold,
    }
    disconnect()


def test_unreachable_connection_fails_fast_without_leaking_url_or_leaving_pool():
    url = "postgresql://user:secret@127.0.0.1:1/db"
    started = time.perf_counter()
    with pytest.raises(PyzzelError) as error_info:
        connect(url, timeout=2)
    elapsed = time.perf_counter() - started
    error = error_info.value
    assert str(error) == "Could not connect to the database."
    assert "secret" not in str(error)
    assert "secret" not in "".join(traceback.format_exception(error))
    # Refused connections can take about 2 seconds per attempt on Windows.
    assert elapsed < 10
    with pytest.raises(NotConnectedError):
        Base.execute("SELECT 1")
    with pytest.raises(PyzzelError) as retry_error:
        connect(url, timeout=2)
    assert not isinstance(retry_error.value, NotConnectedError)
    assert "secret" not in str(retry_error.value)


def test_transaction_requires_a_connection():
    with pytest.raises(NotConnectedError):
        with Base.transaction():
            pass


def test_importing_package_does_not_load_psycopg_drivers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pyzzel; from pyzzel import connect, disconnect, Base; "
            "assert connect and disconnect and Base; "
            "assert 'psycopg' not in sys.modules; assert 'psycopg_pool' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
