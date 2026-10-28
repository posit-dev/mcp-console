# First-evaluation latency, October 25, 2026

Deferring unused runtimes and reusing completed preparation reduced first-evaluation latency in the measured R and managed-Python configurations.
With existing environments, the largest R improvement came from skipping an unused reticulate preparation probe when `uv` was absent.
Existing Python without DuckDB showed no consistent improvement.
The results do not yet meet a target of 10–20% overhead over plain R.

Measurements used an Apple M4 Max with 16 logical CPUs and 128 GiB RAM, running macOS 26.7.1 (25G241), R 4.6.1, and Python 3.13.16.
Both the existing and managed Python selections used that Python version.
No builds or test suites ran during measurement; unrelated activity on this shared host was uncontrolled.
These are local macOS results, with no Linux or Windows timing evidence.

Each value below is the median of five fresh server sessions, in milliseconds.
The timer starts at process launch and ends at the completed public `send` response, including MCP initialization and tool discovery.
R and Python evaluate `x = 1 + 1`; SQL evaluates `SELECT 1 + 1 AS answer` and returns its result.
Shutdown and subsequent metadata collection are excluded.
One untimed session warms each configuration before measurement, and baseline/candidate order alternates between rounds.
Dependency caches remain populated and shared; this measures fresh processes with warm package caches, not first-time downloads or client-application startup.

The [sample CSV](first-evaluation-samples.csv) contains all timed Console and plain-interpreter runs, including MCP handshake times and warm-call medians.

Direct execution uses `--no-sandbox` and its ordinary host-cache policy.
Sandboxed execution uses the native default sandbox and its managed-cache policy.
Fresh workspaces and temporary Console homes are used in both modes; `MCP_CONSOLE_HOME` does not select dependency cache locations.
Native R startup files are suppressed for these comparisons.

| First evaluation and configuration    | Direct before | Direct after | Sandboxed before | Sandboxed after |
| ------------------------------------- | ------------: | -----------: | ---------------: | --------------: |
| R; existing Python, `uv` available    |         602.5 |        374.6 |           1000.4 |           633.9 |
| R; existing Python, no `uv` on `PATH` |        1256.8 |        373.4 |           1649.8 |           640.1 |
| R; managed defaults                   |        1968.0 |       1385.1 |           2540.3 |          1404.3 |

The first two configurations disable R package preparation and use installed R libraries and an existing Python environment without DuckDB.
Their tool interface exposes R, Python, and SQL.
Managed defaults retain ordinary R and Python package preparation.

The following configurations physically exclude R from discovery, except the final two rows, which explicitly disable installed R with `r.enabled: false`.
“Python + SQL” means both source fields are exposed; the first column identifies which is evaluated first.
The data environment is preconfigured with NumPy 2.5.4, pandas 3.1.0, Matplotlib 3.11.2, DuckDB 2.0.0, and PyArrow 26.0.0.

| First evaluation and configuration                           | Direct before | Direct after | Sandboxed before | Sandboxed after |
| ------------------------------------------------------------ | ------------: | -----------: | ---------------: | --------------: |
| Python only; existing environment without DuckDB             |         180.0 |        184.6 |            541.1 |           567.3 |
| Python; Python + SQL, existing environment without DuckDB    |         205.4 |        197.9 |            608.1 |           610.7 |
| Python only; existing data environment                       |         198.7 |        187.6 |            639.1 |           535.0 |
| Python; Python + SQL, existing data environment              |         235.7 |        184.5 |            658.6 |           586.0 |
| SQL; Python + SQL, existing data environment                 |         243.1 |        216.7 |            669.2 |           651.5 |
| Python only; managed environment                             |         634.9 |        476.8 |           1265.6 |          1021.8 |
| Python; Python + SQL, managed environment                    |         637.3 |        487.8 |           1259.4 |          1025.9 |
| SQL; Python + SQL, managed environment                       |         651.2 |        538.1 |           1255.9 |          1126.2 |
| Python only; R disabled, existing environment without DuckDB |             — |        163.1 |                — |           421.7 |
| Python only; R disabled, existing data environment           |             — |        174.7 |                — |           456.4 |

The baseline does not support `r.enabled`, so those rows have no before value.
SQL-first execution is omitted for the existing environment without DuckDB.
The Python-only environment without DuckDB had slightly higher candidate medians; its ranges overlapped: 171.5–197.3 ms before versus 175.5–186.9 ms after directly, and 529.3–605.4 ms versus 558.7–613.5 ms sandboxed.
Five samples on a shared host do not establish a repeatable improvement for that case.

Plain `Rscript --vanilla` reached its silent-assignment completion marker in 259.7 ms in the direct comparison series.
The corresponding Console result is about 1.44 times that reference, still above the 1.10–1.20 target.
Sandboxed Console is about 2.47 times the 257.0 ms plain-R reference measured in that series.
Plain Python reached its marker in 28.1 ms without DuckDB and 29.3 ms in the data environment in the direct series.
The plain-interpreter timer ends at its first stdout line and excludes shutdown; it does not include an MCP exchange or sandbox.

Warm calls remained around 3.6–3.8 ms across both binaries and execution modes, using twenty additional calls per session.
Further startup work should separately measure installation discovery, worker launch, and embedded-runtime setup; these end-to-end measurements do not attribute the remaining overhead to individual stages.

The baseline was `cad6a724c978f3fb18d60344d1541f98adcf8f24`; its native runtime differs from the PR base only in transport comments.
The candidate was `1c9d9d09c2cd76f35c50d76299487c473e39ce54`.
Both executables report version 0.0.6.
Their SHA-256 hashes were:

- Baseline: `ed1bcbef021d3163a4b5f9570d219b0bae226e53ac5c6ce97a47fce8053f8907`.
- Candidate: `a0dbe461eb03e98c1595de3070125b368d7d57bed209bbb84af33a3767c99018`.

Build each revision separately with the repository workflow, then run [the benchmark harness](../../benchmarks/startup.py):

```sh
uv run benchmarks/startup.py \
  --binary /path/to/candidate/mcp-console \
  --candidate-revision 1c9d9d09c2cd76f35c50d76299487c473e39ce54 \
  --baseline /path/to/baseline/mcp-console \
  --baseline-revision cad6a724c978f3fb18d60344d1541f98adcf8f24 \
  --python /path/to/existing/environment/bin/python \
  --r-library /path/to/installed/R/library \
  --warmup 1 --runs 5 --warm-calls 20 --silent \
  --output /tmp/startup-direct.json
```

Repeat with `--sandbox` and a different output path.
The data-environment runs select `--variant python-existing`, `--variant python-with-sql-existing`, `--variant python-sql-existing`, and `--variant r-disabled-existing`.
The JSON output retains every sample, invocation, selected runtime, effective cache setting, and binary hash.
