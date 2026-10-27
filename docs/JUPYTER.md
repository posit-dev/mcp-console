# Jupyter kernels

`mcp-console serve --jupyter-kernel NAME` runs an installed local Jupyter kernel instead of Console's built-in worker.
The MCP client submits native source through `send(code=...)` and keeps the same polling, stdin, interrupt, and restart controls.
This mode supports macOS and Linux.
Windows is rejected at launch; its inherited handles, interrupt events, and process retirement need a separate adapter implementation.

The adapter uses published `jupyter_client` and the selected kernelspec.
It does not bundle, patch, or build the kernel.
The tested releases are ark 0.1.252, ipykernel 7.4.0, jupyter_client 8.10.0, and pyzmq 27.2.0, with Python 3.13.16 and R 4.6.1 on macOS arm64 and Linux x86_64.
Ark 0.1.252 is a published prerelease.
Other standard local kernelspecs use the same protocol, but have not been validated here.

## Start ipykernel

Create a Python 3.11+ environment containing the client and kernel:

```sh
uv venv .venv-jupyter --python 3.13
uv pip install --python .venv-jupyter/bin/python \
  'jupyter_client==8.10.0' 'ipykernel==7.4.0'
```

Select that interpreter explicitly for the adapter:

```sh
mcp-console serve --no-config \
  --jupyter-python "$PWD/.venv-jupyter/bin/python" \
  --jupyter-kernel python3
```

The `python3` kernelspec provided by ipykernel uses that environment, unless a higher-priority kernelspec with the same name overrides it.
Inspect the resolved specification with `.venv-jupyter/bin/jupyter kernelspec list --json` when an existing Jupyter installation also defines `python3`.
This example uses the default read-only sandbox, including private kernel communication and temporary storage.
Host networking and ordinary workspace writes remain blocked.
Unless explicitly selected in the environment, IPython's profile/history and Matplotlib's cache use the worker's disposable temporary storage.
Install analysis libraries in the kernel environment before launch.
Console's optional `jupyter` Python extra installs `jupyter_client`, but not ipykernel, ark, R, or reticulate.

The MCP client can then submit:

```json
{ "code": "answer = 40\nanswer + 2" }
```

The next call can reuse `answer`.
Use `code` for native Python in this mode; `python`, `r`, and `sql` are hidden and rejected in this mode.

The Python clients accept the same field and can pass the kernel selection through their launch arguments:

```python
from mcp_console import MCPConsole

with MCPConsole(
    args=[
        "serve",
        "--no-config",
        "--jupyter-python",
        "/absolute/path/to/.venv-jupyter/bin/python",
        "--jupyter-kernel",
        "python3",
    ]
) as console:
    print(console.send(code="answer = 40\nanswer + 2"))
```

`AsyncMCPConsole.send()` also accepts `code`.
The Python framework adapters use the discovered tool schema.
The R `console_tool()` wrapper derives its input fields from that schema, but its constructor currently has no kernel-selection setting or extra server arguments.
Use an MCP client that accepts the `serve` flags to select Jupyter directly; the ordinary R constructor does not yet expose this launch mode.

## Start ark

