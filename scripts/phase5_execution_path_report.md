# Pyzzel `create_many()` Performance Investigation — Phase 5

## 1. Executive summary

**Measured:** The largest batch’s Psycopg query-conversion stage is material. For one 5,000-row statement, the median `PostgresQuery.convert()` time was 49.59 ms (p95 70.30 ms), while standalone value serialization was 1.99 ms (p95 3.06 ms). The conversion method used here is the method Psycopg calls from `Cursor.execute()`.

**Measured:** The total client `execute()` time remains much larger than PostgreSQL’s reported planning plus execution time. At 5,000 rows in one statement, client execution was 119.17 ms median while server planning plus execution was about 24.69 ms. A rollback-based `EXPLAIN ANALYZE` run had 119.45 ms client wall time, despite not returning the 5,000 IDs to the application.

**Inferred:** Placeholder conversion explains a meaningful part of the client time, but it does not explain all of it. The remaining time is somewhere in query processing not reported by EXPLAIN, protocol/communication, or Psycopg work outside `PostgresQuery.convert()`. Available Windows and PostgreSQL instrumentation could not separate those components.

**Measured:** Randomized chunk tests retained the earlier result that one 5,000-row statement was fastest in this environment. It measured 119.17 ms median `execute()` time versus 311.61 ms for five 1,000-row statements. The intermediate 2,000-row chunk also improved on the current 1,000-row cap. Absolute timings varied between investigation runs, so this is evidence for a separate optimization experiment, not a universal performance claim or a production change.

**Conclusion:** The investigation is sufficient to proceed to a carefully scoped implementation experiment, provided that experiment measures memory, correctness, and failure behavior alongside speed. It is not sufficient to attribute the unexplained remainder to Psycopg, PostgreSQL parsing, or local communication individually. A Linux protocol-trace or server-side timing experiment is needed for that attribution, but is not required before testing a configurable chunk size behind the existing semantics.

## 2. Environment and methodology

- Windows 11, CPython 3.13.14, Psycopg 3.3.9.
- PostgreSQL 17.11 in a Debian container, database `pyzzle_test`, schema `public`.
- Pool min/max 1, `prepare_threshold=None`, read committed, `synchronous_commit=on`; one backend PID was used for the measurements.
- The existing profiling table and schema were used. Each sample used the same 5,000 deterministic rows (`name`, `age`, `active`), generated identity IDs, and `RETURNING id`.
- One transaction wrapped all statements in each sample. Truncation and row-count verification were outside latency timers. Three warm-ups and 20 measured samples were run per chunk configuration. Configuration order was shuffled each round using seed `5142026`.
- Each configuration verified 5,000 distinct returned IDs and a persisted row count of 5,000. Returned ID order matched input order in all 23 runs per configuration. This is an observed result, not a guarantee inferred for other PostgreSQL versions or plans.
- Memory tracing was a separate run and built/executed one chunk at a time. It excludes the already-created input dictionaries and validation/copy overhead. `tracemalloc` does not include libpq’s or PostgreSQL’s native allocations.

The existing Phase 4 raw results were not overwritten. New raw samples are in `phase5_execution_path_results.json`.

## 3. Execution path and Psycopg conversion

Pyzzel materializes and validates input dictionaries, applies defaults, converts values, and builds a parameterized `INSERT ... VALUES ... RETURNING id`. It executes each chunk within one outer transaction, fetches the returned rows, and appends their IDs. Psycopg’s synchronous `Cursor.execute()` converts `%s` placeholders and adapts parameters before sending the query; the call then waits for query processing and response reception. Pyzzel’s subsequent `fetchall()` and ID-list extension are measured separately in the earlier profile.

The Phase 5 profiler timed `Cursor._convert_query()` through an instrumented cursor during actual `conn.execute()` calls. This directly times Psycopg’s `PostgresQuery.convert()` stage. Separate probes measured its placeholder rewriting and `Transformer.dump_sequence()` serialization. The separate substage measurements use Psycopg private APIs and should not be added together as if they were a single non-overlapping timing.

