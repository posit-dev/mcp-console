# Jupyter worker comparison

The initial comparison was measured on October 23, 2026, on an Apple M4 Max with 16 logical CPUs and 128 GiB RAM, running macOS 26.7.1 arm64.
These measurements compare Console's built-in worker with the optional [Jupyter mode](../JUPYTER.md).
They describe this host, these installed releases, and trivial cells; they do not establish general performance parity.
The results retained here are the standalone-kernel subset of an experiment measured before this branch was split; they have not been remeasured on the split branch.

## Method and provenance

The initial comparison's implementation corresponds to commit `79b55739d12592fb1dde0e2858325bad0056bac3`.
The measured release executable is 12,375,056 bytes, with SHA-256 `8aa187a83b1b2764748c667867bdf4ae122462e42e4179c1fdf7075c8d452812`.
Its hash was checked again after the public acceptance tests.
The [benchmark program](../../benchmarks/jupyter.py) drives the public MCP connection and saves [individual measurements](../../benchmarks/results/jupyter-macos-arm64-2026-10-23.json).
The saved results retain the four standalone variants from the original five-variant experiment.
Only executable paths are normalized; their timing, memory, process IDs, version data, and original implementation revision are retained.

The installed releases were Python 3.13.16, R 4.6.1, jupyter_client 8.10.0, ipykernel 7.4.0, pyzmq 27.2.0, ark 0.1.252, and CRAN reticulate 1.47.0.
Ark is a published prerelease.
No development checkout of a kernel or reticulate was used.

The original experiment launched five fresh Console sessions per variant, in round-robin order across all five variants, and ran the identical assignment `x = 1` once followed by 30 warm repetitions.
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
The ipykernel variant used IPC, while ark used loopback TCP because the tested ark release does not use jupyter_client's IPC endpoint convention.

## Observations

| Variant         | First result median (s) | First result range (s) | Warm median (ms) | Warm p95 (ms) | Median summed RSS (MiB) |
| --------------- | ----------------------: | ---------------------- | ---------------: | ------------: | ----------------------: |
| Built-in Python |                   1.134 | 1.115–1.259            |            3.108 |         3.421 |                   129.0 |
| ipykernel, IPC  |                   0.385 | 0.378–0.453            |            5.503 |         6.001 |                   112.4 |
| Built-in R      |                   1.151 | 1.136–1.195            |            3.096 |         3.520 |                   129.8 |
| ark, TCP        |                   0.894 | 0.873–0.902            |            4.270 |         4.799 |                   163.6 |

Warm p95 is the linearly interpolated 95th percentile of all 150 warm cells for that variant.
RSS is a `ps` snapshot after warm execution, summed over Console and its descendants, then summarized across the five launches.
It excludes the benchmark client, is not peak memory, and can count shared pages more than once.
All variants had four live processes in these snapshots: the built-in path retained its preparation process, while the Jupyter path had separate adapter and kernel processes alongside Console and its relay.
Equal process counts do not imply equal responsibilities or memory ownership.

For this assignment, ipykernel's median warm call took about 1.77 times the built-in Python call; ark's R call took about 1.38 times the built-in R call.
The absolute differences were approximately 2.40 ms and 1.17 ms.
These comparisons measure dispatch overhead with very little language work.
They do not compare Python final-expression display.

Standalone ipykernel and ark reached their first result sooner than the built-in worker in this setup.
Jupyter is therefore not uniformly slower to start: the paths do different discovery, preparation, initialization, and protocol work.
None of these timings include a cold installation, sandbox startup, SQL, package activation, large text or image transfer, concurrent load, or a long numerical workload.
The subsequently added automatic private sandbox transports are outside these measurements: macOS adds a private-directory Unix-socket policy, while Linux starts a native deny-all proxy and private network namespace.
The Linux path therefore has additional setup and process costs that this direct-execution comparison does not quantify.
Image serialization, output queues, history writes, and extra protocol copies need their own workload measurements before selecting a mode for those cases.

## Executable and distribution costs

The initial comparison's Console executable is about 11.80 MiB.
The feature adds embedded Python source and Rust configuration/protocol glue; it adds no Rust dependencies and embeds no ark or ipykernel binary.
The built-in worker remains available in the same executable.
There is no Jupyter-only build that removes its code or dependencies.
A comparative clean-base binary and distribution wheels were not built for this experiment, so the exact change in executable or wheel bytes is unmeasured.

The optional `jupyter` extra installs the client dependencies into a Python environment.
Running an R or another non-Python kernel still requires a Python 3.11+ adapter environment.
ipykernel, ark, R, and analysis packages are separate installations.
The [external footprint measurements](../JUPYTER.md#performance-and-distribution) include a 24,592,144-byte unpacked ark executable and approximately 12 MiB of installed client dependencies before the kernel or language runtimes.
Ark's published executable includes language/debug services that this adapter does not expose.
The 8,456,517-byte macOS arm64 ark download excludes R; the universal macOS archive is 17,196,572 bytes.
The [published release assets](https://github.com/posit-dev/ark/releases/tag/0.1.252) also require separate operating-system and architecture selection.

Bundling a self-contained Jupyter distribution would require Python, native pyzmq/ZeroMQ components, the chosen kernel, its language runtime, licenses/notices, and a larger compatibility/update matrix.
Keeping them external avoids adding that payload to Console's wheels, at the cost of more installation steps and environment-selection requirements.
The measured environment sizes overlap and exclude shared caches, interpreter installations, user packages, and later generated history/bytecode; they are not estimates of total download size or incremental physical disk use.

## Reproduce

Build and stage Console through the repository workflow, then select published installations.
The R library must include published reticulate for the built-in worker baseline:

```sh
MCP_CONSOLE_TEST_JUPYTER_PYTHON=/absolute/path/to/python \
MCP_CONSOLE_TEST_ARK=/absolute/path/to/ark \
MCP_CONSOLE_TEST_RETICULATE_LIBRARY=/absolute/path/to/R/library \
python3 benchmarks/jupyter.py --binary target/release/mcp-console \
  --variant builtin-python --variant ipykernel-ipc --variant builtin-r \
  --variant ark-tcp \
  --launches 5 --warm-cells 30 > jupyter-results.json
```

Check that every variant reports `status: "ok"` and has five samples before summarizing the results.
The script records failed variants rather than substituting another runtime.
For supported behavior, sandbox requirements, lost features, export limits, and unvalidated platforms, use the [Jupyter guide](../JUPYTER.md#differences-from-the-built-in-worker).
