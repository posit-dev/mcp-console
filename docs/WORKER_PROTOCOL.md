# Worker protocol

This private interface connects a relay to one worker generation, including custom workers selected with `serve --worker PATH`.
It evolves in lockstep without an independent version.
Custom workers must implement the current build's contract, not a stable third-party API.

[`src/worker_protocol.rs`](../src/worker_protocol.rs) defines the exact message objects.
[`src/jsonl.rs`](../src/jsonl.rs), [`src/sideband.rs`](../src/sideband.rs), and the boundary tests define framing and endpoints.
This guide explains the protocol's phases and invariants without duplicating every nested field.

## Launch and transport

On macOS/Linux, the relay supplies stdin/stdout/stderr plus two sideband pipes named by `MCP_CONSOLE_SIDEBAND_READ_FD` and `MCP_CONSOLE_SIDEBAND_WRITE_FD`.
Adopt the descriptors, remove the variables, set close-on-exec, and close the endpoints in fork-only children without disturbing the parent.

Windows supplies decimal named-pipe handles through `MCP_CONSOLE_SIDEBAND_READ_HANDLE` and `MCP_CONSOLE_SIDEBAND_WRITE_HANDLE`, plus `MCP_CONSOLE_INTERRUPT_HANDLE` and `MCP_CONSOLE_INPUT_READY_HANDLE` events.
Adopt them, clear inheritance, and remove the variables before user code runs.

Each direction is ordered UTF-8 JSONL: one object and newline, flushed per frame.
Frames cannot interleave.
Unknown kinds/fields, wrong types, invalid UTF-8, malformed JSON, and partial-frame closure fail the boundary.
Nested objects are strict too.
No general frame-size limit is defined.

Stdin is one generation-long byte stream, not cell records.
Writes append exact UTF-8 bytes without newline or echo.
Payload end is not EOF; unread bytes can reach later reads.
There is no general stdin-queue limit.
Sideband endpoint leakage to descendants violates the closure contract; stdout/stderr inheritance is handled by bounded relay drainage.

## Message schemas

All objects have `kind`.
The source enums are the complete schema; these are the principal message families:

| Direction   | Purpose              | Messages                                                                                                                                          |
| ----------- | -------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| To worker   | Evaluation/control   | `evaluate`, `shutdown`                                                                                                                            |
| To worker   | Explicit preparation | `prepare_r`, `prepare_python`                                                                                                                     |
| To worker   | Resolver replies     | `r_resolved`, `r_resolution_failed`, `python_resolved`, `python_resolution_failed`, `python_version_resolved`, `python_version_resolution_failed` |
| From worker | Readiness/lifecycle  | `ready`, `runtime_initialized`, `r_initialization`, `completed`                                                                                   |
| From worker | Output/input         | `console_output`, `console_diagnostic`, `image`, `input_requested`, `input_received`, `input_cancelled`                                           |
| From worker | Preparation results  | `r_prepared`, `r_preparation_failed`, `python_prepared`, `python_preparation_failed`, `python_preparation_rejected`                               |
| From worker | Resolver requests    | `resolve_r`, `resolve_python`, `resolve_python_version`                                                                                           |
| From worker | Activation receipts  | `r_activated`, `r_activation_failed`, `python_activated`, `python_activation_failed`                                                              |

For example:

```json
{ "kind": "evaluate", "language": "python", "source": "2 + 2" }
```

```json
{ "kind": "console_output", "data": "4\n" }
```

```json
{ "kind": "completed" }
```

Ordinary language errors are text followed by completion when the worker remains usable.
There is no structured language-error result, poll, output acknowledgment, general request ID, or session name.
Interrupt delivery belongs to the outer relay; MCP response assembly belongs to the server.

Python environment replies contain inspected runtime identity and manifests, not rediscovery hints.
Runtime callbacks cannot supply arbitrary resolver programs or environment maps.
Public package declarations and private activation objects are different schemas.

## Readiness and operations

`ready` is the first semantic message and appears exactly once; early diagnostics can use raw streams.
For built-in workers it means services are connected, not that interpreters are initialized.
Bootstrap then uses the same interpreter thread and can produce output, input, and resolver requests before evaluation.