Download the matching binary from the [published ark release](https://github.com/posit-dev/ark/releases/tag/0.1.252) and install R separately.
Use ark's `--install` command to register its kernel, or create `.jupyter/kernels/ark/kernel.json` explicitly:

```json
{
  "argv": [
    "/absolute/path/to/ark",
    "--connection_file",
    "{connection_file}",
    "--session-mode",
    "notebook",
    "--",
    "--vanilla"
  ],
  "display_name": "Ark R",
  "language": "R",
  "env": { "RUST_LOG": "error" }
}
```

On Linux, ark's installer also configures the R library loader path; a manually written kernelspec must make the selected R installation's shared libraries discoverable.
Use the following launch with the manually written spec:

```sh
JUPYTER_PATH="$PWD/.jupyter" \
mcp-console serve --no-config \
  --jupyter-python "$PWD/.venv-jupyter/bin/python" \
  --jupyter-kernel ark
```

No network or filesystem grants are needed for kernel startup with the default sandbox.
Console automatically selects private communication appropriate to the platform; see [Private kernel communication](#private-kernel-communication).
`--no-sandbox` remains available when host permissions are intended.

Submit native R through `code`:

```json
{ "code": "answer <- 40L; answer + 2L" }
```

## Configuration and ownership

| Option                     | Meaning                                                                                                                                     |
| -------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------- |
| `--jupyter-kernel NAME`    | Select a locally discoverable kernelspec by name.                                                                                           |
| `--jupyter-python PATH`    | Python 3.11+ with `jupyter_client`; defaults to `python3` on PATH. This is the adapter interpreter, not necessarily the kernel interpreter. |
| `--jupyter-transport auto` | Default: private Unix sockets on restricted macOS; private namespace TCP on restricted Linux; ordinary loopback TCP with host permissions.  |
| `--jupyter-transport tcp`  | Authenticated loopback TCP using the standard connection file.                                                                              |
| `--jupyter-transport ipc`  | Unix-domain sockets in private temporary storage; requires kernel IPC support.                                                              |

Kernel selection is currently a `serve` launch option, not a YAML setting.
Built-in `startup`, `python`, `r`, and `languages` settings conflict with this mode rather than silently changing meaning.
Use kernelspec `argv` and `env` entries for kernel initialization and environment selection.
Kernelspec discovery follows `jupyter_client`, including `JUPYTER_PATH`.
Arguments support `{connection_file}`, `{resource_dir}`, `{prefix}`, and tilde expansion; environment values support Jupyter's environment-variable substitution.
Kernelspecs are trusted launch configuration with executable and environment access.
Remote kernels, existing connection files, custom provisioners, and daemonizing launchers are outside this mode's contract.

Console owns the adapter; the adapter owns and reaps its direct kernel child.
It keeps the child in the existing process group instead of using `KernelManager`'s detached-session launcher.
Normal shutdown requests kernel shutdown, then terminates or kills an unresponsive child within the relay's retirement budget.
Kernel death wakes an idle or evaluating adapter and fails that worker; restart creates a fresh kernel and fresh connection credentials.
Readiness requires a matching kernel-information reply and IOPub status within 30 seconds; a missing heartbeat during startup does not imply process death.
Replies and idle messages remain correlated across readiness retries, so a delayed channel does not invalidate an earlier request.
Readiness forwards received startup text and errors through process stdout/stderr before announcing the worker ready.
Messages published before subscription and startup rich displays are not guaranteed to be captured.
Interrupting startup retires the acquired child and discards the pending source before evaluation.
Native sandbox supervision retains ownership of descendants.
Outside a sandbox, neither arbitrary kernel subprocesses nor an adapter killed externally with `SIGKILL` have a general descendant-cleanup guarantee.

## Assessment

Ark delegates R execution and graphics to a published kernel.
Standalone ipykernel provides Python expression display, tracebacks, magics, and async execution.
The private transports make these capabilities usable under Console's default read-only sandbox on macOS and Linux.
The main benefit is reuse of the kernels' language behavior and ecosystem; Console still owns MCP translation, sandbox policy, and lifecycle coordination.

The built-in worker should remain the default while managed dependencies, SQL sessions, Windows support, and the existing runtime contracts matter.
This implementation provides a practical alternative for preinstalled R/Python environments, but replacing the default would remove those features and add installation, compatibility, output-delivery, and operational costs described below.
It also increases the number of externally selected components; it does not currently produce a smaller Console distribution.

## Differences from the built-in worker

| Area             | Jupyter behavior and limitation                                                                                                                                                                                                                                                                                                   |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Languages        | One kernel's native language per session. The adapter does not combine kernels or expose a cross-language bridge.                                                                                                                                                                                                                 |
| R/Python objects | Console's custom cross-language helpers and conversion behavior are not installed. Standalone ipykernel has no automatic R bridge.                                                                                                                                                                                                |
| Packages         | No host-side preparation, automatic missing-import installation, live dependency activation, version selection, or managed DuckDB extensions. `requirements={action: "get"}` reports an empty Console declaration, not an installed-package inventory. Mutation requests fail before code, input, or control effects.             |
| SQL              | No `sql` source field, selected connection registry, automatic data-frame registration, or retained last-query result. Installed language packages can execute SQL explicitly.                                                                                                                                                    |
| Startup          | Lazy kernel launch on the first operation requiring it. Kernelspec launch failures and a 30-second readiness timeout fail the worker. Built-in startup scripts and interpreter discovery do not run. `warmup` does not select or exercise this mode; a configured client's background warmup may only validate its MCP handshake. |
| Text and errors  | Kernel streams and tracebacks are preserved. Ordinary language errors complete the cell without an MCP tool error, as in the built-in worker. Formatting, ANSI escapes, warnings, execution counts, and traceback frames differ by kernel/version.                                                                                |
| Rich output      | PNG, JPEG, GIF, and WebP are forwarded. Otherwise the kernel's `text/plain` representation is used. HTML, SVG, PDF, JavaScript, widgets, comms, debugger/UI integrations, and shell payloads are not implemented. Displays without a supported representation produce an omission notice.                                         |
| Display updates  | `update_display_data` appends a marked update; `clear_output` adds a notice and retains earlier output. Previously delivered MCP content cannot be replaced. There is no persistent notebook display-ID model.                                                                                                                    |
| Input            | Jupyter `input_request` receives one newline-terminated UTF-8 reply. Partial lines wait; extra lines remain queued. Direct kernel reads from OS stdin see EOF. Password prompts use the same recorded stdin interface; it is not a secret-input channel.                                                                          |
| Interrupt        | The adapter uses the kernelspec's signal or control-message interrupt mode. Kernel and native-library behavior determine responsiveness.                                                                                                                                                                                          |
| Ordering         | Completion waits for the matching shell reply and IOPub idle, independent of their arrival order. Late output remains visible, but output from background work may arrive after a cell or during another cell; Jupyter does not supply a universal cross-channel chronology.                                                      |
| Output volume    | Console's response and recording limits still apply after translation. The adapter bounds its event queue by count, but Jupyter messages, ZeroMQ queues, MIME bundles, and queued stdin have additional allocations. This is not an end-to-end bounded-memory guarantee.                                                          |
| Platforms        | Native ark and ipykernel were tested on macOS arm64 and Linux x86_64, including the default read-only sandbox. Native Windows is unavailable. Other kernels require their own validation.                                                                                                                                         |

Jupyter IOPub uses ZeroMQ publish/subscribe delivery.
[ZeroMQ can drop published messages](https://libzmq.readthedocs.io/en/latest/zmq_socket.html#zmq-pub) when a subscriber's queue reaches its high-water limit; the adapter has no replay or delivery acknowledgment mechanism.
Consequently, a kernel that floods output can lose output before Console records it, and a lost terminal status can leave the operation pending until restart.
The acceptance tests do not establish lossless delivery under sustained output overload.

## Private kernel communication

The default `auto` transport allows the adapter and kernel to communicate without granting access to host services or external networks.
Console adds the required private communication policy only to Jupyter worker launches.
The built-in worker and standalone sandbox commands retain their existing permissions.

On macOS, the kernel uses filesystem Unix sockets beneath the native runner's private temporary directory.
A Seatbelt extension permits binding and connecting only beneath that directory; TCP and host Unix sockets remain denied.
The runner supplies the directory to the policy, rather than accepting its location from the workload environment.
The adapter creates local aliases for published ark's `prefix:port` naming and Jupyter's `prefix-port` naming, without changing either kernel.
Private IPC requires `sandbox.tempdir: managed`, the default.
Unix socket paths have OS length limits; unusually long temporary-directory paths can prevent startup.
Published ipykernel also attempts a TCP socket for forwarding output from forked children, even when its Jupyter channels use IPC.
macOS denies that optional socket; ipykernel warns and disables that forwarding.
Do not assume that Python output from forked children is captured.
Its shutdown may also print a `psutil` permission traceback while enumerating processes; Console and the native supervisor still retire the worker.

On Linux, the pinned runner's restricted syscall policy cannot selectively permit filesystem Unix sockets.
Console instead requests its existing managed-network path with a deny-all proxy, which creates a private network namespace.
The kernel and adapter communicate over that namespace's loopback TCP.
Host loopback, external routes, and host Unix sockets remain inaccessible; the proxy permits no destinations, SOCKS connections, or upstream proxy.
This adds native proxy/namespace setup and processes even though the session cannot access the network.
It does not grant `allow_local_binding` on the host or change filesystem permissions.
Explicitly configured network permissions retain their selected behavior.

The standard `python -m ipykernel_launcher` kernelspec receives a Console-owned bootstrap inside that namespace.
Published libzmq 4.3.5 enumerates interfaces when binding a numeric TCP address and aborts when the sandbox denies its `AF_NETLINK` socket.
The bootstrap uses pyzmq's socket subclass hook to bind to the namespace's interfaces without that enumeration.
It runs only after native namespace isolation is selected; it is never used with host networking or `--no-sandbox`.
Custom kernel launchers keep their original command and may need their own compatibility handling.
The published Jupyter client's interface-discovery warning is retained.

TCP connections use private credentials and HMAC authentication without encryption.
The default Linux namespace supplies isolation from host networking; it does not make the kernel and adapter separate trust domains.
Kernel startup warnings remain visible, including ipykernel's TCP warning.
Restart discards the kernel's private temporary files and default IPython history along with interpreter state.
Use explicit writable storage if history must outlive the worker.
Matplotlib's default font cache is also disposable, so the first plot after restart may rebuild it and take substantially longer than a warm plot.

## History and exports

Console retains translated text, images, source, input, and lifecycle evidence using its existing recording machinery.
Generic `code` is saved as text.
Jupyter session QMDs identify the kernelspec, contain non-executable source fences, and set `execute.eval: false`.
They are source archives with no promise of IR or Quarto replay.
They do not lock kernel versions, interpreter versions, environments, or external inputs.

Native ipykernel also maintains IPython history when its configured profile is writable.
IPython adds its own namespace entries and in-memory output cache, so displayed results can remain referenced after a cell completes.
That cache is separate from Console's response and recording limits and can retain large objects.
Other kernels own their own history behavior.
Console does not currently expose Jupyter `history_request`, export `.ipynb`, retain complete MIME bundles/raw Jupyter messages, or save widget/comm state.
Execution counts and display metadata, including display IDs, are not retained as structured fields.
A future notebook exporter could use source, execution replies, and display messages directly.
The current archives are not live-session checkpoints.

## Performance and distribution

The extra Python adapter and ZeroMQ channels add another protocol hop between Console and the interpreter.
Messages incur Jupyter framing and authentication, then translation into Console's existing JSON sideband protocol.
Base64 images pass through both protocols; the adapter does not eliminate the encoding and recording costs of the built-in worker.
Kernel startup also includes its own history, display, debugger, event-loop, and import machinery.
For long computations these costs may be small relative to execution; short cells and large MIME output are more sensitive.
The reproducible [Jupyter benchmark](../benchmarks/jupyter.py) compares the completed MCP path with matched trivial assignments and records startup, warm latency, and process RSS.
Its results are workload- and host-specific; kernel-only timings are not substitutes for an end-to-end comparison.
The [initial October 23 comparison](benchmarks/jupyter.md) records all samples and limitations: warm medians were 3.10–3.11 ms for the built-in worker, 5.50 ms for ipykernel, and 4.27 ms for ark.
Standalone kernels started sooner in that setup.
Startup remains sensitive to host load: a concurrent macOS test run exceeded ark's 30-second readiness budget.
Direct ark probes reproduced the slowdown outside Console; samples showed its R-discovery launcher stalled at the process loader before entering R.
This observation does not establish a kernel defect or a general startup bound.
The runtime deadline remains unchanged; the final macOS kernel verification uses limited test concurrency.

No new Rust dependencies or kernel binaries are linked into Console for this mode.
The adapter is embedded Python source plus Rust launch/schema glue.
The built-in worker remains compiled in; this mode provides no smaller Jupyter-only executable and removes none of the existing runtime dependencies.
The optional Python extra moves Jupyter client dependencies into the selected Python environment; it does not make the executable a standalone Jupyter distribution.

Measured external footprints on this macOS arm64 host:

| Component                                |                  Size |
| ---------------------------------------- | --------------------: |
| Published ark arm64 ZIP                  |       8,456,517 bytes |
| Unpacked ark executable                  |      24,592,144 bytes |
| `jupyter_client` 8.10.0 and dependencies |  12,448 KiB allocated |
| ipykernel + client + NumPy               |  97,876 KiB allocated |
| The same environment plus matplotlib     | 162,180 KiB allocated |

Directory figures are `du -sk` observations of temporary installations, not wheel download sizes or incremental physical disk use.
The Python environment rows overlap: the ipykernel and matplotlib rows already include the client dependencies and should not be summed.
They exclude Python, R, user data/packages, download caches, and later history/bytecode caches; uv may share cached files.
Ark's release also distributes separate macOS x64/universal, Linux arm64/x64, and Windows arm64/x64 archives.
Bundling these components would add platform selection, native pyzmq libraries, interpreter/R installations, notices/licenses, updates, and compatibility testing.
Keeping them external makes installation less self-contained and requires the caller to select mutually compatible releases.

## Validation and upstream contracts

Public acceptance cases exercise native ipykernel/ark evaluation, persistence, language errors and recovery, image output, prompted input, interruption, restart, unexpected kernel exit, process retirement, large cells, transport selection, and source archives.
They require explicitly supplied published installations:

```sh
MCP_CONSOLE_TEST_JUPYTER_PYTHON=/absolute/path/to/python \
MCP_CONSOLE_TEST_ARK=/absolute/path/to/ark \
scripts/test client_server/jupyter/test_python client_server/jupyter/test_ark \
  client_server/jupyter/test_macos_sandbox \
  client_server/jupyter/test_linux_sandbox
```

Missing capabilities are reported as skips.
The ordinary final gate does not substitute for these optional kernel cases or validation on another platform.

The [Jupyter messaging specification](https://jupyter-client.readthedocs.io/en/stable/messaging.html) owns the shell, IOPub, stdin, control, history, and display contracts.
The [kernelspec documentation](https://jupyter-client.readthedocs.io/en/stable/kernels.html) describes local launch metadata.
Relevant published implementation details include [ark's endpoint naming](https://github.com/posit-dev/ark/blob/0.1.252/crates/amalthea/src/connection_file.rs) and [jupyter_client's process launcher](https://github.com/jupyter/jupyter_client/blob/v8.10.0/jupyter_client/launcher.py).
