"""Profile Psycopg's create_many execution path without changing Pyzzel."""

from __future__ import annotations

import json
import math
import os
import platform
import random
import re
import statistics
import sys
import tempfile
import time
import tracemalloc
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from psycopg.adapt import Transformer
from psycopg._queries import (
    MAX_CACHED_STATEMENT_LENGTH,
    MAX_CACHED_STATEMENT_PARAMS,
    PostgresQuery,
    _query2pg_nocache,
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from profile_create_many import (  # noqa: E402
    Base,
    ProfileRow,
    TABLE,
    build_insert_many,
    clear_table,
    database_environment,
    input_rows,
    validate_profile_schema,
)
from pyzzel import connect, disconnect  # noqa: E402
from pyzzel.pool import connection  # noqa: E402


ROWS = 5000
CHUNK_SIZES = (100, 250, 500, 1000, 2000, 5000)
ADAPTATION_BATCH_SIZES = (100, 500, 1000, 2000, 5000)
ITERATIONS = int(os.environ.get("PG_PROFILE_ITERATIONS", "20"))
WARMUPS = int(os.environ.get("PG_PROFILE_WARMUPS", "3"))
SEED = 5142026
ACTIVE_CONVERSION_SAMPLES: list[float] | None = None


class TimedCursor(psycopg.Cursor):
    """Time Psycopg's real query conversion call within Cursor.execute()."""

    def _convert_query(self, query, params=None):
        started = time.perf_counter_ns()
        result = super()._convert_query(query, params)
        elapsed = (time.perf_counter_ns() - started) / 1_000_000
        if ACTIVE_CONVERSION_SAMPLES is not None:
            ACTIVE_CONVERSION_SAMPLES.append(elapsed)
        return result


class _RollbackProbe(Exception):
    pass


def summarize(samples):
    ordered = sorted(samples)
    median = statistics.median(samples)
    deviations = [abs(value - median) for value in samples]
    return {
        "samples_ms": samples,
        "median_ms": median,
        "p95_ms": ordered[math.ceil(0.95 * len(ordered)) - 1],
        "min_ms": min(samples),
        "max_ms": max(samples),
        "mad_ms": statistics.median(deviations),
        "coefficient_of_variation_percent": (
            statistics.pstdev(samples) / statistics.mean(samples) * 100
            if statistics.mean(samples)
            else 0.0
        ),
    }


def build_statement(rows, offset, chunk_size):
    columns = ("name", "age", "active")
    chunk = rows[offset : offset + chunk_size]
    params = [
        tuple(ProfileRow._all_columns[name].to_db(row[name]) for name in columns)
        for row in chunk
    ]
    return build_insert_many(TABLE, columns, params, returning=["id"])


def statements_for(rows, chunk_size):
    statements = []
    for offset in range(0, len(rows), chunk_size):
        statements.append(build_statement(rows, offset, chunk_size))
    return statements


def profile_conversion_batches(rows):
    result = []
    for batch_size in ADAPTATION_BATCH_SIZES:
        statements = statements_for(rows[:batch_size], batch_size)
        statement = statements[0]
        query_bytes = statement.sql.encode("utf-8")
        parameter_count = len(statement.params)
        use_psycopg_cache = (
            len(query_bytes) <= MAX_CACHED_STATEMENT_LENGTH
            and parameter_count <= MAX_CACHED_STATEMENT_PARAMS
        )

        with connection() as conn:
            encoding = conn.info.encoding
            query_rewrite_samples = []
            adaptation_samples = []
            total_conversion_samples = []
            transformed_bytes = 0
            for index in range(WARMUPS + ITERATIONS):
                started = time.perf_counter_ns()
                _, wanted_formats, _, _ = _query2pg_nocache(query_bytes, encoding)
                rewrite_ms = (time.perf_counter_ns() - started) / 1_000_000

                transformer = Transformer(conn)
                started = time.perf_counter_ns()
                adapted = transformer.dump_sequence(statement.params, wanted_formats)
                adaptation_ms = (time.perf_counter_ns() - started) / 1_000_000

                started = time.perf_counter_ns()
                pg_query = PostgresQuery(Transformer(conn))
                pg_query.convert(statement.sql, statement.params)
                total_ms = (time.perf_counter_ns() - started) / 1_000_000
                transformed_bytes = sum(len(value) for value in adapted if value is not None)

                if index >= WARMUPS:
                    query_rewrite_samples.append(rewrite_ms)
                    adaptation_samples.append(adaptation_ms)
                    total_conversion_samples.append(total_ms)

        result.append(
            {
                "rows": batch_size,
                "parameter_count": parameter_count,
                "sql_bytes_utf8": len(query_bytes),
                "psycopg_query_conversion_cache_used_by_thresholds": use_psycopg_cache,
                "psycopg_query_cache_limits": {
                    "max_sql_bytes": MAX_CACHED_STATEMENT_LENGTH,
                    "max_parameters": MAX_CACHED_STATEMENT_PARAMS,
                },
                "placeholder_rewrite_only_private_path": summarize(query_rewrite_samples),
                "standalone_parameter_adaptation_only": summarize(adaptation_samples),
                "actual_PostgresQuery_convert_path": summarize(total_conversion_samples),
                "adapted_parameter_payload_bytes": transformed_bytes,
                "note": "PostgresQuery.convert() is the exact conversion method called by Cursor.execute(), measured separately. Its timing includes SQL placeholder conversion and Psycopg adaptation but not socket/protocol exchange. Private Psycopg 3.3.9 internals are used for stage detail.",
            }
        )
    return result


def profile_chunk_variants(rows):
    variants = []
    order_log = []
    rng = random.Random(SEED)
    configs = list(CHUNK_SIZES)
    samples = {size: [] for size in configs}
    conversion_samples = {size: [] for size in configs}
    conversion_per_statement = {size: [] for size in configs}
    return_order_checks = {size: [] for size in configs}
    backend_pids = set()

    for round_index in range(WARMUPS + ITERATIONS):
        order = configs.copy()
        rng.shuffle(order)
        order_log.append(order)
        for chunk_size in order:
            statements = statements_for(rows, chunk_size)
            clear_table()
            with connection() as conn:
                backend_pids.add(conn.info.backend_pid)
                original_factory = conn.cursor_factory
                conn.cursor_factory = TimedCursor
                global ACTIVE_CONVERSION_SAMPLES
                current_conversion_samples: list[float] = []
                ACTIVE_CONVERSION_SAMPLES = current_conversion_samples
                ids = []
                execute_ms = 0.0
                started_total = time.perf_counter_ns()
                try:
                    with conn.transaction():
                        for statement in statements:
                            started = time.perf_counter_ns()
                            cursor = conn.execute(statement.sql, statement.params)
                            execute_ms += (time.perf_counter_ns() - started) / 1_000_000
                            ids.extend(row[0] for row in cursor.fetchall())
                    transaction_ms = (time.perf_counter_ns() - started_total) / 1_000_000
                finally:
                    ACTIVE_CONVERSION_SAMPLES = None
                    conn.cursor_factory = original_factory

            expected_ids = list(range(1, ROWS + 1))
            if len(ids) != ROWS or len(set(ids)) != ROWS or sorted(ids) != expected_ids:
                raise AssertionError(f"chunk={chunk_size}: wrong generated ID set")
            return_order_checks[chunk_size].append(ids == expected_ids)
            count = Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0]
            if count != ROWS:
                raise AssertionError(f"chunk={chunk_size}: persisted {count} rows, expected {ROWS}")

            if round_index >= WARMUPS:
                samples[chunk_size].append(
                    {"execute_returning_ms": execute_ms, "transaction_total_ms": transaction_ms}
                )
                conversion_samples[chunk_size].append(sum(current_conversion_samples))
                conversion_per_statement[chunk_size].append(current_conversion_samples)

    if len(backend_pids) != 1:
        raise RuntimeError(f"Connection backend changed: {sorted(backend_pids)}")

    for chunk_size in configs:
        statements = statements_for(rows, chunk_size)
        execute_values = [sample["execute_returning_ms"] for sample in samples[chunk_size]]
        transaction_values = [sample["transaction_total_ms"] for sample in samples[chunk_size]]
        variants.append(
            {
                "chunk_rows": chunk_size,
                "statement_count": len(statements),
                "parameters_per_statement": [len(statement.params) for statement in statements],
                "sql_bytes_utf8_per_statement": [len(statement.sql.encode("utf-8")) for statement in statements],
                "total_parameter_count": sum(len(statement.params) for statement in statements),
                "total_sql_bytes_utf8": sum(len(statement.sql.encode("utf-8")) for statement in statements),
                "execute_returning": summarize(execute_values),
                "transaction_total": summarize(transaction_values),
                "Psycopg_PostgresQuery_convert_inside_execute": summarize(conversion_samples[chunk_size]),
                "conversion_ms_per_statement_samples": conversion_per_statement[chunk_size],
                "all_returned_ids_unique_and_complete": True,
                "returning_id_order_matches_insert_input_samples": return_order_checks[chunk_size],
                "returning_id_order_match_count": sum(return_order_checks[chunk_size]),
            }
        )

    return {
        "random_seed": SEED,
        "randomized_order_by_round": order_log,
        "warmups_per_configuration": WARMUPS,
        "measurements_per_configuration": ITERATIONS,
        "backend_pid": next(iter(backend_pids)),
        "variants": variants,
    }


