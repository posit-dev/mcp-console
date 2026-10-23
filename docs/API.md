# MCP send reference

For clients that call Console or expose its tool to a model.
To configure an existing MCP client, start with [Get started](getting-started.qmd).
The project is a development preview; public interfaces can change, with their changes documented.

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
| `requirements`       | Dependency declaration or inspection action; see [Requirements](#requirements).                                             |

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

For a client walkthrough, see [Drive send directly](SEND_OPERATIONS.md).
[Calls and session lifetime](#calls-and-session-lifetime) below defines admission, timing, and partial effects.
[Language behavior](RUNTIME_REFERENCE.md) defines code execution behavior and connection helpers.

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
[Recording reference](RECORDING_REFERENCE.md) explains retained logs and their limits.
Requirements inspection is different: its complete declaration is available in MCP `structuredContent`, even when the text preview is shortened.

## Output limits

| Output                                            | Limit                                                            |
| ------------------------------------------------- | ---------------------------------------------------------------- |
| Text in one complete response, including notices  | 8 KiB of UTF-8 text                                              |
| Images per undrained interval and complete result | 8 MiB of encoded data, 64 KiB of MIME metadata, and 4,096 images |
| SQL preview                                       | 20 rows and 12 columns; the response text budget still applies   |

Images are admitted whole, not partially returned.
These response limits are independent of raw image capture; [recordings](RECORDING_REFERENCE.md#output-intervals) can retain images omitted from a response.
Omission notices identify retained logs or artifacts, and say when omitted content was not retained.
Polling consumes the observed interval, including its omitted middle; use the named files rather than another poll to retrieve that text.
[Recording reference](RECORDING_REFERENCE.md) owns on-disk limits and failure behavior.

## Calls and session lifetime

### Cells and polling

```json
{ "python": "answer = 6 * 7; answer" }
```

If the response says `[running; poll with an empty send]`, send `{}` to collect more output.
Do not resubmit the source: the accepted cell continues and is not replayed.
New code is rejected while a cell or its uncollected result is active.

`timeout_ms` defaults to 60,000 and limits observation, not execution.
`0` observes immediately.
A long poll such as `{"timeout_ms": 300000}` returns early on completion or managed input; it avoids repeated short polls.

Completion returns text/images, or `[done]` when empty.
An idle poll returns pending idle output and `[idle]`.
Elapsed/phase notices describe observations, not a queue position or completion guarantee.

### Server readiness

MCP initialization, tool discovery, and ping do not wait for runtime preparation.
A valid early cell can be accepted while the shared startup continues.
Its observation deadline includes startup; timeout does not cancel it or create another worker.

An empty poll or requirements inspection can return `[worker starting]`.
Standalone requirements wait for shared startup and preparation, including initial discovery, without a `timeout_ms` deadline.
Once transport is ready, `[idle]` does not necessarily mean every interpreter startup hook has finished.

After initial setup failure, fix the reported problem and explicitly restart.
Ordinary calls retain the failure rather than retrying setup.
Rejected code and input are not replayed.

### Standard input and managed reads

`stdin` queues exact UTF-8 bytes.
It adds no newline, echoes nothing, and does not close the stream or acknowledge consumption.
Line-oriented input normally needs `\n`:

```json
{ "stdin": "yes\n" }
```

R `readline()` / `browser()` and main-thread Python `input()` / `pdb` can report `[waiting for stdin]`.
Answer with `stdin`, not another code cell.
In R's debugger, for example, `c\n` continues and `Q\n` quits.

Unread bytes can satisfy later reads and are discarded on restart.
Direct fd-0 and descendant reads bypass managed-input notifications.
Queue order does not determine which reader consumes the bytes.

### Interruption

```json
{ "control": "interrupt" }
```

Interrupt signals the active resolver, otherwise the current worker.
It does not start a missing worker or retarget a replacement.
Interruption is cooperative: code can catch, delay, or block it.
Use restart when fresh state is required.

On Windows, R-owned DuckDB supports native query interruption; long native Python-owned DuckDB queries can require restart.

A control-only interrupt may overlap a pending call.
If that call owns polling or output delivery, interrupt returns a running notice without consuming its output.
This is a control exception, not support for concurrent cells.

### Explicit restart

```json
{ "control": "restart" }
```

Restart prepares changed requirements before retiring the old worker.
Failed preparation preserves the old state.
After retirement begins, replacement failure cannot restore live objects.
Accepted requirements and recordings survive; language objects, database catalogs, and unread input do not.

A bundled cell or stdin reaches only the replacement after it is ready.
Native R startup and configured session startup can run again and repeat side effects.
No failed cell is automatically replayed.

### Operations

Prefer separate calls for control, dependency changes, and execution unless bundling serves a clear purpose.

| Call                                           | Effect order                                                                                                           |
| ---------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------- |
| Cell, with optional requirements/stdin         | Validate and prepare requirements, then admit the cell and deliver input.                                              |
| Requirements only                              | Validate and prepare; return `[prepared]`. Nonempty stdin is invalid.                                                  |
| Interrupt + cell                               | Deliver interrupt and input, allow interruption to settle, then prepare/admit the new cell only if prior work stopped. |
| Restart, with optional requirements/stdin/cell | Validate and prepare, commit requirements, retire, start replacement, deliver input/code.                              |
| Empty poll                                     | Collect output; do not start an initial or stopped worker.                                                             |
| Stdin only                                     | Queue input for the current generation and observe; after shared startup, it can start an initial/stopped worker.      |
| Requirements `get`                             | Read the committed declaration without consuming output.                                                               |

Structural errors are rejected before actions.
In the supported interrupt-plus-cell path, deferred requirement-content or policy errors occur **after** interrupt/input effects; those effects are not rolled back.
Interrupt with requirements but no cell is invalid.
Python-only sessions reject interrupt-plus-requirements entirely.

Changed `set`/`reset` normally needs explicit restart after user execution.
A still-unused prewarmed worker has a limited replacement exception; configured session startup or nonempty user input ends it.
See [Requirements actions](#inspecting-and-replacing-requirements).

### Wait timing and cancellation

The observation deadline starts at call entry.
After startup, explicit preparation and control/retirement work can make the total call exceed it.
Standalone package preparation, including initial discovery, has no `timeout_ms` deadline.

Cancelling a request's wait after admission does not cancel the accepted cell or shared startup.
Poll to discover retained state.
Cancelling before admission leaves the cell unaccepted.
Closing the MCP connection instead triggers owned cleanup.

Outputs are bounded and consumed by polling.
Recovery after cancellation is not exactly-once client delivery: bytes can reach a client before cancellation settles.
A recorded result proves assembly, not receipt.
Developer details belong to [Architecture](ARCHITECTURE.md#output-and-delivery).

## Requirements

MCP argument objects, admission rules, and live package changes.
For an introduction to package preparation, read [Packages and environments](REQUIREMENTS.md).
For startup declarations and policy settings, see [Package settings](PACKAGE_CONFIGURATION.md#startup-package-declarations).
Accepted declarations are not installed-package inventories or lockfiles.

### Inspecting and replacing requirements

The examples are MCP argument objects:

```json
{ "requirements": { "action": "get" } }
```

```json
{ "requirements": { "python": ["requests[socks]>=2,<3"] } }
```

```json
{
  "control": "restart",
  "requirements": { "action": "set", "python": ["requests>=2"] }
}
```

```json
{ "control": "restart", "requirements": { "action": "reset" } }
```

| Action          | Meaning                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------- |
| `get`           | Inspect the last committed declaration without consuming output or launching a worker.            |
| `add` (default) | Accumulate requirements. Exact repeats do nothing.                                                |
| `set`           | Replace the complete declaration. Omitted lists and constraints are empty; no defaults are added. |
| `reset`         | Restore the startup declaration captured from configuration, removing later additions.            |

`get` must be used alone: no code, control, stdin (even empty), or declaration fields.
`reset` accepts no declaration fields.
A bare requirements object `{}` is invalid; `{"action":"set"}` intentionally clears optional declarations.

Changed `set` or `reset` needs explicit restart after a worker has accepted user code or nonempty input.
An unused prewarmed worker can be replaced automatically after successful preparation.
Unchanged requirements do not wait for bootstrap.
When R is available, changed requirements wait for runtime bootstrap to finish; supply any requested startup input before changing requirements.
Configured session startup code and failed/interrupted native R startup end that exception.
An explicitly requested restart still occurs for unchanged requirements.

Inspection returns `requirements`, `prepared`, and `runtime_requirements` in structured content.
`requirements` is the editable user declaration; `runtime_requirements` identifies additional runtime infrastructure.
Skipped built-in defaults are absent from the accepted declaration.
Plain restart retains that accepted declaration; `reset` retries the captured startup defaults under the configured resolution policies.
During resolution, inspection reports the last commit, not an in-progress candidate.
During initial startup it may instead return `[worker starting]`.

Copy and edit the complete `requirements` object, then add `action: "set"` to replace it.
Removing a declaration neither uninstalls a package nor prohibits its use; implicit resolution can add it again.

### Accepted requirement input

| Field            | Accepted values                                                                                                                                       |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`              | Single-line `ir` package references, including supported remote references. Local sources, NUL, and line breaks are rejected.                         |
| `python`         | Named PEP 508 registry requirements, with versions, extras, and markers. No paths, editable requirements, URLs, archives, or `name @ URL` references. |
| `duckdb`         | Extension names of at most 64 characters: lowercase ASCII letter first, then lowercase letters, digits, or underscores.                               |
| `python_version` | A list of versions or supported `==`, `!=`, `<`, `<=`, `>`, `>=` constraints, not executable paths.                                                   |
| `exclude_newer`  | A publication cutoff accepted by uv, such as `"2026-01-01"`, or null.                                                                                 |

Lists contain strings, not null.
`add` accepts at most 64 entries per language per call; `set` accepts a complete accumulated manifest without that per-list limit.

`add` accumulates Python version constraints and can fill an unset cutoff, but cannot replace an existing cutoff.
`set` clears omitted constraints and cutoffs.
`reset` restores the configured baseline.
Effective version-constraint changes on a live interpreter require restart.

### Requirements for a cell

Requirements may accompany one cell or stand alone.
Failed preparation withholds the accompanying cell.
Standalone success returns `[prepared]`; standalone preparation rejects nonempty stdin.
Its wait includes initial runtime discovery and is not limited by `timeout_ms`.

```json
{
  "requirements": { "python": ["polars>=1"] },
  "python": "import polars as pl; pl.DataFrame({'value': [1, 2, 3]})"
}
```

For ordinary calls, preparation precedes bundled input and code.
Supported interrupt-plus-cell calls are different: the interrupt and input are delivered before deferred requirement validation.
Those effects are not rolled back when the follow-up fails.
Python-only sessions reject interrupt-plus-requirements combinations.
See [operation order](#operations).

### Automatic R package resolution

Under `implicit`, a reached missing package load can request preparation through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`.
Console does not scan source or rerun the cell.

Automatic requests accept plain package names.
Use explicit requirements for remote references.
Available packages, explicit `lib.loc`, and ordinary help/listing operations retain their native behavior.
`install.packages()` is not intercepted.

Failure preserves the original loading operation's behavior, including `FALSE` from a missing-package `require()` or `requireNamespace()`.
A successfully accepted library remains available even if the subsequent load or cell fails.

### Python declarations from R

For managed Python, evaluated `reticulate::py_require()` calls count as explicit dependency requests under `explicit` and `implicit`, including calls from R package `.onLoad()` hooks.
Package extras, version constraints, and publication cutoffs retain their declared meaning.

```r
reticulate::py_require("requests[socks]>=2.32,<3", python_version = ">=3.10,<4")
requests <- reticulate::import("requests")
```

Before Python initializes, declarations remain local to the worker until preparation succeeds.
They can be prepared on first Python use or together with a later MCP requirements request.
Requirements inspection reports the last accepted declaration, including R-side requirements after successful preparation.

After initialization, compatible additions use [live Python preparation](#live-python-preparation) and preserve live objects.
Reticulate's declaration checks still apply: changing existing package requirements, removing packages, changing the publication cutoff, or requesting an incompatible interpreter requires restart.
Resolution failure preserves accepted requirements and live state.
The `fixed` policy continues to reject changes; existing Python environments retain their native reticulate behavior.

### Automatic Python import resolution

Managed Python defaults to `explicit`: declare dependencies through `requirements.python` or `reticulate::py_require()` before importing them.
Set `python.resolution: implicit` in `config.yaml` to let a last-chance import finder prepare a missing distribution.
Console registers this dependency-resolution finder only under `implicit`.
Under `explicit` and `fixed`, missing imports retain native Python errors without distribution inference or a preparation request.
Display, plotting, and reticulate interoperability hooks remain active independently of this policy.
Existing standard-library, installed, local, and loaded modules retain normal lookup.
Availability checks such as `find_spec()` do not install packages.

Known mappings include `yaml` → `pyyaml`, `PIL` → `pillow`, and `sklearn` → `scikit-learn`.
Otherwise inference uses a conservative same-name fallback.
It declines ambiguous shared namespaces, missing submodules of an available package, and absent standard-library modules.
Missing optional imports during an installed package's eager initialization retain ordinary behavior.

Use explicit requirements for the correct distribution, versions, extras, or markers.
Preparation is available only on the configuring worker thread and process; prepare dependencies before starting background threads or fork children.
Existing Python environments and bare workers do not use this finder.

An accepted environment survives a later import error.
The cell is never replayed.

### Live preparation

Changed additions require an idle worker, except automatic requests within the worker's own execution.
Preparation is noninteractive; finish managed input or debugger work first.

#### Live R preparation

Console changes its managed library entry while preserving other library paths and live objects.
Loaded namespaces are not unloaded.
Some activation failures leave recoverable state but require restart before further changes.

#### Live Python preparation

A compatible addition preserves objects and the running interpreter.
It must match the loaded Python library and version and must not change a loaded distribution's version.
A new package can still change unloaded or transitive dependencies.
Use restart for changes that need fresh imports or a different environment.

Live R and Python activations have separate acceptance points: a successful Python activation can remain after a later R failure.
Arbitrary startup-hook effects are not rolled back.

#### DuckDB extension preparation

Preparation uses the selected engine's installation and signature checks.
An extension-only addition does not replace a SQL connection or catalog.
A Python replacement that retains extension requirements must also supply a usable DuckDB provider; include `duckdb` explicitly when needed.

### Restarting with requirements

Console prepares and accepts the complete candidate before retiring the old worker.
Preparation failure preserves the old worker and declaration and withholds same-call input/code.
Once retirement begins, a replacement failure cannot restore live state.
A plain restart reuses accepted requirements without resolving again.

Downloads, builds, and cache writes can remain after any failed preparation.
Accepted declarations and filesystem effects have different lifetimes.

### Resolution policy admission

Configuration chooses a policy independently for R and managed Python:

| Policy                         | Prepare startup declaration | Explicit requests | Automatic runtime additions |
| ------------------------------ | --------------------------- | ----------------- | --------------------------- |
| `implicit`                     | Yes                         | Yes               | Yes                         |
| `explicit`                     | Yes                         | Yes               | No                          |
| `fixed`                        | Yes                         | No                | No                          |
| R `disabled` / existing Python | No                          | No                | No                          |

Unchanged declarations remain no-ops.
Explicit requests include MCP `add` / `set` changes and managed Python declarations through `reticulate::py_require()`.
Under `fixed`, `reset` can retry skipped startup defaults.
A mixed request is checked as a whole before preparation.
`set` cannot omit a locked language to clear it, and `reset` or restart cannot unlock a policy.
A policy change requires a new server connection.

DuckDB extension preparation follows its managed provider's policy.
Selecting another SQL connection does not grant preparation authority.
These policies govern Console's resolver, not arbitrary installer code in cells or network permissions.

## Runtime helpers

The built-in runtimes expose the following session helpers:

| Helper                                                                          | Purpose                                                        |
| ------------------------------------------------------------------------------- | -------------------------------------------------------------- |
| Python `r.name`                                                                 | Access an R object through reticulate.                         |
| R `py$name`                                                                     | Access a Python object through reticulate.                     |
| R `.console$sql_connection()`                                                   | Get, select, or reset a native SQL connection owned by R.      |
| Python `_console.sql_connection()`                                              | Get, select, or reset a native SQL connection owned by Python. |
| R `.console$sql_last_result(n = NULL)`                                          | Retrieve successive rows from a SQL result owned by R.         |
| Python `_console.sql_last_result(n=None)`                                       | Retrieve successive rows from a SQL result owned by Python.    |
| R `console.plot.width_in`, `console.plot.height_in`, `console.plot.dpi` options | Configure captured plot dimensions and resolution.             |

[Language behavior](RUNTIME_REFERENCE.md) defines ownership and examples.
These helpers do not make R and Python connection objects interchangeable.

## CLI and configuration

See [Command-line reference](CLI.md), [Configuration files](CONFIGURATION.md), and [Tool configuration](TOOL_CONFIGURATION.md).

## Client packages

The [Python](PYTHON.md) and [R](R.md) guides cover the client packages; [Reference](REFERENCE.md#apis-and-package-manuals) links their API documentation.
Register tools through the supplied adapters so the model receives the connected server's schema; inferring it from Python method annotations loses configured capabilities.

## What is not a public compatibility contract

Hidden worker/relay/resolver subcommands, internal `MCP_CONSOLE_*` handoffs, process layouts, and recording journal schemas are implementation interfaces.
Console-owned private protocols evolve in lockstep.
The separately pinned native runner and external MCP protocol retain their own contracts.

Internal details belong in [Architecture](ARCHITECTURE.md), [Relay protocol](RELAY_PROTOCOL.md), and [Worker protocol](WORKER_PROTOCOL.md).
Open work is collected in [TODO](TODO.md); it is not a list of promised APIs.
