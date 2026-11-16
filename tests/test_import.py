import subprocess
import sys


def test_package_import_and_version():
    import pyzzel

    assert pyzzel.__version__ == "0.0.1"


def test_package_modules_are_importable():
    import pyzzel.core
    import pyzzel.pool
    import pyzzel.sql

    assert pyzzel.core
    assert pyzzel.sql
    assert pyzzel.pool


def test_import_does_not_load_psycopg_drivers():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys, pyzzel; assert 'psycopg' not in sys.modules; "
            "assert 'psycopg_pool' not in sys.modules; "
            "assert 'decimal' not in sys.modules; assert 'uuid' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