def server_explain_statements(statements):
    planning = execution = 0.0
    actual_rows = 0
    client_started = time.perf_counter_ns()
    try:
        with connection() as conn:
            with conn.transaction():
                for statement in statements:
                    row = conn.execute(
                        "EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON) " + statement.sql,
                        statement.params,
                    ).fetchone()[0]
                    document = json.loads(row) if isinstance(row, str) else row
                    plan = document[0]
                    planning += plan.get("Planning Time", 0.0)
                    execution += plan.get("Execution Time", 0.0)
                    actual_rows += plan["Plan"].get("Actual Rows", 0)
                raise _RollbackProbe()
    except _RollbackProbe:
        pass
    return {
        "server_planning_ms": planning,
        "server_execution_ms": execution,
        "actual_rows_reported": actual_rows,
        "explain_client_total_ms": (time.perf_counter_ns() - client_started) / 1_000_000,
    }


def profile_server_chunk_variants(rows):
    rng = random.Random(SEED + 1)
    samples = {size: [] for size in CHUNK_SIZES}
    order_log = []
    configs = list(CHUNK_SIZES)
    for round_index in range(WARMUPS + ITERATIONS):
        order = configs.copy()
        rng.shuffle(order)
        order_log.append(order)
        for chunk_size in order:
            clear_table()
            result = server_explain_statements(statements_for(rows, chunk_size))
            if result["actual_rows_reported"] != ROWS:
                raise AssertionError(
                    f"EXPLAIN reported {result['actual_rows_reported']} rows for chunk={chunk_size}"
                )
            if Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0] != 0:
                raise AssertionError("EXPLAIN rollback left rows in the profiling table")
            if round_index >= WARMUPS:
                samples[chunk_size].append(result)
    return {
        "randomized_order_by_round": order_log,
        "variants": [
            {
                "chunk_rows": chunk_size,
                "server_planning": summarize([sample["server_planning_ms"] for sample in samples[chunk_size]]),
                "server_execution": summarize([sample["server_execution_ms"] for sample in samples[chunk_size]]),
                "explain_client_total": summarize([sample["explain_client_total_ms"] for sample in samples[chunk_size]]),
                "actual_rows_reported_samples": [sample["actual_rows_reported"] for sample in samples[chunk_size]],
                "note": "EXPLAIN ANALYZE insert effects are rolled back. Server Planning/Execution excludes parse/analyze costs not covered by those fields and normal RETURNING row transfer.",
            }
            for chunk_size in configs
        ],
    }


