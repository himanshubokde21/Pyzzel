"""Offline migration snapshot and SQL generation."""

import re
from pathlib import Path

from pyzzel.errors import MigrationError
from .diff import diff, describe
from .schema import load_snapshot, save_snapshot, schema_from_models
from .sqlgen import render
from .apply import MigrationStatus, apply, status
from .introspect import fetch_catalog, build_snapshot, schema_from_database
from .compare import PullResult, CheckReport, pull, check

_NAME = re.compile(r"^[a-z][a-z0-9_]{0,50}$")


def _validate_name(name):
    if not isinstance(name, str) or _NAME.fullmatch(name) is None:
        raise MigrationError("Migration name must match ^[a-z][a-z0-9_]{0,50}$")


def _next_path(name, directory):
    _validate_name(name)
    directory = Path(directory)
    highest = 0
    if directory.exists():
        for path in directory.iterdir():
            match = re.match(r"^(\d{4})_.*\.sql$", path.name)
            if match:
                highest = max(highest, int(match[1]))
    path = directory / f"{highest + 1:04d}_{name}.sql"
    if path.exists():
        raise MigrationError(f"Migration file already exists: {path}")
    return path


def generate(name, directory="migrations"):
    path = _next_path(name, directory)
    snapshot_path = path.parent / "snapshot.json"
    old = load_snapshot(snapshot_path)
    new = schema_from_models()
    ops = diff(old, new)
    if not ops:
        return None
    path.parent.mkdir(parents=True, exist_ok=True)
    content = f"-- pyzzel migration {path.stem}\n\n{render(ops)}\n"
    path.write_text(content, encoding="utf-8", newline="\n")
    save_snapshot(snapshot_path, new)
    return path


def create(name, directory="migrations"):
    path = _next_path(name, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"-- pyzzel migration {path.stem}\n", encoding="utf-8", newline="\n")
    return path


__all__ = ["generate", "create", "apply", "status", "MigrationStatus", "MigrationError",
           "diff", "describe", "fetch_catalog", "build_snapshot", "schema_from_database",
           "PullResult", "CheckReport", "pull", "check"]
