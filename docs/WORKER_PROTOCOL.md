# Worker protocol

This private JSONL interface connects a relay to one worker connection.
Both endpoints evolve with Console, without independent version negotiation.
Internal `--worker` and `--relay` overrides exist for testing; fixtures must match the build.

[worker_protocol.rs](../src/worker_protocol.rs) owns message types and fields; [jsonl.rs](../src/jsonl.rs) and [sideband.rs](../src/sideband.rs) own framing and transport.
This page explains the lifecycle those messages implement.

## Launch and transport

The relay supplies stdin/stdout/stderr and separate command/event pipes:

| Host        | Sideband endpoints                                                         |
| ----------- | -------------------------------------------------------------------------- |
| macOS/Linux | `MCP_CONSOLE_SIDEBAND_READ_FD` and `MCP_CONSOLE_SIDEBAND_WRITE_FD`         |
| Windows     | `MCP_CONSOLE_SIDEBAND_READ_HANDLE` and `MCP_CONSOLE_SIDEBAND_WRITE_HANDLE` |

Windows also supplies `MCP_CONSOLE_INTERRUPT_HANDLE` and `MCP_CONSOLE_INPUT_READY_HANDLE` events.
Adopt the supplied descriptors/handles, remove their environment variables, and prevent inheritance before user code runs.
Unix fork-only children must close sideband endpoints without disturbing the parent.
Leaking sideband ownership into descendants violates closure; inherited standard streams instead receive bounded relay drainage.

Each direction has one writer emitting complete UTF-8 JSON objects, each followed by a newline and flush.
Unknown kinds/fields, wrong types, invalid UTF-8, malformed JSON, and partial-frame closure fail the boundary; nested objects are strict too.
There is no general frame-size cap.

Stdin is a generation-long byte stream.
Writes append exact UTF-8 bytes without newline or echo; payload end is not EOF and unread bytes can reach later reads.
There is no general stdin-queue cap.
[TODO](TODO.md#runtime-and-resource-limits) tracks these resource-policy questions.

## Message schemas

Every frame has a `kind`.
The source enums define evaluation, preparation, resolver, input, output, and lifecycle messages.
An ordinary successful evaluation follows `evaluate`, `cell_started`, output events, then `completed`.
A usable worker emits `execution_error` before completion for an ordinary language failure; diagnostic text alone is not such a failure.

Ordinary cells carry no session name, poll, output acknowledgment, or general request ID.
One worker belongs to one session.
The relay delivers interrupts; the server assembles MCP responses and owns session admission.

## Readiness and operations

`ready` appears once as the first semantic message.
Raw streams can carry earlier diagnostics.
For built-in workers it means services are connected, not that interpreters are initialized.
Bootstrap uses the interpreter thread and can emit output, input, and resolver requests before evaluation.

`runtime_initialized` ends built-in bootstrap and reports interruption.
Interrupt admission, receipt handling, and enqueueing a pending cell share an ordering point: a pre-dispatch interrupt must withhold that cell even if bootstrap already sampled its interrupt flag.
Later cells do not inherit the withholding.
`r_initialization` reports native R entry/completion; incomplete R initialization requires explicit restart.
Test workers need not emit built-in bootstrap events.

`cell_started` confirms evaluator entry, after foreground ownership is established.
It must occur once before `completed`, including during retirement.
Admission alone does not open a cell log.
Completion closes its output interval; output observed later belongs to the post-cell interval.

| Operation        | Terminal receipt                                                                                   |
| ---------------- | -------------------------------------------------------------------------------------------------- |
| `evaluate`       | `completed`, with preceding `execution_error` for a language failure if the worker remains usable. |
| `prepare_r`      | Matching `r_prepared` or `r_preparation_failed`.                                                   |
| `prepare_python` | `python_prepared`, `python_preparation_rejected`, or `python_preparation_failed`.                  |
| `shutdown`       | Process exit, without a sideband acknowledgment.                                                   |

Wrong, duplicate, unsolicited, or mismatched receipts fail the protocol.
Semantic output belonging to an operation precedes its receipt.
Independent native streams may be observed later, regardless of when they were written.

### Managed input

A managed read emits `input_requested` immediately before waiting and exactly one `input_received` or `input_cancelled` before resuming or unwinding.
Only one can be outstanding, including while idle.
Its termination precedes cell completion.
Direct stdin readers emit no managed-input events.
Preparation is noninteractive; requesting managed input during preparation fails the operation and worker.

### Preparation and activation

Resolved environments are provisional.
For R, apply the matching `r_resolved` library and send `r_activated` before resuming the load.
For initialized Python, validate and activate the candidate before `python_activated` and final preparation success.
Before Python initialization, preparation can accept a materialized selection without a live activation receipt.

Python replies carry inspected identity and manifests, not arbitrary resolver programs, environment maps, or rediscovery hints.
Requests distinguish explicit declarations from inferred imports and physical resolution requirements from the logical retained declaration.
A matching activation commits the logical manifest; a version-only reply creates no candidate.
The host checks admission policy on every request, independently of runtime resolver capability.

Only one nested resolver request can be outstanding.
Wait for its matching reply; shutdown ends the wait.
Queued evaluation may wait until the callback finishes.
Wrong-kind, duplicate, or unsolicited replies fail the boundary.
Report activation failure before propagating the language error; do not disguise transport failure as a missing package.
Accepted activation survives later cell failure, while unaccepted candidates are discarded on operation completion or retirement.
See [preparation ownership](ARCHITECTURE.md#preparation-and-activation).

### Concurrent integrator requests

`evaluate_priority` carries its own request ID and R-reentrant or Python-thread execution mode.
One request may be outstanding per worker connection.
R services it on its owning thread; Python uses a background thread after initialization.
Managed output, language failures, and completion retain that request identity without ordinary cell-entry/completion events.
Native streams remain untagged.
Priority work cannot prepare dependencies or request managed stdin.
Retirement settles only the matching connection's result owner.
The [integrator guide](INTEGRATOR_CONTROL.md#opt-in-to-immediate-execution) describes admission and caller responsibilities; ingress wakeup mechanics belong in source.

## Shutdown and closure

The relay closes stdin and attempts shutdown concurrently; workers cannot require both signals or a particular order.
Exit without acknowledgment.
The relay can terminate and reap the direct child after grace; the native runner owns descendants and managed storage.

Outside intentional retirement, unexpected sideband EOF or worker exit, including status zero, fails the generation.
Closure and direct-child exit do not prove native cleanup.

## Test worker fixtures {#custom-worker-conformance}

`--worker PATH` selects one executable without arguments or shell parsing.
Fixtures implement framing, descriptor ownership, readiness, input, receipts, shutdown, and worker-defined interruption.
They have no built-in runtime defaults, managed Python preparation, or inherited SQL implementation.
Python selection, explicit R settings, and configured startup are unsupported with test workers or relays.
These overrides also apply to warmup.

Fixtures implementing R/DuckDB preparation must apply supplied `R_LIBS` before loading DuckDB and honor the supplied extension directory.
[Zod fixtures](../tests/fixtures/zod) exercise conformance; their commands are not protocol extensions.
Validate endpoint changes through the [boundary tests](../tests/boundaries/README.md).
