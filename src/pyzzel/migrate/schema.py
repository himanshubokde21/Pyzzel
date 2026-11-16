"""Offline model schema snapshots."""

import json
import os
from pathlib import Path
import re
import tempfile

from pyzzel.errors import MigrationError


_TYPES = {
    "integer": "integer", "int": "integer", "int4": "integer",
    "smallint": "smallint", "int2": "smallint", "bigint": "bigint", "int8": "bigint",
    "double precision": "double precision", "numeric": "numeric", "decimal": "numeric",
    "text": "text", "boolean": "boolean", "bool": "boolean", "date": "date",
    "time": "time without time zone", "time without time zone": "time without time zone",
    "timetz": "time with time zone", "timestamp": "timestamp without time zone",
    "timestamp without time zone": "timestamp without time zone",
    "timestamptz": "timestamp with time zone", "timestamp with time zone": "timestamp with time zone",
    "uuid": "uuid", "jsonb": "jsonb", "bytea": "bytea",
}


def canonical_type(sql_type):
    value = " ".join(sql_type.strip().lower().split())
    varchar = re.fullmatch(r"(?:varchar|character varying)\s*(\(\s*\d+\s*\))", value)
    if varchar:
        return f"character varying({varchar[1][1:-1].strip()})"
    numeric = re.fullmatch(r"(?:numeric|decimal)\s*(\(\s*\d+\s*(?:,\s*\d+\s*)?\))", value)
    if numeric:
        return "numeric(" + ",".join(part.strip() for part in numeric[1][1:-1].split(",")) + ")"
    return _TYPES.get(value, value)


def normalize_default(expr):
    if expr is None:
        return None
    value = expr.strip()
    while True:
        stripped = re.sub(r"::\s*[a-z_][a-z0-9_ ]*(?:\([^)]*\))?\s*$", "", value, flags=re.I)
        if stripped == value:
            break
        value = stripped
    value = value.strip()
    depth, quoted, closes_at_end, index = 0, False, False, 0
    while index < len(value):
        ch = value[index]
        if ch == "'":
            if quoted and index + 1 < len(value) and value[index + 1] == "'":
                index += 2
                continue
            quoted = not quoted
        elif not quoted and ch == "(":
            depth += 1
        elif not quoted and ch == ")":
            depth -= 1
            if depth == 0:
                closes_at_end = index == len(value) - 1
                if not closes_at_end:
                    break
        index += 1
    if len(value) >= 2 and value[0] == "(" and value[-1] == ")" and depth == 0 and closes_at_end:
        value = value[1:-1].strip()
    out, quoted, pending_space, i = [], False, False, 0
    while i < len(value):
        ch = value[i]
        if ch == "'":
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(ch)
            if quoted and i + 1 < len(value) and value[i + 1] == "'":
                out.append("'"); i += 1
            else:
                quoted = not quoted
        elif ch.isspace() and not quoted:
            pending_space = True
        else:
            if pending_space and out:
                out.append(" ")
            pending_space = False
            out.append(ch if quoted else ch.lower())
        i += 1
    result = "".join(out)
    result = "now()" if result == "current_timestamp" else result
    numeric_literal = re.fullmatch(r"'([+-]?(?:\d+(?:\.\d*)?|\.\d+))'", result)
    return numeric_literal[1] if numeric_literal else result


def schema_from_models(models=None):
    if models is None:
        from pyzzel.core.base import Base
        models = Base._registry
    tables = {}
    for table, model in models.items():
        columns = {}
        for spec in model._column_specs():
            sql_type = canonical_type(spec.sql_type)
            columns[spec.name] = {
                "type": sql_type, "nullable": False if spec.primary_key else spec.nullable,
                "primary_key": spec.primary_key, "identity": spec.identity, "unique": spec.unique,
                "default": spec.default,
                "references": list(spec.foreign_key) if spec.foreign_key else None,
                "on_delete": spec.on_delete if spec.foreign_key else None,
            }
        indexes = [column.name for column in model._columns.values() if column.index]
        tables[table] = {"columns": columns, "indexes": indexes}
    return {"version": 1, "tables": tables}


def load_snapshot(path):
    path = Path(path)
    if not path.exists():
        return {"version": 1, "tables": {}}
    with path.open(encoding="utf-8") as stream:
        snapshot = json.load(stream)
    if snapshot.get("version") != 1:
        raise MigrationError(f"Unsupported snapshot version: {snapshot.get('version')!r}")
    return snapshot


def save_snapshot(path, snapshot):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    content = json.dumps(snapshot, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
