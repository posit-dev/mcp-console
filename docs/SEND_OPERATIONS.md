# Calls, input, and control

For client authors handling calls and session lifetime.
To connect an existing MCP client, start with [Get started](getting-started.qmd).

`send` operates on one session and accepts at most one complete `r`, `python`, or `sql` cell.
Submit cells sequentially and collect unfinished results before sending another cell.
See the [API reference](API.md) for argument types and limits.

## Cells and polling

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

## Server readiness

MCP initialization, tool discovery, and ping do not wait for runtime preparation.
A valid early cell can be accepted while the shared startup continues.
Its observation deadline includes startup; timeout does not cancel it or create another worker.

An empty poll or requirements inspection can return `[worker starting]`.
Standalone requirements wait for shared startup and preparation, including initial discovery, without a `timeout_ms` deadline.
Once transport is ready, `[idle]` does not necessarily mean every interpreter startup hook has finished.

After initial setup failure, fix the reported problem and explicitly restart.
Ordinary calls retain the failure rather than retrying setup.
Rejected code and input are not replayed.

## Standard input and managed reads

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

## Interruption

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

## Explicit restart

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

## Operations

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
See [Requirements actions](REQUIREMENTS_API.md#inspecting-and-replacing-requirements).

## Wait timing and cancellation

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

## Python client cancellation

If `KeyboardInterrupt` interrupts a synchronous response wait, the client cancels that request and re-raises the exception.
It waits for the SDK's request cleanup before re-raising so subsequent calls cannot overtake cancellation.
Concurrent sends are rejected while cancellation cleanup is in progress; ordinary concurrent control calls remain available when a cell is running.
The interrupted response wait retains admission while publishing cancellation, so a competing send cannot pass between those steps.
Cleanup has a five-second limit; another `KeyboardInterrupt` also ends the wait.
Cancellation submission does not wait for the SDK event loop, so this limit also applies while the SDK is busy validating a response.
Interrupting before a request starts prevents it from being sent and leaves the connection usable.
If cleanup does not finish, the client aborts that request task and rejects further calls until the connection has been closed and reconnected.
Closing waits at most five seconds for the aborted request to stop before closing the SDK transport and portal.
If that request still cannot stop, closing raises an error and retains the invalid connection until it stops.
An admitted cell keeps running.
Catch the exception inside the connection's context to poll for its output or call `console.send(control="interrupt")` to interrupt execution explicitly.
Leaving the context closes the session.
If SDK teardown raises after consuming its cleanup callbacks, the client records a disconnected state; reconnecting starts a fresh session.

A journaled result records response assembly, not confirmed client receipt.
Cancellation can race with delivery.
See the generated [Python client API](https://t-kalinowski.github.io/mcp-console/python/reference/MCPConsole.html) for signatures and connection lifetime.
