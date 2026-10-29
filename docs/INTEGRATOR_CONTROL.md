# Internal integrator control channel

This is an internal, experimental interface with no stability or compatibility promises.
The `--control-socket` argument is hidden from CLI help.
Integrators must use the protocol matching their Console build.

An application can inspect and evaluate code in Console's `default` session through a local socket while its agent uses MCP.
Use this channel for application setup, UI state, and output previews that do not need to become model tool calls.
All control requests target `default`; this interface has no session selector.
Control requests and MCP calls targeting `default` share the same worker, objects, and sandbox permissions.
Named sessions created through MCP have separate workers and state.

## Start the socket

Pass `--control-socket PATH` to `mcp-console serve` on Unix, including macOS and Linux.
The option is disabled by default and unavailable on Windows.
The socket's parent directory must already exist, belong to the current user, and grant no permissions to other users; create it with mode `0700`.
Console creates the socket with mode `0600` and refuses an existing path, including a stale socket.

For example, an application using the Python client can create a private directory and send setup code:

```python
import json
from pathlib import Path
import socket
import tempfile

from mcp_console import MCPConsole

with tempfile.TemporaryDirectory(prefix="console-control-") as directory:
    path = str(Path(directory) / "control.sock")
    with MCPConsole(
        args=["serve", "--control-socket", path, "-c", "languages=[python]"]
    ) as console:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as control:
            control.connect(path)
            with control.makefile("rwb") as messages:
                request = {
                    "id": "setup",
                    "op": "evaluate",
                    "language": "python",
                    "code": "selected_region = 'North'",
                    "busy": "wait",
                    "wait_ms": 60000,
                    "record": False,
                }
                messages.write(json.dumps(request).encode() + b"\n")
                messages.flush()
                response = json.loads(messages.readline())
                print(response)
        # Connect console.send to the agent while this MCPConsole remains open.
```

Keep the MCP connection open: it owns the server's lifetime.
Closing the control socket leaves the server and its sessions running.
MCP shutdown closes the control listener and its connections, retires all session workers, and removes the socket path it created.

The channel uses the configured runtime rather than the model-visible tool schema.
It can submit a language hidden from that schema when the runtime supports it.
It cannot add filesystem or network permissions.
When permissions are inherited from the MCP client, evaluation requires those permissions to have been captured through MCP; a premature control request returns `not_ready`.

## Send requests

Send one UTF-8 JSON object per newline-terminated line.
Each request requires a string `id` and an `op` from the table below.
Unknown fields, unknown operations, and incorrect field types are rejected.
A frame, including its newline, must fit within 1 MiB.
An oversized frame or EOF before its terminating newline closes the connection.

Wait for each response before sending another request on that connection.
Use a second connection to inspect status or output while an evaluation is waiting or running.
Responses contain the same `id` and either `result` or `error`:

```json
{"id":"state","op":"status"}
{"id":"state","result":{"state":"idle","idle":true,"evaluation":null}}
```

Errors have `code` and `message` fields.
Malformed requests use `id: null`.
Request IDs correlate responses; they do not deduplicate submissions.
Sending an evaluation again can execute it again.

| Operation      | Additional fields                                                                           | Result                                                                                               |
| -------------- | ------------------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------- |
| `status`       | None                                                                                        | Current state, `idle`, and the active evaluation's origin, language, and whether execution started.  |
| `output`       | None                                                                                        | Retained typed output snapshot for ordinary evaluations, independent of MCP collection.              |
| `preview`      | None                                                                                        | Bounded `content` blocks containing currently pending text and images, without consuming them.       |
| `requirements` | None                                                                                        | The accepted `requirements`, `prepared`, and Console's separate `runtime_requirements`.              |
| `manifest`     | None                                                                                        | The accepted requirements declaration alone.                                                         |
| `prepare`      | `requirements`, using the MCP requirements schema with `action: "add"` (the default)        | Prepare additions and return a structured outcome with the current accepted requirements.            |
| `recording`    | None                                                                                        | Absolute paths to the existing Markdown and current-worker QMD projections, with recording identity. |
| `evaluate`     | `language`, `code`, optional `execution`, `busy`, `wait_ms`, `record`, and `include_output` | Evaluation `content` blocks and `isError`, using Console's usual output limits.                      |
| `subscribe`    | Optional `format`: `preview` (default) or `events`                                          | Acknowledgment followed by pushed snapshots on a dedicated connection.                               |
| `stdin`        | `evaluation_id`, `input`                                                                    | Deliver managed input to the identified active evaluation.                                           |
| `interrupt`    | `evaluation_id`                                                                             | Request interruption of the identified active evaluation.                                            |
| `restart`      | None                                                                                        | Wait for session replacement; return `accepted` and the replacement `worker_id`.                     |

