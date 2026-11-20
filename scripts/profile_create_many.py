"""Profile Pyzzel create_many() without changing its implementation."""

from __future__ import annotations

import argparse
import json
import math
import os
import platform
import statistics
import sys
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

from pyzzel import Base, Boolean, Column, Integer, String, __version__, connect, disconnect
from pyzzel.core.base import build_insert_many
from pyzzel.pool import connection


TABLE = "pyzzel_test_create_many_profile"
SIZES = (10, 100, 1000, 5000)


class ProfileRow(Base):
    __tablename__ = TABLE
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False)
    age = Column(Integer, nullable=False)
    active = Column(Boolean, nullable=False)


def input_rows(size: int) -> list[dict]:
    return [
        {"name": f"profile-{index:05}", "age": index % 100, "active": index % 2 == 0}
        for index in range(size)
    ]


def materialize(rows):
    return list(rows)


def validate_and_apply_defaults(rows):
    """Mirror the validation/default portion of Base.create_many()."""
    if any(not isinstance(row, dict) for row in rows):
        raise TypeError("create_many() rows must be dictionaries")
    copied = [dict(row) for row in rows]
    for row in copied:
        for name, default in ProfileRow._defaults:
            if name not in row:
                row[name] = default() if callable(default) else default
    keys = tuple(copied[0]) if copied else ()
    for row in copied:
        if set(row) != set(keys):
            raise ValueError("All rows must have the same columns")
        for name in keys:
            if name not in ProfileRow._columns:
                raise ValueError(f"Unknown column {name!r}")
            if name in (ProfileRow._pk_name, "version"):
                raise ValueError(f"Column {name!r} cannot be set by create_many()")
    return copied, keys


def convert_values(rows, keys):
    return [
        tuple(ProfileRow._all_columns[name].to_db(row[name]) for name in keys)
        for row in rows
    ]