def profile_memory(rows):
    output = []
    for chunk_size in CHUNK_SIZES:
        clear_table()
        tracemalloc.start()
        try:
            with connection() as conn:
                with conn.transaction():
                    ids = []
                    for offset in range(0, ROWS, chunk_size):
                        statement = build_statement(rows, offset, chunk_size)
                        ids.extend(row[0] for row in conn.execute(statement.sql, statement.params).fetchall())
            current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        if len(ids) != ROWS or len(set(ids)) != ROWS:
            raise AssertionError(f"memory probe chunk={chunk_size}: invalid IDs")
        output.append(
            {
                "chunk_rows": chunk_size,
                "statement_count": math.ceil(ROWS / chunk_size),
                "python_current_bytes_after_operation": current,
                "python_peak_bytes_during_statement_build_and_insert": peak,
                "note": "Statements are built and discarded one chunk at a time to approximate create_many's working set. Input row dicts already exist before tracing; validation/copying is excluded. tracemalloc is separate from latency and excludes libpq/PostgreSQL native allocations and process RSS.",
            }
        )
        clear_table()
    return output


def protocol_trace_probes(rows):
    """Summarize libpq protocol trace events without persisting trace contents."""
    probes = []
    for batch_size in (100, 1000, 5000):
        statement = statements_for(rows[:batch_size], batch_size)[0]
        clear_table()
        with connection() as conn:
            with tempfile.TemporaryFile(mode="w+b") as trace_file:
                with conn.transaction():
                    try:
                        conn.pgconn.trace(trace_file.fileno())
                    except psycopg.NotSupportedError as error:
                        return {
                            "available": False,
                            "reason": str(error),
                            "note": "Psycopg/libpq protocol trace is unsupported on this Windows environment; no trace contents or parameter values were captured.",
                        }
                    try:
                        returned = conn.execute(statement.sql, statement.params).fetchall()
                    finally:
                        conn.pgconn.untrace()
                if len(returned) != batch_size or len({row[0] for row in returned}) != batch_size:
                    raise AssertionError(f"Protocol trace probe returned invalid IDs for {batch_size} rows")
                trace_file.seek(0)
                trace_bytes = trace_file.read()

        trace_text = trace_bytes.decode("utf-8", errors="replace")
        frontend_counts = {}
        backend_counts = {}
        reported_lengths = []
        for line in trace_text.splitlines():
            match = re.search(r"\b(FE|BE)(?:=>|<=)\s+([A-Za-z][A-Za-z0-9_]*)", line)
            if not match:
                continue
            direction, message = match.groups()
            counts = frontend_counts if direction == "FE" else backend_counts
            counts[message] = counts.get(message, 0) + 1
            length_match = re.search(r"\blen(?:gth)?\s*[=:]?\s*(\d+)", line, flags=re.IGNORECASE)
            if length_match:
                reported_lengths.append(int(length_match.group(1)))

        probes.append(
            {
                "rows": batch_size,
                "parameter_count": len(statement.params),
                "sql_bytes_utf8": len(statement.sql.encode("utf-8")),
                "frontend_message_counts": frontend_counts,
                "backend_message_counts": backend_counts,
                "trace_reported_lengths": reported_lengths,
                "trace_reported_length_sum": sum(reported_lengths),
                "libpq_trace_text_bytes_not_wire_bytes": len(trace_bytes),
                "note": "Counts and lengths are parsed from libpq's textual PQtrace output. Trace text size is not wire byte count; the raw trace is discarded and synthetic benchmark values are used.",
            }
        )
        clear_table()
    return probes


