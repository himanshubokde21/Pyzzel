"""Separate server plans, client execution, parameter adaptation, and chunking costs."""

from __future__ import annotations

import json
import math
import os
import platform
import statistics
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.adapt import PyFormat, Transformer

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_create_many import (
    Base,
    ProfileRow,
    TABLE,
    build_insert_many,
    clear_table,
    database_environment,
    input_rows,
    prepare,
    validate_profile_schema,
)
from pyzzel import connect, disconnect
from pyzzel.pool import connection


SIZES = (10, 100, 1000, 5000)
ITERATIONS = int(os.environ.get("PG_PROFILE_ITERATIONS", "20"))
WARMUPS = int(os.environ.get("PG_PROFILE_WARMUPS", "3"))
EXPLAIN_PREFIX = "EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) "


class _RollbackProbe(Exception):
    pass


def summarize(samples):
    ordered = sorted(samples)
    return {
        "samples_ms": samples,
        "median_ms": statistics.median(samples),
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "min_ms": min(samples),
        "max_ms": max(samples),
    }


def custom_chunk_statements(rows, chunk_rows, returning=("id",)):
    base = prepare(rows)
    columns = tuple(("name", "age", "active"))
    width = len(columns)
    values = [
        row
        for statement in base
        for row in (statement.params[offset : offset + width]
                    for offset in range(0, len(statement.params), width))
    ]
    return [
        build_insert_many(TABLE, columns, values[offset : offset + chunk_rows], returning=returning)
        for offset in range(0, len(values), chunk_rows)
    ]


def returning_shape_probe(rows, chunk_rows, returning):
    return custom_chunk_statements(rows, chunk_rows, returning=returning)


def extract_plan_metrics(raw):
    if isinstance(raw, str):
        raw = json.loads(raw)
    document = raw[0]
    nodes = []

    def visit(node):
        nodes.append(node)
        for child in node.get("Plans", ()):
            visit(child)

    visit(document["Plan"])
    jit = document.get("JIT", {}).get("Timing", {})
    return {
        "planning_time_ms": document.get("Planning Time"),
        "execution_time_ms": document.get("Execution Time"),
        "actual_rows_reported": document["Plan"].get("Actual Rows"),
        "shared_hit_blocks": sum(node.get("Shared Hit Blocks", 0) for node in nodes),
        "shared_read_blocks": sum(node.get("Shared Read Blocks", 0) for node in nodes),
        "wal_records": sum(node.get("WAL Records", 0) for node in nodes),
        "wal_bytes": sum(node.get("WAL Bytes", 0) for node in nodes),
        "jit_total_ms": jit.get("Total"),
    }


def server_explain(statements, iterations, warmups):
    samples = []
    plans = []
    for index in range(iterations + warmups):
        clear_table()
        planning = execution = 0.0
        actual_rows = wal_bytes = wal_records = 0
        jit_total = 0.0
        client_started = time.perf_counter_ns()
        try:
            with connection() as conn:
                with conn.transaction():
                    for statement in statements:
                        result = conn.execute(EXPLAIN_PREFIX + statement.sql, statement.params)
                        metrics = extract_plan_metrics(result.fetchone()[0])
                        planning += metrics["planning_time_ms"] or 0.0
                        execution += metrics["execution_time_ms"] or 0.0
                        actual_rows += metrics["actual_rows_reported"] or 0
                        wal_bytes += metrics["wal_bytes"]
                        wal_records += metrics["wal_records"]
                        jit_total += metrics["jit_total_ms"] or 0.0
                    raise _RollbackProbe()
        except _RollbackProbe:
            pass
        client_ms = (time.perf_counter_ns() - client_started) / 1_000_000
        plans.append({"planning_time_ms": planning, "execution_time_ms": execution,
                      "actual_rows_reported": actual_rows, "wal_bytes": wal_bytes,
                      "wal_records": wal_records, "jit_total_ms": jit_total,
                      "explain_client_total_ms": client_ms})
        if index >= warmups:
            samples.append(plans[-1])
    return {
        "server_planning_ms": summarize([sample["planning_time_ms"] for sample in samples]),
        "server_execution_ms": summarize([sample["execution_time_ms"] for sample in samples]),
        "explain_client_total_ms": summarize([sample["explain_client_total_ms"] for sample in samples]),
        "wal_bytes_samples": [sample["wal_bytes"] for sample in samples],
        "wal_records_samples": [sample["wal_records"] for sample in samples],
        "jit_total_ms_samples": [sample["jit_total_ms"] for sample in samples],
        "actual_rows_reported_samples": [sample["actual_rows_reported"] for sample in samples],
        "statement_count": len(statements),
    }