def build_statements(rows, keys):
    chunk_size = min(1000, 65535 // len(keys))
    return [
        build_insert_many(TABLE, keys, rows[offset : offset + chunk_size], returning=[ProfileRow._pk_name])
        for offset in range(0, len(rows), chunk_size)
    ]


def prepare(rows):
    copied_input = materialize(rows)
    validated, keys = validate_and_apply_defaults(copied_input)
    chunk_size = min(1000, 65535 // len(keys))
    statements = []
    for offset in range(0, len(validated), chunk_size):
        converted = convert_values(validated[offset : offset + chunk_size], keys)
        statements.append(
            build_insert_many(TABLE, keys, converted, returning=[ProfileRow._pk_name])
        )
    return statements


def percentile95(samples):
    return sorted(samples)[math.ceil(0.95 * len(samples)) - 1]


def summarize(samples):
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "p95_ms": percentile95(samples),
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def timed_samples(iterations, warmups, operation):
    samples = []
    for index in range(warmups + iterations):
        started = time.perf_counter_ns()
        operation()
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if index >= warmups:
            samples.append(elapsed)
    return summarize(samples)


def python_stage_profile(rows, iterations, warmups):
    stages = {}

    def run_stage(label, setup, action):
        samples = []
        for index in range(warmups + iterations):
            state = setup()
            started = time.perf_counter_ns()
            action(state)
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if index >= warmups:
                samples.append(elapsed)
        stages[label] = summarize(samples)

    run_stage("input_materialization", lambda: rows, materialize)
    run_stage("validation_and_default_application", lambda: rows, validate_and_apply_defaults)
    validated, keys = validate_and_apply_defaults(rows)
    run_stage("value_conversion_to_db", lambda: (validated, keys), lambda state: convert_values(*state))
    converted = convert_values(validated, keys)
    run_stage("sql_construction_and_parameter_preparation", lambda: (converted, keys), lambda state: build_statements(*state))

    all_samples = []
    for index in range(warmups + iterations):
        started = time.perf_counter_ns()
        prepare(rows)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if index >= warmups:
            all_samples.append(elapsed)
    stages["python_only_preparation_total"] = summarize(all_samples)
    return stages


def memory_profile(rows):
    tracemalloc.start()
    try:
        statements = prepare(rows)
        current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return {
        "tracemalloc_current_bytes_after_preparation": current,
        "tracemalloc_peak_bytes_during_preparation": peak,
        "sql_bytes_utf8": sum(len(statement.sql.encode("utf-8")) for statement in statements),
        "parameter_count": sum(len(statement.params) for statement in statements),
    }


def clear_table():
    Base.execute(f'TRUNCATE TABLE "{TABLE}" RESTART IDENTITY')


def validate_profile_schema():
    expected = [
        ("id", "integer", "NO", None, "YES", "ALWAYS"),
        ("name", "text", "NO", None, "NO", None),
        ("age", "integer", "NO", None, "NO", None),
        ("active", "boolean", "NO", None, "NO", None),
        ("version", "integer", "NO", "1", "NO", None),
    ]
    with connection() as conn:
        actual = conn.execute(
            """SELECT column_name, data_type, is_nullable, column_default,
                      is_identity, identity_generation
               FROM information_schema.columns
               WHERE table_schema = 'public' AND table_name = %s
               ORDER BY ordinal_position""",
            (TABLE,),
        ).fetchall()
        constraints = conn.execute(
            """SELECT contype, array_agg(a.attname ORDER BY k.ordinality)
               FROM pg_constraint c
               JOIN pg_class t ON t.oid = c.conrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
               JOIN unnest(c.conkey) WITH ORDINALITY k(attnum, ordinality) ON true
               JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
               WHERE n.nspname = 'public' AND t.relname = %s
               GROUP BY c.oid, c.contype ORDER BY c.contype""",
            (TABLE,),
        ).fetchall()
        indexes = conn.execute(
            """SELECT i.indisprimary, i.indisunique,
                      array_agg(a.attname ORDER BY k.ordinality)
               FROM pg_index i
               JOIN pg_class t ON t.oid = i.indrelid
               JOIN pg_namespace n ON n.oid = t.relnamespace
               JOIN unnest(i.indkey) WITH ORDINALITY k(attnum, ordinality)
                    ON k.ordinality <= i.indnkeyatts
               JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum
               WHERE n.nspname = 'public' AND t.relname = %s
               GROUP BY i.indexrelid, i.indisprimary, i.indisunique
               ORDER BY i.indisprimary DESC""",
            (TABLE,),
        ).fetchall()
    if actual != expected or constraints != [("p", ["id"])] or indexes != [(True, True, ["id"])]:
        raise RuntimeError(
            f"Profiling table schema differs from canonical schema: {actual!r}, {constraints!r}, {indexes!r}"
        )


def database_profile(statements, rows_count, iterations, warmups):
    result = {"database_execution_and_returning": [], "fetch_returned_rows": [], "extract_ids": [], "transaction_total": []}
    with connection() as conn:
        backend_pid = conn.info.backend_pid
    sample_backend_pids = []
    for index in range(warmups + iterations):
        clear_table()  # Deliberately excluded from every timed interval.
        with connection() as conn:
            sample_backend_pids.append(conn.info.backend_pid)
            total_started = time.perf_counter_ns()
            with conn.transaction():
                execute_ms = fetch_ms = ids_ms = 0.0
                ids = []
                for statement in statements:
                    started = time.perf_counter_ns()
                    cursor = conn.execute(statement.sql, statement.params)
                    execute_ms += (time.perf_counter_ns() - started) / 1_000_000

                    started = time.perf_counter_ns()
                    returned_rows = cursor.fetchall()
                    fetch_ms += (time.perf_counter_ns() - started) / 1_000_000

                    started = time.perf_counter_ns()
                    ids.extend(row[0] for row in returned_rows)
                    ids_ms += (time.perf_counter_ns() - started) / 1_000_000

                if len(ids) != rows_count or len(set(ids)) != rows_count:
                    raise AssertionError("Database insert returned an unexpected ID set")
            total_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        if index >= warmups:
            result["database_execution_and_returning"].append(execute_ms)
            result["fetch_returned_rows"].append(fetch_ms)
            result["extract_ids"].append(ids_ms)
            result["transaction_total"].append(total_ms)
        actual_count = Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0]
        if actual_count != rows_count:
            raise AssertionError(f"Database persisted {actual_count} rows; expected {rows_count}")
    if set(sample_backend_pids) != {backend_pid}:
        raise RuntimeError(f"Database profiling did not reuse one backend: {sorted(set(sample_backend_pids))}")
    return {
        "database_execution_and_returning": summarize(result["database_execution_and_returning"]),
        "fetch_returned_rows": summarize(result["fetch_returned_rows"]),
        "extract_ids": summarize(result["extract_ids"]),
        "transaction_total": summarize(result["transaction_total"]),
        "backend_pid": backend_pid,
        "statement_count": len(statements),
    }


def api_profile(rows, iterations, warmups):
    samples = []
    backend_pids = []
    for index in range(warmups + iterations):
        clear_table()
        started = time.perf_counter_ns()
        ids = ProfileRow.create_many(rows)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if len(ids) != len(rows) or len(set(ids)) != len(rows):
            raise AssertionError("create_many() returned an unexpected ID set")
        if index >= warmups:
            samples.append(elapsed)
        with connection() as conn:
            backend_pids.append(conn.info.backend_pid)
        actual_count = Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0]
        if actual_count != len(rows):
            raise AssertionError(f"create_many() persisted {actual_count} rows; expected {len(rows)}")
    unique_pids = sorted(set(backend_pids))
    if len(unique_pids) != 1:
        raise RuntimeError(f"create_many() did not reuse one backend: {unique_pids}")
    return {**summarize(samples), "backend_pids": unique_pids}