| Rows in one statement | Parameters | SQL bytes | Placeholder rewrite median / p95 | Standalone adaptation median / p95 | `PostgresQuery.convert()` median / p95 |
|---:|---:|---:|---:|---:|---:|
| 100 | 300 | 1,492 | 0.72 / 0.91 ms | 0.042 / 0.056 ms | 0.77 / 0.88 ms |
| 500 | 1,500 | 7,092 | 3.97 / 4.82 ms | 0.20 / 0.22 ms | 4.14 / 5.14 ms |
| 1,000 | 3,000 | 14,092 | 8.31 / 9.66 ms | 0.38 / 0.62 ms | 9.23 / 15.60 ms |
| 2,000 | 6,000 | 28,092 | 17.61 / 27.41 ms | 0.81 / 1.51 ms | 18.45 / 23.15 ms |
| 5,000 | 15,000 | 70,092 | 46.08 / 59.78 ms | 1.99 / 3.06 ms | 49.59 / 70.30 ms |

**Measured:** For these workloads, rewriting SQL placeholders into PostgreSQL placeholders takes much longer than serializing the values alone. The 5,000-row case has 15,000 placeholders and about 70 KB of SQL text; standalone value serialization was about 2 ms, while the complete conversion stage was about 50 ms median.

**Measured:** All tested statements exceeded Psycopg 3.3.9’s query-conversion cache thresholds: 4,096 SQL bytes or 50 parameters. Psycopg therefore uses its uncached placeholder conversion path for these ORM-generated bulk statements. The reason documented in the installed Psycopg implementation is that very large, varying ORM statements have poor cacheability.

**Unknown:** This does not show the complete “driver overhead.” `PostgresQuery.convert()` is one actual stage of `Cursor.execute()`. Psycopg protocol calls, C/libpq work, backend parsing/binding, network transfer, and result reception occur outside that measured stage.

## 4. Randomized chunk-size measurements

All variants inserted the same 5,000 rows and returned the same 15,000 total parameters across their statements.

| Rows per statement | Statements | Aggregate SQL bytes | Client execute median / p95 | Transaction median / p95 | Psycopg conversion inside execute, median |
|---:|---:|---:|---:|---:|---:|
| 100 | 50 | 74,600 | 153.37 / 175.29 ms | 168.86 / 191.38 ms | 45.57 ms |
| 250 | 20 | 71,840 | 964.86 / 979.85 ms | 978.66 / 1,000.79 ms | 47.29 ms |
| 500 | 10 | 70,920 | 535.60 / 543.78 ms | 552.59 / 565.34 ms | 51.36 ms |
| 1,000 | 5 | 70,460 | 311.61 / 339.64 ms | 329.24 / 364.57 ms | 48.16 ms |
| 2,000 | 3 | 70,276 | 183.44 / 216.76 ms | 209.42 / 242.25 ms | 48.08 ms |
| 5,000 | 1 | 70,092 | 119.17 / 154.75 ms | 137.06 / 190.73 ms | 50.20 ms |

Variability, expressed as median absolute deviation (MAD) and coefficient of variation (CV) for client `execute()`, was:

| Rows per statement | MAD | CV |
|---:|---:|---:|
| 100 | 5.62 ms | 7.68% |
| 250 | 5.06 ms | 0.77% |
| 500 | 4.38 ms | 1.15% |
| 1,000 | 5.70 ms | 4.47% |
| 2,000 | 18.32 ms | 10.10% |
| 5,000 | 19.81 ms | 23.37% |

**Measured:** The 5,000-row one-statement configuration reduced median execute time by about 62% compared with five 1,000-row statements in this run. The 2,000-row configuration was also faster than the current 1,000-row cap.

**Measured:** The 250-row result is a repeatable anomaly: 20 statements took about 965 ms median, while 50 statements of 100 rows took about 153 ms. Its CV is low, so this was not just one slow outlier. Psycopg conversion totaled about 47 ms in the 250-row case and does not explain the result. The server-side EXPLAIN comparison below also does not show a matching increase in reported plan/execution time.