def client_insert(statements, expected_rows, iterations, warmups, prepare=False):
    components = {"execute_returning_ms": [], "fetch_rows_ms": [], "extract_ids_ms": [], "transaction_total_ms": []}
    backend_pids = []
    for index in range(iterations + warmups):
        clear_table()
        with connection() as conn:
            backend_pids.append(conn.info.backend_pid)
            total_started = time.perf_counter_ns()
            with conn.transaction():
                execute_ms = fetch_ms = ids_ms = 0.0
                ids = []
                for statement in statements:
                    started = time.perf_counter_ns()
                    cursor = conn.execute(statement.sql, statement.params, prepare=prepare)
                    execute_ms += (time.perf_counter_ns() - started) / 1_000_000
                    started = time.perf_counter_ns()
                    returned = cursor.fetchall()
                    fetch_ms += (time.perf_counter_ns() - started) / 1_000_000
                    started = time.perf_counter_ns()
                    ids.extend(row[0] for row in returned)
                    ids_ms += (time.perf_counter_ns() - started) / 1_000_000
                if len(ids) != expected_rows or len(set(ids)) != expected_rows:
                    raise AssertionError("Insert did not return the expected unique generated IDs")
            total_ms = (time.perf_counter_ns() - total_started) / 1_000_000
        count = Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0]
        if count != expected_rows:
            raise AssertionError(f"Database persisted {count} rows; expected {expected_rows}")
        if index >= warmups:
            components["execute_returning_ms"].append(execute_ms)
            components["fetch_rows_ms"].append(fetch_ms)
            components["extract_ids_ms"].append(ids_ms)
            components["transaction_total_ms"].append(total_ms)
    if len(set(backend_pids)) != 1:
        raise RuntimeError(f"Pool backend changed during measurements: {sorted(set(backend_pids))}")
    return {name: summarize(samples) for name, samples in components.items()} | {
        "backend_pid": backend_pids[0], "statement_count": len(statements),
        "psycopg_prepare": prepare,
    }


def make_values_statement(statements, row_count):
    flat_params = tuple(value for statement in statements for value in statement.params)
    tuple_sql = "(" + ", ".join(("%s::text", "%s::integer", "%s::boolean")) + ")"
    values_sql = ", ".join(tuple_sql for _ in range(row_count))
    sql = f"SELECT count(*) FROM (VALUES {values_sql}) AS supplied(name, age, active)"
    return sql, flat_params


def values_control(sql, flat_params, row_count, iterations, warmups):
    execute_samples = []
    total_samples = []
    for index in range(iterations + warmups):
        with connection() as conn:
            started_total = time.perf_counter_ns()
            with conn.transaction():
                started = time.perf_counter_ns()
                count = conn.execute(sql, flat_params).fetchone()[0]
                execute_ms = (time.perf_counter_ns() - started) / 1_000_000
                if count != row_count:
                    raise AssertionError(f"VALUES transport control counted {count} rows")
            total_ms = (time.perf_counter_ns() - started_total) / 1_000_000
        if index >= warmups:
            execute_samples.append(execute_ms)
            total_samples.append(total_ms)
    return {
        "execute_and_single_result_ms": summarize(execute_samples),
        "transaction_total_ms": summarize(total_samples),
        "sql_bytes_utf8": len(sql.encode("utf-8")),
        "parameter_count": len(flat_params),
        "returned_rows": 1,
        "note": "Control sends the same values and parameter count but constructs/scans VALUES and returns one aggregate row; it is diagnostic, not equivalent INSERT work.",
    }