`language` is `r`, `python`, or `sql`; `code` is a string.
Evaluation and preview results have `resultType: "complete"`, `content`, and `isError` fields.
Ordinary language diagnostics appear in `content`; `isError` marks server or worker operation failures.
Evaluation results also carry `execution_error`, either `null` or a structured language failure with `ename`, `evalue`, and `traceback`.
Ordinary R evaluation reports uncaught interrupts as `Interrupt` and runtime errors as `RError`.
R error metadata preserves the diagnostic emitted before `options(error)` runs.
If that diagnostic is suppressed or redirected, `evalue` is `R evaluation failed (diagnostic unavailable)`; Console does not recover hidden error text from R's mutable error buffer.
Replacing global calling handlers during an evaluation can remove the temporary condition observers; an unclassified native unwind then reports `RAbort` with `R evaluation aborted`, without reusing an earlier error message.
This metadata has a separate 8 KiB allowance, including retained string slots, and includes `truncated: true` when shortened.
Ordinary diagnostics retain their existing output and raw-recording limits.
Admission failures use the response's `error` field.
Requirement mutation supports additive preparation only; replacement and reset remain MCP operations.
Use the `id` in `status.evaluation` as `evaluation_id` for input and interruption; stale IDs are rejected so a delayed request cannot affect a later cell.
Startup input is not supported through this channel.
MCP interruption and restart targeting `default` still affect the shared session.

`restart` replaces the shared session using its accepted configuration and requirements.
It waits for confirmed retirement and replacement startup before returning `accepted: true`; it does not submit code or change requirements.
The response contains the replacement `worker_id`, without evaluation output.
Use `status` to inspect the session after replacement; another request can already be active by then.
Uncollected MCP output and lifecycle notices remain available to MCP exactly once.
An active control evaluation settles with its original requester, including its private output.
Restart clears runtime objects and private viewer registrations; applications should rebuild their private setup for the new `worker_id`.
Runtime configuration and inherited MCP permissions must be ready, or the request returns `not_ready`.
Once started, restart continues if its control connection disconnects.

`prepare` uses Console's normal requirements validation, resolution policies, and preparation admission.
It does not execute a cell, use immediate execution, or restart automatically.
The worker must be ready and idle, with no outstanding immediate request or uncollected evaluation; otherwise it returns `status: "busy"` without queuing the change.
Once admitted, preparation continues if its control connection disconnects.
The result has `status: "prepared"`, `"restart_required"`, `"busy"`, or `"failed"`, the current `worker_id`, and the same `requirements`, `prepared`, and `runtime_requirements` fields as inspection.
These fields describe the actual accepted state after the operation, including partial activation if a mixed-language change fails.
They are declarations and aggregate preparation state, not an installed-package inventory or per-package versions.
Failures include a diagnostic `message`, limited to an 8 KiB UTF-8 prefix with `message_truncated: true` when shortened.
The diagnostic comes from preparation before MCP response assembly.
Existing MCP output and shared preparation/lifecycle diagnostics remain available to MCP exactly once; the control receipt does not collect them.
Unknown request fields and unsupported actions are `invalid_request` errors; unavailable configuration or inherited permissions are `not_ready` errors.

`recording` returns `directory`, `markdown`, `qmd`, and `worker_id` for existing projections.
Paths are absolute; `qmd` is null until the current worker has visible recorded source, and `worker_id` can be null before a worker starts.
The lookup does not materialize projections, run code, or consume output.
If a Markdown projection is not available, the request fails with `projection_unavailable`.

## Choose how to handle a busy worker

By default, `execution: "serial"` executes an ordinary REPL cell.
Choose one of these policies:

- `"busy": "reject"` is the default.
  If Console cannot admit the cell immediately, it returns `busy` without running the code.
  Leave out `wait_ms`.
