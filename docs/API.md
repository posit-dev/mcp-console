# Public interfaces

Console's public interfaces are the MCP `send` tool, command-line configuration, and the R and Python client packages.
This page maps those contracts.
The project is a development preview; “public” identifies an interface users should be able to rely on and maintainers must document when changing, not a promise that it is frozen.

## MCP: `send`

One MCP connection owns one persistent session.
Send at most one complete source cell per call.
Collect its result before submitting another cell.

| Argument             | Meaning                                                                                                                     |
| -------------------- | --------------------------------------------------------------------------------------------------------------------------- |
| `r`, `python`, `sql` | Optional source string. At most one may be non-null. The configured language selection determines which fields are exposed. |
| `timeout_ms`         | Nonnegative integer; maximum observation wait, normally 60,000 milliseconds. It does not cancel execution.                  |
| `stdin`              | Optional string of exact input bytes encoded as UTF-8. No newline is added.                                                 |
| `control`            | `interrupt` or `restart`.                                                                                                   |
| `requirements`       | Dependency declaration or inspection action; see [Requirements](REQUIREMENTS.md).                                           |

Unknown fields and incorrect types are errors.
Omit unused arguments.
A hidden language field is rejected even when its value is `null`.
`timeout_ms` cannot be null.

Examples below are **tool argument objects**, not complete JSON-RPC messages:

```json
{ "python": "values = [2, 4, 6]; sum(values)" }
```

```json
{ "sql": "SELECT 6 * 7 AS answer", "timeout_ms": 0 }
```

```json
{ "timeout_ms": 300000 }
```

The last object polls without submitting code.
`{}` is an immediate idle poll or a wait for active work using the default observation budget; it is not a new evaluation.

```json
{ "stdin": "yes\n" }
```

```json
{ "control": "restart", "python": "answer = 42; answer" }
```

A control-only interrupt may overlap a pending call.
Requirements inspection can read the last committed declaration without consuming that call's output.
These exceptions do not enable parallel evaluation.

[Calls, input, and control](SEND_OPERATIONS.md) defines admission, timing, and partial effects.
[R, Python, and SQL](BUILTIN_RUNTIME.md) defines language behavior and connection helpers.

## Results

MCP results contain text and, when produced, images.
Language exceptions normally appear as console text; preparation, transport, and worker failures are tool errors.
Errors do not undo earlier side effects.

| Notice                               | Action                                                                                                    |
| ------------------------------------ | --------------------------------------------------------------------------------------------------------- |
| `[running; poll with an empty send]` | Poll without code. Do not resubmit the cell.                                                              |
| `[waiting for stdin]`                | Supply `stdin`, usually with a trailing newline.                                                          |
| `[worker starting]`                  | Startup is pending. Poll; do not assume interpreters are ready.                                           |
| `[done]`                             | The operation completed without additional display content, or a combined control-and-cell call finished. |
| `[idle]`                             | No evaluation is active. Startup hooks or later idle callbacks can still produce output.                  |
| `[prepared]`                         | Standalone dependency preparation succeeded.                                                              |

Notices may be accompanied by output, elapsed time, preparation phases, or diagnostics.
Treat the text as user-visible status rather than an independently versioned machine-state protocol.
The client packages do not turn it into a structured job API.

Text previews are bounded and can omit output.
[Recordings](RECORDING.md) explains retained logs and their limits.
Requirements inspection is different: its complete declaration is available in MCP `structuredContent`, even when the text preview is shortened.

## Output limits

| Output                                            | Limit                                                            |
| ------------------------------------------------- | ---------------------------------------------------------------- |
| Text in one complete response, including notices  | 8 KiB of UTF-8 text                                              |
| Images per undrained interval and complete result | 8 MiB of encoded data, 64 KiB of MIME metadata, and 4,096 images |
| SQL preview                                       | 20 rows and 12 columns; the response text budget still applies   |

Images are admitted whole, not partially returned.
Omission notices identify retained logs or artifacts, and say when omitted content was not retained.
Polling consumes the observed interval, including its omitted middle; use the named files rather than another poll to retrieve that text.
[Recordings](RECORDING.md) owns on-disk limits and failure behavior.

## Runtime helpers

The built-in runtimes expose the following session helpers:

| Helper                                                                          | Purpose                                                        |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Python `r.name`                                                                 | Access an R object through reticulate.                         |
| R `py$name`                                                                     | Access a Python object through reticulate.                     |
| R `.console$sql_connection()`                                                   | Get, select, or reset a native SQL connection owned by R.      |
| Python `_console.sql_connection()`                                              | Get, select, or reset a native SQL connection owned by Python. |
| R `console.plot.width_in`, `console.plot.height_in`, `console.plot.dpi` options | Configure captured plot dimensions and resolution.             |

The [runtime guide](BUILTIN_RUNTIME.md) defines ownership and examples.
These helpers do not make R and Python connection objects interchangeable.

## CLI and configuration

The [CLI reference](CLI.md) covers `serve` and `sandbox`.
[Configuration](CONFIGURATION.md) owns discovery, merge rules, interpreter selection, startup settings, and tool description/parameter overrides.
[Sandbox configuration](SANDBOX_CONFIGURATION.md) owns the public permission schema.

The low-level `sandbox --config-env NAME` interface explicitly selects the separately pinned native runner's complete JSON policy.
It is not another spelling of `config.yaml`.

## Client packages

Use [Python clients and adapters](PYTHON.md) or [R and ellmer](../r/README.md) to connect from application code.
Generated references document their exact signatures and connection lifetimes:

- [Python API](https://t-kalinowski.github.io/mcp-console/python/reference/index.html)
- [R API](https://t-kalinowski.github.io/mcp-console/r/reference/console_tool.html)

Register tools through the supplied adapters so the model receives the connected server's schema.
Inferring a schema from the Python `send` method's annotations loses configured capabilities.

## What is not a public compatibility contract

Hidden worker/relay/resolver subcommands, internal `MCP_CONSOLE_*` handoffs, process layouts, and recording journal schemas are implementation interfaces.
Console-owned private protocols evolve in lockstep; custom workers must match the current build.
The separately pinned native runner and external MCP protocol retain their own contracts.

Internal details belong in [Architecture](ARCHITECTURE.md), [Relay protocol](RELAY_PROTOCOL.md), and [Worker protocol](WORKER_PROTOCOL.md).
Open work is collected in [TODO](TODO.md); it is not a list of promised APIs.