def values_server_plan(sql, flat_params, iterations, warmups):
    samples = []
    for index in range(iterations + warmups):
        with connection() as conn:
            with conn.transaction():
                result = conn.execute(
                    "EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) " + sql, flat_params
                )
                metrics = extract_plan_metrics(result.fetchone()[0])
        if index >= warmups:
            samples.append(metrics)
    return {
        "server_planning_ms": summarize([row["planning_time_ms"] for row in samples]),
        "server_execution_ms": summarize([row["execution_time_ms"] for row in samples]),
        "jit_total_ms_samples": [row["jit_total_ms"] for row in samples],
        "note": "EXPLAIN ANALYZE planning/execution for the VALUES control only; includes query parsing costs outside the reported Planning Time only in the client wall time, and uses EXPLAIN instrumentation.",
    }


def adaptation_profile(statements, iterations, warmups):
    with connection() as conn:
        formats = [[PyFormat.AUTO] * len(statement.params) for statement in statements]
        samples = []
        bytes_out = 0
        for index in range(iterations + warmups):
            started = time.perf_counter_ns()
            adapted = [Transformer(conn).dump_sequence(statement.params, fmt)
                       for statement, fmt in zip(statements, formats)]
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            bytes_out = sum(len(value) for chunk in adapted for value in chunk if value is not None)
            if index >= warmups:
                samples.append(elapsed)
    return {
        "psycopg_transformer_dump_sequence_ms": summarize(samples),
        "adapted_parameter_payload_bytes": bytes_out,
        "note": "Psycopg Transformer serialization only; excludes SQL placeholder processing, socket transfer, PostgreSQL, and result handling.",
    }


def roundtrip_baseline(iterations, warmups):
    samples = []
    with connection() as conn:
        for index in range(iterations + warmups):
            started = time.perf_counter_ns()
            row = conn.execute("SELECT 1").fetchone()
            elapsed = (time.perf_counter_ns() - started) / 1_000_000
            if row != (1,):
                raise AssertionError("SELECT 1 baseline returned an unexpected result")
            if index >= warmups:
                samples.append(elapsed)
    return summarize(samples)