- `"busy": "wait"` requires `wait_ms`, an integer from `0` to `4294967295` milliseconds.
  Console waits for admission until that deadline, then returns `timeout` without running the code if the slot remains unavailable.

The deadline covers startup and waiting for admission.
Once admitted, the evaluation runs to completion; `wait_ms` does not limit execution time.
A pending MCP result retains its delivery owner, so an integrator wait may also depend on the MCP client collecting that result.
Use `busy: "wait"` when submitting after an MCP response: the server can still be acknowledging delivery after the client receives the complete frame.
Requests do not reserve a position ahead of other callers.
Worker replacement, including automatic recovery after failure, invalidates a request waiting for the previous worker connection.

Disconnecting before admission cancels the pending submission.
Disconnecting after admission leaves the evaluation running and recorded.
Its response does not become output for the agent to collect.
Code can still change objects that later agent calls observe.

### Opt in to immediate execution

For short, latency-sensitive requests, an integrator can select `execution: "immediate"` to evaluate code privately in the existing session.
Console chooses reentrant R execution or a Python background thread from the request's `language`.
SQL with `execution: "immediate"` returns `execution_failed` with the message `immediate execution supports only R and Python`.
Immediate requests require `busy: "reject"` (the default) and no `wait_ms`.
They run in the existing worker and share its objects and permissions.
The integrator is responsible for coordinating access to those objects and choosing code that is safe to run concurrently.

R requests run on R's owning thread when it services events, including during `Sys.sleep()`.
An already executing ordinary R cell pauses at that event checkpoint while the immediate request evaluates under `R_ToplevelExec`, which contains native unwinds.
The immediate request then returns control to the ordinary cell.
This is cooperative execution: requests cannot preempt native code that does not service R events.
They return `busy` while an ordinary Python or SQL cell is admitted, since those cells can block R event handling.
They also return `busy` while an ordinary R cell has been admitted but is still awaiting entry into the worker.
This gives the admitted ordinary cell precedence over further immediate requests until it starts executing.
Ordinary client submissions remain accepted while immediate R runs; they wait for the interpreter before their source is dispatched and can still be cancelled during that wait.
Keep immediate requests short: R cannot run the ordinary cell until the private request returns.
Integrators must tolerate `busy` responses; immediate execution does not take precedence over ordinary cell admission.
`status.evaluation` and `status.idle` describe ordinary cell ownership, not immediate work.
R evaluates parsed expressions in the global environment and prints visible values; it leaves `.Last.value`, history, and ordinary REPL task callbacks alone.
Deferred warnings are returned after each private expression, preserving the foreground warning queue and `last.warning`.
Private errors are caught locally and do not run the ordinary `options(error)` handler.
Each immediate R evaluation restores the foreground error buffer returned by `geterrmessage()`.
An uncaught interrupt during a reentrant request ends that request and remains pending for the foreground evaluation after private cleanup.
Integrator code that catches an interrupt condition is responsible for its handling.
Python requests run on a background thread and require the GIL; native code holding the GIL can delay them.
Background Python code must not call into R.
Only one immediate request may be outstanding per worker.
Immediate requests also return `busy` while the worker is restarting, before code is admitted.
An immediate request already admitted to a worker still returns `execution_failed` if restart retires that worker before it completes; it is never replayed on the replacement.
Prepare dependencies and initialize the requested interpreter through an ordinary cell first.
Concurrent requests cannot prepare packages or request managed stdin.

After initializing Python, an integrator can send:

```json
{
  "id": "ui",
  "op": "evaluate",
  "language": "python",
  "execution": "immediate",
  "code": "print(2 + 2)"
}
```

Managed output belongs to the concurrent request's response.
This includes cleanup performed when an immediate Python thread releases its interpreter state.
Native stdout/stderr and child-process output retain the ordinary foreground attribution because process streams do not identify a writing thread.
Graphics state is shared; concurrent requests do not automatically finalize plots or close the foreground cell's devices.
Images explicitly emitted by an immediate request are retained under its call and worker session, with their own recording budget and no ordinary cell interval.
As with serial evaluations, `record: false` omits them from the transcript projection while retaining their raw files and journal events.
The pending-output subscription observes the ordinary output interval; concurrent request output is returned in that request's response.

## Inspect state and pending output

