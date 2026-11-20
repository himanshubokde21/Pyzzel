# Pyzzel Performance Optimization — Phase 6 Report

## Outcome

Phase 6 is complete. The dedicated PostgreSQL database became reachable, so the fresh database baseline, randomized chunk experiment, candidate comparison, and complete test suite all ran. **No production optimization was accepted**: the candidate cap change did not produce a consistent end-to-end improvement. The production chunk cap remains 1,000 rows.

The new randomized run did **not** reproduce Phase 5's 250-row slowdown. At 5,000 total rows, client execution median fell steadily as statement chunks grew. The 2,000-row-cap candidate was then measured through the complete public `create_many()` API and was rejected because results across dataset sizes were mixed.

## Execution flow reviewed

`Base.create_many()` materializes input, checks dictionary rows, copies row dictionaries, applies defaults, validates uniform keys and columns, converts values through `Column.to_db()`, and builds parameterized `INSERT ... RETURNING` statements through `sql/builder.py::insert_many()`. It executes bounded chunks inside one `_run_tx` transaction, fetches returned rows, and collects generated IDs.

The current per-statement row cap is `min(1000, 65535 // number_of_columns)`. SQL uses `%s` placeholders and all user values remain in Psycopg parameter tuples.

## Fresh database baseline

Measurements used the dedicated `pyzzle_test` PostgreSQL 17.11 database, one pooled connection, `prepare_threshold=None`, 3 warm-ups, and 20 measured repetitions. The profiler used its dedicated `pyzzel_test_create_many_profile` table. Cleanup and row-count verification were outside timed sections.

| Rows | Python preparation median / p95 | `create_many()` median / p95 | Min–max API sample |
|---:|---:|---:|---:|
| 10 | 0.03 / 0.04 ms | 5.36 / 7.40 ms | 4.60–7.45 ms |
| 100 | 0.39 / 0.58 ms | 8.16 / 10.63 ms | 6.68–11.05 ms |
| 1,000 | 2.46 / 2.86 ms | 22.52 / 23.81 ms | 20.62–24.58 ms |
| 5,000 | 9.28 / 11.41 ms | 76.84 / 88.38 ms | 71.75–88.70 ms |

At 5,000 rows, separately measured medians were 63.50 ms for execute plus RETURNING, 0.34 ms fetching rows, 0.24 ms extracting IDs, and 69.02 ms for the profiled transaction. The full API includes its own preparation and normal execution path, so these independently measured stages need not add to the API median.

The fresh Python-only 5,000-row preparation median was 6.60 ms; validation/default application was 2.77 ms, `to_db()` conversion 2.97 ms, and SQL/parameter preparation 0.79 ms. Raw samples are saved in `phase6_baseline_python_only.json`.

## Phase 6A: randomized chunk-size validation

The test inserted the same 5,000 rows and returned all generated IDs under one transaction. Each of six configurations had 3 warm-ups and 20 measured samples, with seeded randomized order. The single pool backend was reused. Every configuration verified the complete unique ID set, row count, and observed ID order. Memory was measured separately.

| Rows per statement | Statements | Execute + RETURNING median / p95 | Transaction median / p95 | Server plan / execution median | Traced peak memory |
|---:|---:|---:|---:|---:|---:|
| 100 | 50 | 107.76 / 114.71 ms | 113.58 / 121.11 ms | 4.12 / 12.38 ms | 0.30 MB |
| 250 | 20 | 80.05 / 96.82 ms | 85.58 / 102.69 ms | 3.53 / 10.87 ms | 0.47 MB |
| 500 | 10 | 69.89 / 81.85 ms | 74.85 / 87.23 ms | 3.04 / 9.95 ms | 0.76 MB |
| 1,000 | 5 | 61.67 / 67.46 ms | 66.38 / 72.76 ms | 3.14 / 10.24 ms | 1.46 MB |
| 2,000 | 3 | 59.12 / 63.89 ms | 64.41 / 68.97 ms | 3.67 / 10.37 ms | 2.56 MB |
| 5,000 | 1 | 56.51 / 64.17 ms | 62.29 / 71.65 ms | 4.57 / 9.62 ms | 6.11 MB |