**Inferred:** There is a per-statement or per-query-processing cost with a nonlinear dependence on statement shape/parameter count. The experiment does not identify whether it is PostgreSQL parse/bind work, protocol/client work, or another layer. The 250-row result should be investigated before choosing a chunk default.

## 5. PostgreSQL-side evidence

For each chunk shape, the profiler ran the actual parameterized `INSERT ... RETURNING id` under `EXPLAIN (ANALYZE, BUFFERS, WAL, FORMAT JSON)`, then rolled the transaction back. Each plan reported the expected 5,000 affected rows.

| Rows per statement | Server planning median | Server execution median | EXPLAIN client wall median |
|---:|---:|---:|---:|
| 100 | 6.35 ms | 20.38 ms | 162.93 ms |
| 250 | 6.57 ms | 20.53 ms | 975.75 ms |
| 500 | 5.53 ms | 20.02 ms | 541.47 ms |
| 1,000 | 5.41 ms | 18.40 ms | 315.69 ms |
| 2,000 | 6.15 ms | 17.54 ms | 178.77 ms |
| 5,000 | 8.01 ms | 16.68 ms | 119.45 ms |

**Measured:** Server-reported planning plus execution medians stay around 23–28 ms across chunk sizes. Client-side EXPLAIN wall time closely follows the measured insertion execution time, including the unusually slow 250-row configuration.

**Important limit:** EXPLAIN’s “Planning Time” and “Execution Time” are not complete server query-processing time. In particular, they do not account for all parsing/analysis/bind handling. EXPLAIN also adds instrumentation and does not deliver the normal 5,000-row `RETURNING` result to the application. Do not calculate a precise driver/network cost by subtracting server time from client time.

## 6. Communication, RETURNING, and instrumentation limits

The prior Phase 4 `SELECT 1` median was 0.93 ms (p95 1.10 ms). This is a small query round-trip reference, not a pure network measurement. The machine is Windows 11 and PostgreSQL is a local Debian container.

**Measured:** `fetchall()` and ID extraction were sub-millisecond at 5,000 rows in the prior profile. The EXPLAIN client timings are close to ordinary insert timings even though EXPLAIN does not return all IDs. This makes Python-side row decoding and the RETURNING payload less likely to explain the long delay.

**Inferred:** Receiving 5,000 IDs is not the primary cost. The client timer includes response reception, but this experiment cannot separately time socket transfer from libpq’s protocol processing. A small `SELECT 1` result also cannot quantify the transfer cost of a large bind message or RETURNING response.

**Unavailable:** Psycopg’s `PGconn.trace()` raised `NotSupportedError: currently only supported on Linux`; no protocol trace or message counts were captured. PostgreSQL statement logging was disabled (`log_statement=none`, `log_min_duration_statement=-1`), the logging collector was off, and `pg_current_logfile()` returned `NULL`. `pg_stat_statements` was not installed or preloaded. `track_io_timing` and `track_wal_io_timing` were off. These settings were only read; none were changed.

Thus, exact protocol message count/size, time in PostgreSQL parsing and binding, and split client send/receive times remain **Unknown**.

## 7. Memory and correctness / operational trade-offs

Separate Python `tracemalloc` peaks for streaming statement construction and execution were:

| Rows per statement | Python traced peak |
|---:|---:|
| 100 | 0.30 MB |
| 250 | 0.47 MB |
| 500 | 0.76 MB |
| 1,000 | 1.46 MB |
| 2,000 | 2.56 MB |
| 5,000 | 6.11 MB |

**Measured:** Python-traced peak rose with chunk size, reaching about 6.1 MB for one 5,000-row statement versus 1.46 MB for 1,000-row statements. These are additional allocations during the probe; the input row dictionaries already existed. This is not total process memory and does not include libpq or server memory.

All configurations stayed within the measured workload’s limits: the largest query had 15,000 parameters and 70,092 SQL bytes. Every configuration committed once, returned a complete unique ID set, and left the table clean after cleanup. The whole-operation transaction boundaries were equivalent, so any statement error would abort the same outer transaction; failure injection was not part of this phase.