def main():
    if not os.environ.get("PYZZEL_TEST_DATABASE_URL"):
        raise SystemExit("Set PYZZEL_TEST_DATABASE_URL to the dedicated disposable PostgreSQL test database.")
    connect(os.environ["PYZZEL_TEST_DATABASE_URL"], min_size=1, max_size=1, prepare_threshold=None)
    try:
        validate_profile_schema()
        env = database_environment()
        with connection() as conn:
            extensions = [row[0] for row in conn.execute(
                "SELECT extname FROM pg_extension ORDER BY extname"
            ).fetchall()]
            settings = dict(conn.execute(
                """SELECT name, setting FROM pg_settings
                   WHERE name IN ('track_io_timing', 'track_wal_io_timing', 'wal_level',
                                  'fsync', 'full_page_writes', 'synchronous_commit')"""
            ).fetchall())
            has_pgss = "pg_stat_statements" in extensions
            pgss_preload = conn.execute(
                "SELECT 'pg_stat_statements' = ANY(string_to_array(current_setting('shared_preload_libraries'), ','))"
            ).fetchone()[0]
            backend_pid = conn.info.backend_pid

        report = {
            "profile": "Pyzzel create_many PostgreSQL vs client analysis",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "configuration": {"iterations": ITERATIONS, "warmups": WARMUPS, "sizes": list(SIZES),
                              "table": TABLE, "pool_min": 1, "pool_max": 1, "prepare_threshold": None},
            "environment": {"python": sys.version, "platform": platform.platform(),
                            "pyzzel": "0.0.1", "psycopg": psycopg.__version__,
                            **env, "backend_pid": backend_pid, "extensions": extensions,
                            "pg_stat_statements_extension_installed": has_pgss,
                            "pg_stat_statements_preloaded": pgss_preload,
                            "postgres_settings": settings},
            "method_notes": {
                "explain": "EXPLAIN ANALYZE executes the INSERT inside an explicit transaction that is rolled back. Its server Execution Time excludes normal transmission of all RETURNING rows and includes EXPLAIN instrumentation overhead.",
                "execute": "Psycopg conn.execute duration includes client parameter adaptation, query protocol, PostgreSQL work, and response reception; not pure server time.",
                "values_control": "Sends the same bind values but executes a read-only VALUES/count query; diagnostic only.",
                "roundtrip_baseline": "SELECT 1 establishes a small-query round-trip baseline; it is not a pure network measurement.",
                "chunking": "Only test-profile statement boundaries vary; transaction, data, schema, RETURNING ids, and generated-ID checks remain the same.",
            },
            "roundtrip_select_1": roundtrip_baseline(ITERATIONS, WARMUPS),
            "sizes": [],
        }

        for size in SIZES:
            rows = input_rows(size)
            default_statements = prepare(rows)
            values_sql, values_params = make_values_statement(default_statements, size)
            item = {
                "rows": size,
                "default_chunk_rows": 1000,
                "default_statement_count": len(default_statements),
                "default_parameters": sum(len(stmt.params) for stmt in default_statements),
                "default_sql_bytes": sum(len(stmt.sql.encode("utf-8")) for stmt in default_statements),
                "psycopg_parameter_adaptation": adaptation_profile(default_statements, ITERATIONS, WARMUPS),
                "insert_returning_id": client_insert(default_statements, size, ITERATIONS, WARMUPS),
                "insert_explain_analyze": server_explain(default_statements, ITERATIONS, WARMUPS),
                "values_transport_control": values_control(values_sql, values_params, size, ITERATIONS, WARMUPS),
                "values_control_explain_analyze": values_server_plan(values_sql, values_params, ITERATIONS, WARMUPS),
                "chunking_variants": [],
                "prepared_statement_variants": [],
                "returning_shape_variants": [],
            }

            if size in (1000, 5000):
                item["prepared_statement_variants"].append({
                    "prepare": True,
                    "client_insert_returning_id": client_insert(
                        default_statements, size, ITERATIONS, WARMUPS, prepare=True
                    ),
                    "note": "Diagnostic psycopg server-prepared execution. Warm-ups establish preparation; production default remains prepare_threshold=None.",
                })
                full_columns = ("id", "name", "age", "active", "version")
                full_returning = returning_shape_probe(rows, 1000, full_columns)
                item["returning_shape_variants"].append({
                    "returning_columns": list(full_columns),
                    "statement_count": len(full_returning),
                    "client_insert": client_insert(full_returning, size, ITERATIONS, WARMUPS),
                    "note": "Diagnostic payload-size comparison. Same inserts and generated IDs, but returns every model column instead of only id.",
                })

            chunk_sizes = [1000, 500] if size == 1000 else ([1000, 500, 250, 5000] if size == 5000 else [1000])
            for chunk_rows in chunk_sizes:
                statements = custom_chunk_statements(rows, chunk_rows)
                variant = {
                    "chunk_rows": chunk_rows,
                    "statement_count": len(statements),
                    "parameter_count": sum(len(stmt.params) for stmt in statements),
                    "sql_bytes_utf8": sum(len(stmt.sql.encode("utf-8")) for stmt in statements),
                    "client_insert_returning_id": client_insert(statements, size, ITERATIONS, WARMUPS),
                }
                if size == 5000 and chunk_rows == 5000:
                    variant["server_explain_analyze"] = server_explain(statements, ITERATIONS, WARMUPS)
                item["chunking_variants"].append(variant)

            clear_table()
            item["rows_after_final_cleanup"] = Base.execute(
                f'SELECT count(*) FROM "{TABLE}"'
            )[0][0]
            report["sizes"].append(item)

        output = Path(__file__).with_name("create_many_server_analysis.json")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Server analysis saved to {output.resolve()}")
    finally:
        disconnect()


if __name__ == "__main__":
    main()