def run_instrumentation_probes(rows):
    """Collect feature availability and settings, without enabling new DB settings."""
    with connection() as conn:
        settings = dict(
            conn.execute(
                """SELECT name, setting FROM pg_settings
                   WHERE name IN ('log_statement', 'log_min_duration_statement',
                                  'track_io_timing', 'track_wal_io_timing',
                                  'shared_preload_libraries', 'jit')"""
            ).fetchall()
        )
        privileges = {
            "superuser": conn.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user").fetchone()[0],
            "pg_monitor": conn.execute("SELECT pg_has_role(current_user, 'pg_monitor', 'MEMBER')").fetchone()[0],
        }
        extensions = [row[0] for row in conn.execute("SELECT extname FROM pg_extension ORDER BY extname").fetchall()]
    return {"settings": settings, "current_user_privileges": privileges, "extensions": extensions}


def main():
    if not os.environ.get("PYZZEL_TEST_DATABASE_URL"):
        raise SystemExit("Set PYZZEL_TEST_DATABASE_URL to the dedicated disposable PostgreSQL test database.")
    connect(os.environ["PYZZEL_TEST_DATABASE_URL"], min_size=1, max_size=1, prepare_threshold=None)
    try:
        validate_profile_schema()
        rows = input_rows(ROWS)
        env = database_environment()
        instrumentation = run_instrumentation_probes(rows)
        report = {
            "profile": "Pyzzel Phase 5 execution path analysis",
            "created_at_utc": datetime.now(timezone.utc).isoformat(),
            "configuration": {
                "rows": ROWS,
                "chunk_sizes": list(CHUNK_SIZES),
                "adaptation_batch_sizes": list(ADAPTATION_BATCH_SIZES),
                "iterations": ITERATIONS,
                "warmups": WARMUPS,
                "pool_min": 1,
                "pool_max": 1,
                "psycopg_prepare_threshold": None,
                "returning": "id",
                "transaction_scope": "one transaction around all insert statements in a configuration sample",
            },
            "environment": {
                "python": sys.version,
                "platform": platform.platform(),
                "psycopg": psycopg.__version__,
                **env,
                **instrumentation,
            },
            "methodology": {
                "statement_order": "Configuration order is shuffled deterministically each round with a fixed recorded seed; each configuration has its own 3 warmups and 20 measured samples.",
                "timed_execute": "Per-statement time from immediately before conn.execute() through its return, which includes conversion, protocol, server, and response reception. fetchall and ID list extension are outside this timer but within transaction_total.",
                "conversion_instrumentation": "TimedCursor overrides Psycopg Cursor._convert_query only to time super()._convert_query during ordinary execute calls. This directly measures Psycopg's actual query-conversion stage (SQL placeholder conversion plus adaptation) while adding a small Python timing wrapper.",
                "adaptation": "Separate Transformer and PostgresQuery probes distinguish parameter serialization from complete Psycopg query conversion. Standalone Transformer time is partial and not equivalent to total driver time.",
                "server_timing": "Prior EXPLAIN ANALYZE results are retained as evidence but do not include all server parsing/binding work or normal RETURNING transfer.",
                "protocol": "libpq PQtrace is enabled for isolated synthetic insert probes. Only parsed message counts/length fields are saved; raw trace text is discarded. This is diagnostic tracing and is not part of latency samples. Server logging settings are read, not modified.",
                "memory": "Python tracemalloc runs are separate from latency runs and do not include native libpq memory or PostgreSQL backend memory.",
                "cleanup": "TRUNCATE and row count verification are outside timed sections. Every variant checks complete unique IDs and persisted row count.",
            },
            "psycopg_query_cache_limits": {
                "max_sql_bytes": MAX_CACHED_STATEMENT_LENGTH,
                "max_parameters": MAX_CACHED_STATEMENT_PARAMS,
                "source_behavior": "Psycopg avoids caching query placeholder parsing when either limit is exceeded.",
            },
            "query_conversion_batches": profile_conversion_batches(rows),
            "libpq_protocol_trace_probes": protocol_trace_probes(rows),
            "randomized_chunk_scaling": profile_chunk_variants(rows),
            "randomized_server_explain_chunk_scaling": profile_server_chunk_variants(rows),
            "separate_python_memory_probes": profile_memory(rows),
            "rows_after_final_cleanup": None,
        }
        clear_table()
        report["rows_after_final_cleanup"] = Base.execute(f'SELECT count(*) FROM "{TABLE}"')[0][0]
        if report["rows_after_final_cleanup"] != 0:
            raise RuntimeError("Profiling table is not empty after cleanup")
        output = Path(__file__).with_name("phase5_execution_path_results.json")
        output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        print(f"Phase 5 raw results saved to {output.resolve()}")
    finally:
        disconnect()


if __name__ == "__main__":
    main()
