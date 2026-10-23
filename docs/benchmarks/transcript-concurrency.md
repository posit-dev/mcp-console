# Transcript concurrency comparison

## Conditions

Measured on October 7, 2026 (UTC) on `tomaszkalinows-WQVX`: Mac16,5, arm64, macOS 26.7 (25G229), 16 physical and logical CPUs, 128 GiB RAM.
The candidates were `max(1, N - 1)` and `max(1, 2 * N - 1)`, tested with explicit job counts of 15 and 31.
The source was `b12f2593f69b0e29cf724a1fe03e7c71222dbafb`, after PR #526 merged.
The same release executable and transcript runner were used throughout; their SHA-256 hashes were checked before every run.

The fixed suite contained 44 cases and 79 direct/sandboxed executions: R/Python runtime behavior, tool discovery and argument handling, Markdown recording, CLI help/version, and four SQL persistence/dataframe/error/preview cases.
An untimed warmup built the checkout and passed all 79 executions.
Every measured run used warm shared dependency caches and fresh case workspaces/sessions; no caches were cleared.
The warmup's build (86.07 seconds) and transcripts (240.41 seconds) are excluded from the comparison.

Runner Python was 3.13.11 (`UV_PYTHON=3.13`), R was 4.6.1, and the pinned sandbox companion was `85d407d8a4544ed0916aff3c7a273461e739f215`.
`CARGO_BUILD_JOBS=2` and `CARGO_PROFILE_DEV_DEBUG=CARGO_PROFILE_TEST_DEBUG=0` were inherited; the measured runs skipped builds through `MCP_CONSOLE_TEST_BINARY`.
`PYTHON_CPU_COUNT`, `MAKEFLAGS`, `R_HOME`, `R_LIBS_USER`, `RETICULATE_PYTHON`, `UV_CACHE_DIR`, `XDG_CACHE_HOME`, `OMP_NUM_THREADS`, and `OPENBLAS_NUM_THREADS` were unset.
Fixtures isolated `MCP_CONSOLE_HOME` while preserving `HOME` and host tool/cache settings.
The selected programs, dependency declarations, executable, selectors, execution modes, and 600-second case deadline were unchanged between candidates.

Reproduce a run from that source after staging/building and warming the selected suite:

```sh
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" \
    scripts/test --jobs 15 --timeout 600 \
    client_server/r/test_runtime \
    client_server/python/test_runtime \
    client_server/server/test_tools \
    client_server/recording/test_markdown \
    cli/interface/test_help \
    client_server/sql/test_catalog::evaluates_queries_in_a_persistent_catalog \
    client_server/sql/test_catalog::queries_r_data_frames \
    client_server/sql/test_catalog::recovers_from_sql_errors \
    client_server/sql/test_catalog::previews_schema_and_exact_values
```

Change only `--jobs` for the other candidate.
There were three warm repetitions each, in the order 15, 31, 31, 15, 15, 31 to reverse the order in the middle pair.
Wall time includes runner startup, the serial canonical initialization case, selected cases, and snapshot comparison.

Other development builds, tests, and system processes ran on this shared host; their workload was uncontrolled.
System load, memory pressure, swap usage, and compiler/Console process counts were sampled every five seconds and at the start/end of each run.
These are host-wide observations and cannot assign pressure to this suite alone.
No Linux or Windows timing or native validation was performed.

## Results

| Run (UTC start) | Jobs | Wall seconds | Canonical execution seconds | Executions |
| --------------- | ---: | -----------: | --------------------------: | ---------- |
| 1 (01:17:44)    |   15 |       518.70 |                      142.06 | 79 passed  |
| 2 (01:26:23)    |   31 |       168.60 |                       63.03 | 79 passed  |
| 3 (01:29:12)    |   31 |       148.70 |                       77.29 | 79 passed  |
| 4 (01:31:41)    |   15 |       165.17 |                       87.27 | 79 passed  |
| 5 (01:34:26)    |   15 |       126.74 |                       35.69 | 79 passed  |
| 6 (01:36:33)    |   31 |       125.28 |                       46.30 | 79 passed  |

| Jobs | Median seconds | Mean seconds | Range seconds |
| ---: | -------------: | -----------: | ------------- |
|   15 |         165.17 |       270.21 | 126.74–518.70 |
|   31 |         148.70 |       147.53 | 125.28–168.60 |

All six runs exited successfully: no failures, timeouts, skipped executions, or unstarted cases.
The first 15-job run is retained in every summary.

|  Run | Host 1-minute load range | Minimum memory-pressure free % | Compiler processes range | Peak Console processes |
| ---: | ------------------------ | -----------------------------: | ------------------------ | ---------------------: |
|    1 | 8.95–25.61               |                             39 | 0–7                      |                    167 |
|    2 | 16.25–23.80              |                             40 | 0–2                      |                    193 |
|    3 | 11.80–16.57              |                             40 | 0–2                      |                    195 |
|    4 | 9.67–20.14               |                             42 | 0–2                      |                    143 |
|    5 | 10.57–18.89              |                             42 | 0–3                      |                    129 |
|    6 | 8.52–13.23               |                             43 | 0–1                      |                    181 |

Existing swap usage remained at 20,151.19 MiB in every sample, with no new swap-outs during any run.
The memory column is the indicator reported by `memory_pressure -Q`; the process counts and load are host-wide, including other development work.

## Decision

Use `max(2, 2 * N)` on macOS, Linux, and Windows, without an OS-specific cap.
The unavailable-CPU fallback is two cases; explicit `--jobs N` overrides remain available.
The shared boundary runner also accepts `-j N`; unscoped Windows `--full` runs require `--jobs N`.

The 31-job candidate finished sooner in each adjacent pair, but its advantage shrank from 350.10 to 16.48 to 1.46 seconds (67.50%, 9.97%, and 1.15%).
The nominal median difference is 9.97%; the ranges overlap, canonical initialization alone varied from 35.69 to 142.06 seconds, and host load/background compiler activity changed during the comparison.
This bounded comparison is inconclusive about a repeatable benefit from doubling concurrency.
Default to twice the logical CPU count in the absence of conclusive results.
On this 16-CPU host, the default is 32 jobs; the measured higher candidate used 31 jobs.
These measurements do not establish an optimal default for Linux or Windows.