def database_environment():
    with connection() as conn:
        rows = conn.execute(
            """SELECT current_database(), current_schema(), current_setting('server_version'),
                      current_setting('server_version_num'), current_setting('client_encoding'),
                      current_setting('transaction_isolation'), current_setting('synchronous_commit'),
                      current_setting('jit'), current_setting('shared_buffers'),
                      current_setting('work_mem'), current_setting('effective_cache_size'),
                      pg_backend_pid()"""
        ).fetchone()
    keys = ("database", "schema", "server_version", "server_version_num", "client_encoding",
            "transaction_isolation", "synchronous_commit", "jit", "shared_buffers", "work_mem",
            "effective_cache_size", "backend_pid")
    return dict(zip(keys, rows))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--iterations", type=int, default=20)
    parser.add_argument("--warmups", type=int, default=3)
    parser.add_argument("--python-only", action="store_true", help="Run preparation stages without PostgreSQL")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.iterations < 1 or args.warmups < 0:
        parser.error("iterations must be positive and warmups cannot be negative")
    if not args.python_only and not os.environ.get("PYZZEL_TEST_DATABASE_URL"):
        parser.error("Set PYZZEL_TEST_DATABASE_URL to the dedicated PostgreSQL test database, or use --python-only")

    report = {
        "profile": "Pyzzel Base.create_many",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "configuration": {"iterations": args.iterations, "warmups": args.warmups,
                          "sizes": list(SIZES), "table": TABLE, "python_only": args.python_only},
        "environment": {"python": sys.version, "platform": platform.platform(), "pyzzel": __version__},
        "method_notes": {
            "database_execution_and_returning": "conn.execute timing includes PostgreSQL execution, driver adaptation/protocol, and response transfer; it is not pure server execution time.",
            "fetch_returned_rows": "Measures cursor fetch/decode after conn.execute returns.",
            "transaction_total": "Includes transaction context and commit, but excludes table truncation and connection checkout.",
            "python_only_preparation_total": "Runs create_many preparation steps without database execution.",
            "memory": "tracemalloc is measured separately because it distorts latency.",
            "stage_samples": "Each Python stage is timed independently with its prerequisites prepared outside that stage timer.",
        },
        "sizes": [],
    }

    database_open = False
    try:
        if not args.python_only:
            connect(os.environ["PYZZEL_TEST_DATABASE_URL"], min_size=1, max_size=1, prepare_threshold=None)
            database_open = True
            report["environment"].update(database_environment())
            if report["environment"]["schema"] != "public":
                raise RuntimeError("Profiling table requires current_schema() to be public, matching the benchmark schema.")
            Base.create_tables()
            validate_profile_schema()
            import psycopg

            report["environment"].update({"psycopg": psycopg.__version__, "pool_min": 1, "pool_max": 1})
        for size in SIZES:
            rows = input_rows(size)
            statements = prepare(rows)
            item = {
                "rows": size,
                "parameter_count": sum(len(statement.params) for statement in statements),
                "sql_bytes_utf8": sum(len(statement.sql.encode("utf-8")) for statement in statements),
                "statement_count": len(statements),
                "python_stages": python_stage_profile(rows, args.iterations, args.warmups),
                "memory": memory_profile(rows),
            }
            if not args.python_only:
                item["prepared_database_execution"] = database_profile(
                    statements, size, args.iterations, args.warmups
                )
                clear_table()
                item["complete_create_many_api"] = api_profile(
                    rows, args.iterations, args.warmups
                )
                if item["complete_create_many_api"]["backend_pids"] != [
                    item["prepared_database_execution"]["backend_pid"]
                ]:
                    raise RuntimeError("Prepared and complete API measurements used different pool backends.")
                clear_table()
                item["database_rows_after_cleanup"] = Base.execute(
                    f'SELECT count(*) FROM "{TABLE}"'
                )[0][0]
            report["sizes"].append(item)
    finally:
        if database_open:
            disconnect()

    serialized = json.dumps(report, indent=2) + "\n"
    if args.output is not None and str(args.output) == "-":
        print(serialized, end="")
        return
    output = args.output or Path(__file__).with_name("results") / (
        "create_many_profile_" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + ".json"
    )
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(serialized, encoding="utf-8")
    print(f"Profile results saved to {output.resolve()}")


if __name__ == "__main__":
    main()