The prior 250-row cliff was not reproduced: fresh median execute time at 250 rows was 80.05 ms, below 100-row chunks at 107.76 ms and above 500-row chunks at 69.89 ms. The profile showed a smooth trend across configurations. Server EXPLAIN plan and execution medians remained around 13–14 ms combined, while EXPLAIN client wall time followed client latency; it is not pure database execution time.

The larger statement candidates reduce repeated client-side statement work in this particular local environment, but grow traced peak memory from 1.46 MB at 1,000 rows to 6.11 MB at 5,000 rows. Traced memory excludes already-built input rows, libpq native allocations, and server memory.

## Phase 6B: candidate optimization and decision

Candidate: increase the internal row cap from 1,000 to 2,000. The change was implemented alone, the focused tests ran, and the same full API profiler was run at 10, 100, 1,000, and 5,000 rows. The production change was then reverted.

| Rows | 1,000 cap median / p95 | 2,000 cap median / p95 | Median change | p95 change |
|---:|---:|---:|---:|---:|
| 10 | 5.36 / 7.40 ms | 4.90 / 6.33 ms | −8.50% | −14.48% |
| 100 | 8.16 / 10.63 ms | 6.74 / 7.85 ms | −17.37% | −26.21% |
| 1,000 | 22.52 / 23.81 ms | 23.81 / 29.10 ms | +5.76% | +22.25% |
| 5,000 | 76.84 / 88.38 ms | 76.35 / 81.58 ms | −0.64% | −7.70% |

**Rejected.** The complete API results are mixed: 1,000 rows regressed while 5,000 rows had essentially flat median latency. The isolated prepared-statement result was not enough to justify a production default change. The existing 1,000-row policy is retained.

Other candidates:

- **Avoid Psycopg placeholder rewriting — rejected under current constraints.** Phase 5/6 instrumentation confirms conversion matters for large statements, but no supported approach that keeps the repository's parameterized `%s` contract was established. No private driver shortcut or value interpolation was added.
- **Change validation/default handling or `to_db()` conversion — inconclusive.** These are measurable Python stages, but no safe alternative was implemented and shown to improve full API time.
- **Change SQL construction/allocation — inconclusive.** Its measured share is smaller and no end-to-end gain was demonstrated.

## Correctness and tests

Added two database regression tests: a 5,000-row insert checks count, unique IDs, and first/last generated-ID correspondence; a unique constraint failure in a later chunk checks that the earlier chunk is rolled back. The focused write tests passed (11 passed). The final full PostgreSQL-enabled suite passed:

- 263 passed
- 0 skipped
- 0 failed

The raw chunk profile also verified full unique ID sets, row counts, and transaction cleanup. Existing Phase 5 artifacts were not modified.

## Files and limitations

- `phase6_optimization_report.md` — this report.
- `phase6_optimization_results.json` — complete fresh raw profiles and candidate comparison.
- `phase6_optimization_environment.json` — environment and database details.
- `phase6_baseline_python_only.json`, `phase6_baseline_database.json`, `phase6_chunk_validation_results.json`, `phase6_after_chunk_2000.json` — individual raw artifacts.
- `profile_phase6_chunk_validation.py` — isolated Phase 6 copy of the randomized Phase 5 profiler, with a Phase 6 output path.

All findings describe this Windows/PostgreSQL container environment. Psycopg execution timing still combines driver conversion, protocol, PostgreSQL work, and result transfer. The EXPLAIN timings do not isolate those pieces.

## Phase 7 readiness

**Ready to enter the permanent comparative ORM benchmark suite.** The full PostgreSQL-enabled tests pass, and Phase 6 leaves the production implementation unchanged. This does not claim Pyzzel is faster than another ORM; Phase 7 must apply the normalized and idiomatic comparison methodology consistently.
