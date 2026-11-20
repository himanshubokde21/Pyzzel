"""Apply numbered migration files and inspect migration history."""

from datetime import datetime
import hashlib
from pathlib import Path
import re
from typing import NamedTuple

from pyzzel.errors import MigrationError
from pyzzel.pool import connection
from pyzzel.sql import quote_ident


class MigrationStatus(NamedTuple):
    name: str
    state: str
    applied_at: datetime | None


_FILE = re.compile(r"^\d{4}_[a-z][a-z0-9_]*\.sql$")
_LOCK_ID = 7_894_215_087_123  # Shared transaction lock for serialized migration runs.
_HISTORY_DDL = (
    'CREATE TABLE IF NOT EXISTS {table} ('
    '"id" INTEGER GENERATED ALWAYS AS IDENTITY PRIMARY KEY, '
    '"name" TEXT NOT NULL UNIQUE, "checksum" TEXT NOT NULL, '
    '"applied_at" TIMESTAMPTZ NOT NULL DEFAULT now())'
)


def _files(directory):
    found = []
    for path in sorted(Path(directory).iterdir() if Path(directory).exists() else ()):
        if not _FILE.fullmatch(path.name) or not path.is_file():
            continue
        number = path.name[:4]
        found.append((number, path.name, path))
    numbers = set()
    files = []
    for number, name, path in found:
        if number in numbers:
            raise MigrationError(f"Duplicate migration number {number}: {name}")
        numbers.add(number)
        try:
            raw = path.read_bytes()
            text = raw.decode("utf-8")
        except (OSError, UnicodeError) as error:
            raise MigrationError(f"Could not read migration {name}: {error}") from error
        normalized = raw.replace(b"\r\n", b"\n")
        files.append((name, text, hashlib.sha256(normalized).hexdigest()))
    return files


def _table_name(history_table):
    return quote_ident(history_table)


def _history(conn, table_sql):
    exists = conn.execute("SELECT to_regclass(%s)", (table_sql,)).fetchone()[0]
    if exists is None:
        return None
    rows = conn.execute(
        f"SELECT \"name\", \"checksum\", \"applied_at\" FROM {table_sql}"
    ).fetchall()
    return {name: (checksum, applied_at) for name, checksum, applied_at in rows}


def status(directory="migrations", *, history_table="pyzzel_migrations"):
    table_sql = _table_name(history_table)
    files = _files(directory)
    with connection() as conn:
        history = _history(conn, table_sql)
    history = history or {}
    result = []
    names = set()
    for name, _text, checksum in files:
        names.add(name)
        stored = history.get(name)
        if stored is None:
            result.append(MigrationStatus(name, "pending", None))
        elif stored[0] == checksum:
            result.append(MigrationStatus(name, "applied", stored[1]))
        else:
            result.append(MigrationStatus(name, "edited", stored[1]))
    result.extend(
        MigrationStatus(name, "missing", applied_at)
        for name, (_checksum, applied_at) in history.items() if name not in names
    )
    return sorted(result, key=lambda item: item.name)


def _check_history(files, history):
    by_name = {name: checksum for name, _text, checksum in files}
    for name, (_checksum, _applied_at) in history.items():
        if name not in by_name:
            raise MigrationError(f"Applied migration {name} is missing from disk")
        if by_name[name] != _checksum:
            raise MigrationError(f"Migration {name} was edited after it was applied")
    applied = sorted(history)
    latest = applied[-1] if applied else None
    pending = [item for item in files if item[0] not in history]
    if latest is not None:
        for name, _text, _checksum in pending:
            if name < latest:
                raise MigrationError(
                    f"Migration {name} is older than applied migration {latest}; renumber it"
                )
    return pending


def apply(directory="migrations", *, allow_destructive=False, dry_run=False,
          history_table="pyzzel_migrations"):
    table_sql = _table_name(history_table)
    files = _files(directory)
    with connection() as conn:
        if dry_run:
            history = _history(conn, table_sql) or {}
            pending = _check_history(files, history)
            return [name for name, _text, _checksum in pending]

        applied = []
        while True:
            name = "<migration history>"
            try:
                with conn.transaction():
                    # Transaction-scoped locks work through transaction poolers,
                    # unlike session-scoped locks whose backend may be reassigned.
                    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_LOCK_ID,))
                    history = _history(conn, table_sql) or {}
                    pending = _check_history(files, history)
                    if not pending:
                        conn.execute(_HISTORY_DDL.format(table=table_sql))
                        return applied
                    destructive = [
                        migration_name
                        for migration_name, migration_text, _checksum in pending
                        if any(
                            line.strip().startswith("-- pyzzel:destructive")
                            for line in migration_text.splitlines()
                        )
                    ]
                    if destructive and not allow_destructive:
                        names = ", ".join(destructive)
                        raise MigrationError(
                            f"Destructive migrations require allow_destructive=True "
                            f"(CLI: --allow-destructive): {names}"
                        )
                    conn.execute(_HISTORY_DDL.format(table=table_sql))
                    name, text, checksum = pending[0]
                    conn.execute(text)
                    conn.execute(
                        f"INSERT INTO {table_sql} (\"name\", \"checksum\") VALUES (%s, %s)",
                        (name, checksum),
                    )
            except MigrationError:
                raise
            except Exception as error:
                raise MigrationError(f"Migration {name} failed: {error}") from error
            applied.append(name)