`runtime_initialized` ends built-in bootstrap and reports interruption.
The server withholds an already admitted cell's evaluation until bootstrap settles.
Controller interrupt admission, processing this receipt, and enqueueing `evaluate` share a short ordering point.
An interrupt admitted before dispatch withholds the pending cell even when the worker already sampled `interrupted = false`.
An interrupted receipt affects only the cell admitted before receipt processing, including an evaluator that has not attached yet.
Later cells do not inherit that withholding.
These transitions enqueue commands without waiting for pipe I/O or signal acknowledgment; acknowledgment confirms dispatch, not worker-side interruption or cell completion.
Native R startup reports its admission and successful completion through `r_initialization`; failed or interrupted R initialization requires explicit restart rather than in-place or automatic retry.
Custom workers need not send built-in bootstrap events.

Each admitted ordinary operation has one matching terminal result:

| Operation        | Success                                          | Ordinary failure                                              |
| ---------------- | ------------------------------------------------ | ------------------------------------------------------------- |
| `evaluate`       | `completed`                                      | Error text then `completed`, if usable.                       |
| `prepare_r`      | `r_prepared` with the normalized requested path. | `r_preparation_failed`.                                       |
| `prepare_python` | `python_prepared`                                | `python_preparation_rejected` or `python_preparation_failed`. |
| `shutdown`       | Process exit.                                    | No sideband acknowledgment.                                   |

Wrong, duplicate, unsolicited, or mismatched results fail the protocol.
Semantic output belonging to an operation precedes its result; later events are idle activity.
Independent stdout/stderr can be observed later regardless of their write time.

### Managed input

A managed read sends `input_requested` immediately before waiting, then exactly one `input_received` or `input_cancelled` before resuming/unwinding.
One request may be outstanding, including while idle.
Completion cannot precede termination of its managed input request.
Direct stdin readers emit no such events.

Preparation is noninteractive.
A managed input request during explicit preparation fails preparation and the worker.

### Nested managed-R resolution

A runtime `resolve_r` asks the host to prepare the retained declaration plus validated package names.
`r_resolved` is provisional: apply the library, then send matching `r_activated` before resuming the load.
Only that current-generation receipt commits it.
A later load or cell failure does not undo acceptance.

Report failed activation explicitly before propagating the language error; further requirement changes may need restart.
Distinguish ordinary host failure, resolver interruption, and boundary-ending operation failure.
Do not disguise transport failure as an ordinary missing package.

### Live Python preparation

The ordinary sequence is:

```text
prepare_python → resolve_python → python_resolved
               → python_activated → python_prepared
```

Before interpreter initialization, final preparation can accept a materialized selection without a live activation receipt.
After initialization, compatibility checks protect the running interpreter and loaded distributions.
Pre-mutation rejection leaves the accepted environment usable; unsafe mutation can require restart.

### Nested managed-Python resolution

Environment requests carry physical resolution requirements and logical retained requirements.
Version constraints may differ to pin physical resolution to the live interpreter.
A matching complete activation receipt commits the logical manifest; a version-only reply creates no candidate.

Accepted activation survives later import/cell/preparation failure.
Unaccepted candidates are discarded when the operation ends or the generation retires.
The relay never decides commits.

### Synchronous resolver waits

Only one nested resolver request may be outstanding.
Wait for its matching reply; shutdown terminates the wait.
An already queued evaluation can be retained until the callback finishes.
Wrong-kind, duplicate, or unsolicited replies fail the boundary.

Explicit preparation and idle callbacks share environment-change ownership.
Generation checks prevent old candidates from committing to a replacement.
See [Architecture](ARCHITECTURE.md#preparation-and-activation).

## Shutdown and closure

The relay closes stdin and attempts shutdown concurrently; workers cannot require a particular order or both signals.
Exit without acknowledgment.
The relay can forcibly terminate and reap the direct child after grace; descendants/private storage belong to the native runner.

Outside intentional retirement, unexpected sideband EOF or worker exit, including status zero, fails the generation.
Closure and direct-worker outcome do not prove native cleanup.

## Custom-worker conformance

`--worker PATH` selects one executable without arguments or shell parsing.
Implement framing, descriptor ownership, readiness, input, results, shutdown, and worker-defined interruption.
The hidden interface is for development and must match this build.

Custom workers have no built-in defaults or managed Python.
Explicit R/DuckDB preparation and optional runtime R callbacks must honor prepared `R_LIBS`, apply the managed R library before loading DuckDB, and use the supplied extension directory when set.

[`tests/fixtures/zod`](../tests/fixtures/zod) exercises conformance; fixture commands are not protocol extensions.
Use the [boundary tests](../tests/boundaries/README.md), not prose alone, to validate changes to either endpoint.