Returned ID order matched input order in every observed sample. Treat that as an empirical check only; the benchmark does not establish a cross-version ordering contract. A future API decision should clarify whether callers may rely on returned-ID ordering.

Larger statements also increase peak memory, make a single statement’s error reporting cover more rows, and create larger SQL/parameter objects. The 5,000-row statement was valid in this environment, but this one machine/database does not establish a safe universal batch size.

## 8. Comparison with previous profiling

The prior 20-sample Phase 4 run measured five 1,000-row statements at 304.33 ms median execute time and 332.07 ms p95. The randomized Phase 5 run measured 311.61 ms median and 339.64 ms p95, a close recheck.

The prior single 5,000-row experiment measured 88.00 ms median and 122.98 ms p95. The new randomized run measured 119.17 ms median and 154.75 ms p95. The ranking remained the same, but the absolute one-statement latency rose between runs. The 5,000-row CV was 23.37%, so avoid treating the earlier 88 ms result as a stable expectation.

The prior full API medians were 70.39 ms for 1,000 rows and 326.23 ms for 5,000 rows. They include Python preparation and normal `create_many()` behavior, unlike the direct chunk comparison’s prepared data and statements. The new tests preserve insert/conversion/returning semantics but vary statement boundaries in an isolated profiler; they do not call the production API with alternate chunk sizes.

## 9. Bottleneck confidence

- **High — Psycopg SQL placeholder conversion is a real contributor.** Measured inside `Cursor.execute()`; 49.59 ms median for a single 5,000-row statement.
- **High — standalone value serialization is not the whole adaptation cost.** Measured at about 1.99 ms median for 5,000 values rows, versus about 49.59 ms for full query conversion.
- **High — PostgreSQL reported plan/execution fields do not explain client latency.** Direct EXPLAIN metrics are much lower and similar across chunk sizes.
- **Medium — repeated statement-level work contributes to slower smaller chunks.** Randomized chunk timings show a strong relationship overall, though the 100-row/250-row reversal indicates a nonlinear additional factor.
- **Medium — RETURNING decode is not dominant.** Direct fetch/ID stages are small and EXPLAIN client wall is close to normal insert client time, but result transmission cannot be separated independently.
- **Low — exact share of PostgreSQL parsing/binding, Psycopg protocol/C work, and local communication.** The required protocol and server log instrumentation is unavailable in this environment.

## 10. Recommended experiments for a separate implementation phase

1. **Add a configurable chunk-size option behind the existing API default.** Test 1,000, 2,000, and 5,000 first; retain identical validation, conversions, one transaction, and `RETURNING`. Include memory and failure rollback checks. Do not switch the default based only on these results.
2. **Investigate the 250-row latency cliff.** Repeat the same randomized sample on a Linux environment with Psycopg `PGconn.trace()` and PostgreSQL statement logging or `pg_stat_statements` available. Include 100, 150, 200, 250, 300, 400, and 500 rows per statement to locate the transition.
3. **Measure application-level memory/RSS on the target deployment platform.** Compare current `create_many()` with candidate chunk sizes; include its input materialization, validation copies, adapted parameters, and returned ID list.
4. **Evaluate prepared execution only as a separate experiment.** The existing Phase 4 prepared variant did not show a useful improvement, and large dynamic SQL statements have poor cacheability. Keep the current unprepared setting as the baseline.
5. **Preserve correctness checks:** exact row count, complete unique generated IDs, one atomic transaction, cleanup, and explicit tests for a failing row in a later chunk.

## 11. Files and reproducibility

- `profile_phase5_execution_path.py` runs the isolated experiment. It reads `PYZZEL_TEST_DATABASE_URL`; defaults to 20 measured samples and 3 warm-ups. It writes a new raw-results file and does not overwrite earlier profiles.
- `phase5_execution_path_results.json` contains the environment, per-sample raw values, randomized orders, query-conversion timings, server EXPLAIN measurements, memory results, verification status, and limitations.
- This report is `phase5_execution_path_report.md`.

No Pyzzel production source was modified, and no existing benchmark artifact was overwritten.