`status` can report `starting`, `preparing`, `evaluating`, `input_requested`, `completing`, `awaiting_result`, `restarting`, `shutting_down`, `failed`, or `idle`.
It reports `busy` if the state cannot be observed immediately.
An active evaluation identifies its `origin` as `mcp` or `integrator`.
Treat the response as a current observation; it does not reserve the worker for a subsequent request.

`preview` returns the active evaluation's pending output, or idle output between evaluations, including a partially written terminal line.
Repeated previews do not consume output, acknowledge delivery, or alter recording omission counts.
Output already taken by a response writer may no longer be available in the preview.
If an operation's state cannot be observed immediately, it returns `busy`; retry the inspection.

### Subscribe to pending output

Send `{"id":"ghost","op":"subscribe"}` on a dedicated control connection.
Console replies with `{"id":"ghost","result":{"subscribed":true}}`, then sends an initial snapshot and updates as pending output changes:

```json
{
  "id": "ghost",
  "event": "preview",
  "result": {
    "resultType": "complete",
    "content": [{ "type": "text", "text": "working…" }],
    "isError": false
  }
}
```

Replace the displayed ghost output with each snapshot; do not append snapshots.
The stream includes partial terminal lines and images, uses the usual preview limits, and never consumes or acknowledges an MCP result.
An empty snapshot clears output that has been collected.
Changes may be coalesced for a slow reader, so this is a view of pending output rather than an event log.
Output collected by MCP before a subscriber observes it need not appear in the stream.
Each subscriber retains only its latest pending change and cannot block worker output.
Close the connection to unsubscribe; use other connections for requests.

### Inspect typed output

Use `{"id":"output","op":"output"}` to read a bounded history of ordinary evaluation output.
Unlike `preview`, this history survives MCP polling and completed-result collection.
It is captured before terminal formatting and MCP response assembly; clients do not need to parse formatted MCP text to identify streams.
The existing MCP and `preview` projections, delivery acknowledgments, and recording limits are unchanged.
History is retained only when the control listener is enabled.

A result has this shape:

```json
{
  "worker_id": 1,
  "first_sequence": 0,
  "next_sequence": 4,
  "omitted_events": 0,
  "omitted_bytes": 0,
  "evaluations": [
    {
      "id": 1,
      "origin": "mcp",
      "language": "python",
      "source": "print('hello', end='')",
      "source_truncated": false,
      "execution": "serial",
      "request_id": null,
      "call_id": 1,
      "state": "complete",
      "started": true,
      "execution_error": null
    }
  ],
  "events": [
    { "sequence": 0, "evaluation_id": 1, "type": "evaluation_admitted" },
    { "sequence": 1, "evaluation_id": 1, "type": "evaluation_started" },
    {
      "sequence": 2,
      "evaluation_id": 1,
      "type": "output",
      "text": "hello",
      "omitted_bytes": 0
    },
    { "sequence": 3, "evaluation_id": 1, "type": "evaluation_completed" }
  ]
}
```

Evaluation IDs are the existing IDs used by `status`, `stdin`, and `interrupt`.
Each admitted ordinary submission has its own ID; `call_id` is its recording call association when recording is available.
The existing MCP validation still allows only one code language per call.
Integrator metadata also carries the control `request_id`, so a frontend can replace its provisional submission with the admitted evaluation.
`state: "complete"` means the evaluation settled, including language failures or cancellation before worker entry; inspect `execution_error` for a bounded error summary, and its event for the retained traceback.
It does not acknowledge MCP delivery.
`started` distinguishes ordinary admission from confirmed worker entry; admitted code may still be waiting for startup or the interpreter.
Immediate metadata uses `started: true` because its private snapshot is returned after execution.
Worker replacement increments `worker_id` and clears the retained history; evaluation IDs and event sequences continue increasing within the server process.

Each event has `sequence`, nullable `evaluation_id`, and one of these types:

- `stdout` and `stderr` contain `text` from the raw process streams.
- `output` and `diagnostic` contain managed worker console text on the corresponding channel.
- `notice` contains server-generated information, such as a managed input request or operation failure.
- `image` contains the existing captured `data` and `mimeType`, preserving its position among observed publications.
- `execution_error` contains the structured `error`, including its bounded traceback and truncation marker.
- `evaluation_admitted`, `evaluation_started`, and `evaluation_completed` mark the retained evaluation interval.
- `omission` identifies an image too large for this history, with its encoded `bytes` count and `kind: "image"`.

