"""Compare models, migration files, snapshots, and a PostgreSQL schema."""

from pathlib import Path
from typing import NamedTuple

from pyzzel.core.base import Base
from pyzzel.errors import MigrationError
from .diff import describe, diff
from .introspect import schema_from_database
from .schema import load_snapshot, save_snapshot, schema_from_models
from .apply import status


class PullResult(NamedTuple):
    changes: list[str]
    warnings: list[str]
    written: bool


class CheckReport(NamedTuple):
    pending_model_changes: list[str]
    migration_problems: list[str]
    drift: list[str]
    warnings: list[str]
    drift_checked: bool

    @property
    def ok(self):
        return not (self.pending_model_changes or self.migration_problems or self.drift)


def pull(directory="migrations", *, write=False, ignore=(), history_table="pyzzel_migrations"):
    directory = Path(directory)
    current = load_snapshot(directory / "snapshot.json")
    database, warnings = schema_from_database(ignore=ignore, exclude=(history_table,))
    changes = [describe(op, "snapshot", "database") for op in diff(current, database)]
    if write:
        save_snapshot(directory / "snapshot.json", database)
    return PullResult(changes, warnings, bool(write))


def check(directory="migrations", *, ignore=(), history_table="pyzzel_migrations"):
    if not Base._registry:
        raise MigrationError("No models are registered; import your models first (for the command line use --models).")
    directory = Path(directory)
    snapshot = load_snapshot(directory / "snapshot.json")
    models = schema_from_models()
    pending = [describe(op, "snapshot", "models") for op in diff(snapshot, models)]
    problems = []
    for item in status(directory, history_table=history_table):
        if item.state == "pending": problems.append(f"{item.name}: not applied yet")
        elif item.state == "edited": problems.append(f"{item.name}: edited after it was applied")
        elif item.state == "missing": problems.append(f"{item.name}: applied but the file is missing")
    if problems:
        return CheckReport(pending, problems, [], ["drift not checked: apply or fix the migration files first"], False)
    database, warnings = schema_from_database(ignore=ignore, exclude=(history_table,))
    drift = [describe(op, "snapshot", "database") for op in diff(snapshot, database)]
    return CheckReport(pending, problems, drift, warnings, True)
