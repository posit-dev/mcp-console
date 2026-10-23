# Jupyter worker comparison

The initial comparison was measured on October 23, 2026, on an Apple M4 Max with 16 logical CPUs and 128 GiB RAM, running macOS 26.7.1 arm64.
These measurements compare Console's built-in worker with the optional [Jupyter mode](../JUPYTER.md).
They describe this host, these installed releases, and trivial cells; they do not establish general performance parity.
A separate [shared R and Python comparison](#shared-r-and-python) below measures both runtimes on macOS and Linux, with direct execution and the default read-only sandbox.

## Method and provenance

The initial comparison's implementation corresponds to commit `79b55739d12592fb1dde0e2858325bad0056bac3`.
These results retain their original implementation revisions and executable hashes; they are not measurements of subsequent revisions.
The measured release executable is 12,375,056 bytes, with SHA-256 `8aa187a83b1b2764748c667867bdf4ae122462e42e4179c1fdf7075c8d452812`.
Its hash was checked again after the public acceptance tests.
The [benchmark program](../../benchmarks/jupyter.py) drives the public MCP connection and saves [individual measurements](../../benchmarks/results/jupyter-macos-arm64-2026-10-23.json).
Only executable paths in the saved results are normalized; timing, memory, process IDs, and version data are retained.

The installed releases were Python 3.13.16, R 4.6.1, jupyter_client 8.10.0, ipykernel 7.4.0, pyzmq 27.2.0, ark 0.1.252, and CRAN reticulate 1.47.0.
Ark is a published prerelease.
No development checkout of a kernel or reticulate was used.

Each variant launched five fresh Console sessions, in round-robin order, and ran the identical assignment `x = 1` once followed by 30 warm repetitions.
The first-result interval starts before the Console process is launched and ends after the first completed assignment, including MCP initialization and tool discovery.
Warm latency measures one complete subsequent `send` call.
Each variant therefore contributes five startup samples and 150 warm-cell samples.

The initial comparison used direct execution with host permissions, ordinary Console recording, fresh temporary workspaces, and fresh Jupyter/IPython configuration directories.
The Python executable and published R library were selected explicitly; R resolution was disabled and `ir`/`uv` were absent from the child PATH.
No dependency downloads or package installation were timed.
Installed-runtime and filesystem caches remained warm across launches.
The host was not isolated from other activity; these are descriptive samples, not a controlled estimate of statistical significance.

The built-in variants exposed only the measured language.
Built-in Python used the custom evaluator without initializing R; built-in R did not initialize Python.
The ark Python variant initialized R and reticulate before evaluating Python in that same kernel process.
Its startup and memory figures therefore include both language runtimes.
The ipykernel variant used IPC, while ark used loopback TCP because the tested ark release does not use jupyter_client's IPC endpoint convention.

## Observations

| Variant                        | First result median (s) | First result range (s) | Warm median (ms) | Warm p95 (ms) | Median summed RSS (MiB) |
| ------------------------------ | ----------------------: | ---------------------- | ---------------: | ------------: | ----------------------: |
| Built-in Python                |                   1.134 | 1.115–1.259            |            3.108 |         3.421 |                   129.0 |
| ipykernel, IPC                 |                   0.385 | 0.378–0.453            |            5.503 |         6.001 |                   112.4 |
| Built-in R                     |                   1.151 | 1.136–1.195            |            3.096 |         3.520 |                   129.8 |
| ark, TCP                       |                   0.894 | 0.873–0.902            |            4.270 |         4.799 |                   163.6 |
| ark + reticulate, Python cells |                   1.576 | 1.531–1.622            |            4.323 |         4.968 |                   331.0 |

Warm p95 is the linearly interpolated 95th percentile of all 150 warm cells for that variant.
RSS is a `ps` snapshot after warm execution, summed over Console and its descendants, then summarized across the five launches.
It excludes the benchmark client, is not peak memory, and can count shared pages more than once.
All variants had four live processes in these snapshots: the built-in path retained its preparation process, while the Jupyter path had separate adapter and kernel processes alongside Console and its relay.
Equal process counts do not imply equal responsibilities or memory ownership.

For this assignment, ipykernel's median warm call took about 1.77 times the built-in Python call; ark's R call took about 1.38 times the built-in R call.
The absolute differences were approximately 2.40 ms and 1.17 ms.
The shared ark/reticulate Python call took about 1.39 times the built-in Python call, while loading both languages increased observed memory substantially.
These comparisons measure dispatch overhead with very little language work.
They do not compare Python final-expression display, because `py_run_string()` does not provide the native IPython display contract.

Standalone ipykernel and ark reached their first result sooner than the built-in worker in this setup.
Jupyter is therefore not uniformly slower to start: the paths do different discovery, preparation, initialization, and protocol work.
The shared R/Python path had the slowest first result among these variants.
None of these timings include a cold installation, sandbox startup, SQL, package activation, large text or image transfer, concurrent load, or a long numerical workload.
The subsequently added automatic private sandbox transports are outside these measurements: macOS adds a private-directory Unix-socket policy, while Linux starts a native deny-all proxy and private network namespace.
The Linux path therefore has additional setup and process costs that this direct-execution comparison does not quantify.
Image serialization, output queues, history writes, and extra protocol copies need their own workload measurements before selecting a mode for those cases.

## Shared R and Python

The shared endpoint was measured later on October 23 at implementation commit `d1061713f32c1d0365eeafbb5bd0ae0ddb675c50`.
Both the built-in worker and ark/shared ipykernel initialized R with `invisible(NULL)`, then ran the first Python assignment, all within the startup interval.
Each variant again used five fresh sessions and 30 warm assignments per session, in round-robin order.
This compares sessions with both languages loaded; the earlier built-in Python baseline loaded only Python.
The ark variant uses `--jupyter-reticulate --jupyter-reticulate-ipykernel`, including both endpoints' completion barriers.
Direct Jupyter sessions use IPC; sandbox sessions use the automatic private transport with no added permissions.

Published dependencies match the versions above on both hosts.
The [macOS samples](../../benchmarks/results/jupyter-shared-macos-arm64-2026-10-23.json) use the same M4 Max host.
The [Linux samples](../../benchmarks/results/jupyter-shared-linux-x86_64-2026-10-23.json) use an Intel Core i7-8700K with 12 logical CPUs and 62.7 GiB of OS-visible memory, Linux 6.8.0-147 x86_64, and glibc 2.39.
Compare variants within each host; the two machines do not isolate an operating-system effect.
No other tests from this task ran on either host during its benchmark, but unrelated host activity was not excluded.
Runtime caches remained warm, IPython profiles were fresh, and the sandbox used disposable worker caches.
No installation, plots, SQL, numerical workload, sustained output, or cold-cache behavior was measured.

macOS:

| Variant                                   | First result median (s) | First result range (s) | Warm median (ms) | Warm p95 (ms) | Median summed RSS (MiB) |
| ----------------------------------------- | ----------------------: | ---------------------- | ---------------: | ------------: | ----------------------: |
| Built-in R/Python, direct                 |                   1.232 | 1.195–1.476            |            3.231 |         3.691 |                   130.0 |
| Ark + shared ipykernel, direct            |                   1.963 | 1.887–2.048            |            5.953 |         6.533 |                   367.8 |
| Built-in R/Python, read-only sandbox      |                   1.552 | 1.529–1.727            |            3.218 |         3.543 |                   151.5 |
| Ark + shared ipykernel, read-only sandbox |                   2.648 | 2.641–2.729            |            6.094 |         7.556 |                   410.2 |

Linux:

| Variant                                   | First result median (s) | First result range (s) | Warm median (ms) | Warm p95 (ms) | Median summed RSS (MiB) |
| ----------------------------------------- | ----------------------: | ---------------------- | ---------------: | ------------: | ----------------------: |
| Built-in R/Python, direct                 |                   1.144 | 1.139–1.158            |            3.143 |         3.484 |                   130.9 |
| Ark + shared ipykernel, direct            |                   2.832 | 1.944–2.847            |            7.328 |         7.974 |                   343.9 |
| Built-in R/Python, read-only sandbox      |                   1.588 | 1.580–1.594            |            3.146 |         3.481 |                   177.1 |
| Ark + shared ipykernel, read-only sandbox |                   3.537 | 3.516–3.562            |            7.323 |         8.284 |                   410.6 |

Under the default sandbox, the shared endpoint added about 1.10 seconds to median startup and 2.88 ms per warm assignment on macOS; on Linux, the differences were 1.95 seconds and 4.18 ms.
The warm calls took about 1.89 and 2.33 times the built-in calls, respectively.
These are dispatch costs for a trivial assignment, not predictions for computation-heavy cells.
The shared endpoint has a measurable dispatch cost even when cells do not use native Python display or async behavior.

The RSS and percentile definitions remain those given above.
All direct variants had four processes in their snapshots.
The default sandbox produced six built-in versus five Jupyter processes on macOS, and twelve versus eight on Linux; fewer processes did not imply lower memory use.
For the Linux Jupyter variant, adding the default sandbox increased median startup by about 0.71 seconds and summed RSS by 66.7 MiB, with four additional processes.
That measures the whole sandbox path, including its deny-all proxy and private network namespace; it does not isolate the proxy's individual cost.
Shared pages, native libraries, IPython caches, and retained runtime state prevent summed RSS from being interpreted as unique physical memory or peak demand.

The measured macOS executable is 12,391,632 bytes, SHA-256 `9b8d63ba8fc4e001dfcd99a16176045d9770cf1913972d4cbe40eb449e789a52`.
The Linux executable is 14,293,960 bytes, SHA-256 `63706e96becdf1c7df3bc1211fef5c4ea921cbb0351f6d3a29e80baec6b56507`.
The native Linux checkout used an exact overlay of all 186 committed production source files; that match was checked against the commit archive.
The saved results add source, host, and validation provenance and normalize executable paths; all timings, memory observations, PIDs, and dependency versions are retained.
The implementation passed 27 applicable macOS cases, 23 native Linux cases, and the standard `scripts/check` gate before these measurements.

## Executable and distribution costs

The initial comparison's Console executable is about 11.80 MiB; the shared-endpoint executable measured above is about 11.82 MiB on macOS and 13.63 MiB on Linux.
The feature adds embedded Python source and Rust configuration/protocol glue; it adds no Rust dependencies and embeds no ark or ipykernel binary.
The built-in worker remains available in the same executable.
There is no Jupyter-only build that removes its code or dependencies.
A comparative clean-base binary and distribution wheels were not built for this experiment, so the exact change in executable or wheel bytes is unmeasured.

The optional `jupyter` extra installs the client dependencies into a Python environment.
Running an R or another non-Python kernel still requires a Python 3.11+ adapter environment.
ipykernel, ark, R, reticulate, and analysis packages are separate installations.
The [external footprint measurements](../JUPYTER.md#performance-and-distribution) include a 24,592,144-byte unpacked ark executable and approximately 12 MiB of installed client dependencies before the kernel or language runtimes.
Ark's published executable includes language/debug services that this adapter does not expose.
The 8,456,517-byte macOS arm64 ark download excludes R; the universal macOS archive is 17,196,572 bytes.
The [published release assets](https://github.com/posit-dev/ark/releases/tag/0.1.252) also require separate operating-system and architecture selection.

Bundling a self-contained Jupyter distribution would require Python, native pyzmq/ZeroMQ components, the chosen kernel, its language runtime, licenses/notices, and a larger compatibility/update matrix.
Keeping them external avoids adding that payload to Console's wheels, at the cost of more installation steps and environment-selection requirements.
The measured environment sizes overlap and exclude shared caches, interpreter installations, user packages, and later generated history/bytecode; they are not estimates of total download size or incremental physical disk use.

## Reproduce

Build and stage Console through the repository workflow, then select published installations:

```sh
MCP_CONSOLE_TEST_JUPYTER_PYTHON=/absolute/path/to/python \
MCP_CONSOLE_TEST_ARK=/absolute/path/to/ark \
MCP_CONSOLE_TEST_RETICULATE_LIBRARY=/absolute/path/to/R/library \
python3 benchmarks/jupyter.py --binary target/release/mcp-console \
  --variant builtin-python --variant ipykernel-ipc --variant builtin-r \
  --variant ark-tcp --variant ark-reticulate-python \
  --launches 5 --warm-cells 30 > jupyter-results.json
```

For the shared R/Python comparison, use the same environment variables and select its four variants:

```sh
MCP_CONSOLE_TEST_JUPYTER_PYTHON=/absolute/path/to/python \
MCP_CONSOLE_TEST_ARK=/absolute/path/to/ark \
MCP_CONSOLE_TEST_RETICULATE_LIBRARY=/absolute/path/to/R/library \
python3 benchmarks/jupyter.py --binary target/release/mcp-console \
  --variant builtin-r-python --variant ark-shared-ipykernel-python \
  --variant builtin-r-python-sandbox \
  --variant ark-shared-ipykernel-python-sandbox \
  --launches 5 --warm-cells 30 > jupyter-shared-results.json
```

Check that every variant reports `status: "ok"` and has five samples before summarizing the results.
The script records failed variants rather than substituting another runtime.
For supported behavior, sandbox requirements, lost features, export limits, and unvalidated platforms, use the [Jupyter guide](../JUPYTER.md#differences-from-the-built-in-worker).