Text events preserve partial lines, carriage returns, and terminal controls rather than applying the MCP terminal projection.
Raw streams use incremental UTF-8 decoding, replacing invalid bytes; this is a typed text view, not a byte-for-byte recording.
Sequence order is observation order at Console's output owner.
Independent worker and process streams do not provide a global chronology, and write boundaries may split or combine text.
Output observed between ordinary admission and completion carries that evaluation ID, including preparation output before worker entry.
Idle output has `evaluation_id: null`; native output from immediate threads retains the ordinary foreground attribution because process streams do not identify their writing thread.

Subscribe with `{"id":"output","op":"subscribe","format":"events"}` for the same snapshot under `{"id":"output","event":"output","result":...}`.
Both this subscription and `op: "output"` return replacement snapshots, not incremental event batches.
Use evaluation IDs and event sequences to reconcile displayed output; replacing a pending view after completion must not append the same output again.
Slow readers can miss intermediate snapshots, but completed output remains available within the retention bounds.
The subscription coalesces notifications and never blocks output producers.

The history retains at most 512 events, 64 KiB of text/error event payloads, 8 MiB of encoded images, and 32 evaluation descriptors.
Each descriptor retains at most 8 KiB of source, 512 bytes of request ID, and the separately bounded execution-error summary.
`source_truncated` and `request_id_truncated` mark shortened metadata.
Text events report their own `omitted_bytes`; snapshot `omitted_events` and `omitted_bytes` count history eviction and oversized payload omissions.
Evicting an evaluation descriptor also evicts its associated events, so every non-null event identity has retained metadata.
`first_sequence` and `next_sequence` delimit the retained interval; omission counters reset on worker replacement.
These limits are separate from MCP presentation and recording allowances.
A reader should make omissions visible and must not interpret a retained tail as complete output.

Set `include_output: true` on `evaluate` to add `evaluation_id` and `output` to its response.
For ordinary evaluations, `output` filters the current retained history to that evaluation; sequence bounds and omission counters still describe the whole shared history.
Immediate evaluations use their own bounded tape and return its snapshot only to their caller.
Their managed output never enters the shared history or subscription, regardless of `record`.
Ordinary `record: false` evaluations also omit their observed interval and source from this history, independently of whether recording is enabled.
As with other output attribution, background process output first observed after the completion boundary can enter the idle stream.

`requirements` and `manifest` read the last accepted declaration without preparing packages or running code.
They return `not_ready` before runtime configuration is available.
The declaration contains `r`, `python`, `duckdb`, `python_version`, and `exclude_newer` fields.
It is not a lockfile or an inventory of every installed package.

## Select recording projections

Every control request and result enters Console's journal capture; ordinary `evaluate` requests also record source, execution boundaries, output, and retained images.
Concurrent evaluations record their request and result without entering the ordinary cell's source and raw-output interval.
Subscription snapshots are transient views of already captured output and are not individually journaled.
An `output` request records a receipt with worker identity, sequence bounds, and omission counts; its snapshot source, text, and image payloads are not duplicated in the journal.
Likewise, `include_output` adds a typed view only to the evaluation response on the wire; the evaluation retains its existing canonical result and artifact recording.
The usual [recording retention and failure rules](RECORDING_REFERENCE.md) apply.

`record` defaults to `true`.
Set it to `false` to omit that evaluation's call, result, and associated output from `transcript.md` and the session's QMD source document.
The journal and raw capture remain available.
Inspection requests are journaled without adding entries to those human-readable documents.

Output attribution follows observed cell and post-cell intervals.
Only output captured through the integrator evaluation's completion boundary stays with its private response.
Background stdout or stderr observed after completion enters the ordinary idle output stream and may appear in a later MCP response.
Keep instrumentation synchronous or give its background work separately owned output streams when it must stay out of agent responses.
Its projection choice follows the observed post-cell interval until the next cell begins.
Background work that keeps writing after a later cell begins can be attributed to that later interval.
The projection choice does not isolate objects or conceal data that later code explicitly prints.

## Try the Shiny fixture

The [Shiny integration fixture](../tests/fixtures/control_shiny/README.md) displays agent chat beside the shared Console transcript and a live Variables pane.
It demonstrates direct code submission, streamed pending output, and Immediate execution for R and Python, including private inspection of R's global environment and Python's `__main__` symbols.
