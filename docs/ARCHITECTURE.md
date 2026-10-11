# Architecture

Console separates session management from live execution.
The server owns state that survives worker replacement; the worker owns interpreter and database state.
The native runner owns OS enforcement and process-tree cleanup.

Read [Public interfaces](API.md) for user-visible behavior.
This page describes the ownership and invariants contributors must preserve, not every implementation step.
The [glossary](GLOSSARY.md) defines recurring terms.

## Process layout

```text
MCP client
  │ MCP over stdio
  ▼
server ── native runner → resolve → package tools     preparation
  │
  └────── sandbox frontend → native runner
                                 └─ relay → worker    execution
                                             ├─ R
                                             ├─ Python
                                             └─ SQL providers
```

On Windows, dependency preparation runs with host permissions rather than through a resolver sandbox.

These processes run on the same host.
Remote placement belongs to the client or deployment tooling.
`--no-sandbox` omits native enforcement and descendant cleanup, not the server/worker separation.

The frontend verifies the pinned companion before launch.
On Unix it becomes the runner through `exec`; on Windows it waits for the runner.
Console must not introduce another native supervisor that competes for the runner's resources.

## Ownership

| Owner             | Responsibility                                                                                                 |
| ----------------- | -------------------------------------------------------------------------------------------------------------- |
| Server            | MCP admission, session identity, worker generations, retained declarations, response delivery, and recordings. |
| Preparation owner | Discovery, materializer/inspection processes, and confirmed cleanup before publishing their results.           |
| Native runner     | Native permissions, owned temporary storage, and its documented descendant-retirement contract.                |
| Relay             | Worker descriptors, stream translation, interruption, and direct-worker shutdown/reaping.                      |
| Worker            | Interpreter state, evaluation, input, semantic output, SQL connections, and live environment activation.       |

Each operation should have one canonical owner.
In particular, relay exit is not a native cleanup receipt, materialization is not manifest acceptance, and assembled output is not confirmed client receipt.

The [server-relay](RELAY_PROTOCOL.md) and [relay-worker](WORKER_PROTOCOL.md) transports are private JSONL interfaces.
Interactive stdin and direct stdout/stderr are separate streams.
Each producer preserves order, but their observation order does not reconstruct a global chronology.

## Startup and runtime ownership

The server captures configuration once and constructs tool presentation from it.
One connection-owned background task discovers runtimes, prepares defaults, and prelaunches the built-in worker.
MCP initialization, tool discovery, and pings do not wait.
Custom workers remain lazy.

Transport readiness connects command, input, output, and resolver services before built-in interpreter bootstrap.
It does not mean either interpreter is initialized.
Early cells reserve the ordinary evaluation slot; there is no queue.
Cancelling a call's wait does not cancel shared startup or replay an admitted cell.

A failed initial attempt is retried only by explicit restart and only after its preparation and execution owners confirm cleanup.
Every stage's cleanup evidence matters; a later successful inspection cannot erase an earlier unretired process.

### Worker

One coordinator serializes interpreter initialization and cell execution on one thread.
R affinity, CPython thread state/GIL ownership, and reentrant cross-language callbacks constrain this design.
Signal handlers mark state and wake waits; they do not enter interpreters.
Do not hold locks or mutable state borrows across reentrant interpreter calls.

R and Python are peer runtimes.
Console owns CPython bootstrap and services; reticulate supplies compatibility and conversion.
Bridge attachment must use the running interpreter, not select or initialize another one.
Failed or interrupted native R initialization is not retried in place.

SQL connections stay in their owning runtime.
Actual R capability chooses the default managed provider independently of public language visibility or initialization order.
Selecting or attaching another runtime must not replace a user-selected connection.

Configured SQL startup code runs once per worker after its helpers are installed.
A failed startup cannot be silently replayed or replaced with a fallback connection.
Native R profiles run earlier, inside the worker boundary, before Console's managed graphics device.

The remaining process-environment constraint during late R initialization is recorded in [TODO](TODO.md#runtime-and-resource-limits).
Splitting interpreter threads would not solve process-wide environment mutation.

## Generations and operations

An evaluation, input write, control target, resolver callback, candidate, and commit belong to the generation that admitted them.
Old work must never reach or commit into a replacement.

A cell is admitted once.
Its retained server admission owner exists before a worker does and records pending, dispatched, or withheld code separately from the worker's bootstrap receipt.
Worker terminal receipts or terminal generation failure settle execution; output observation grace and response delivery do not.
Completed but unclaimed responses retain the ordinary cell slot until collected.
Control-and-cell calls retain that admission across their ordered steps.
Restart input/code targets only the replacement; an interrupt's follow-up cannot silently migrate after a failure.
[Send operations](SEND_OPERATIONS.md) owns the public ordering and partial-effect rules.

Restart prepares a changed environment before retiring the current worker.
Failure before acceptance preserves the old declaration and worker.
Failure after retirement cannot restore live state.
An established worker failure can trigger one replacement attempt, but never replay of the failed cell or input.

Interrupt selection retains one resolver operation, startup admission, or worker connection and generation through acknowledgment and observation.
A selected resolver that already completed does not authorize selecting another target.
Automatic replacement can reuse a generation, so bundled follow-up work also checks the selected connection and prior cell outcome.

## Preparation and activation

The hidden `resolve` process materializes environments under the separate [resolver policy](RESOLVER.md).
The preparation owner publishes a result only after its process and I/O cleanup contract completes.
The server owns accepted declarations, not the materializer.

For live changes, a resolved environment is provisional.
The worker validates and activates it, then sends an acceptance receipt.
Only a matching candidate from the current generation can commit.
Accepted activation survives a later import or cell error; unaccepted stale candidates do not.

Explicit changes and runtime-originated callbacks share environment-change ownership.
This prevents a result resolved from an old declaration from overwriting a newer one.
Live activations can have separate acceptance points; arbitrary package installation, cache, or site-hook effects are not transactional.

The precise message objects live in the [worker protocol](WORKER_PROTOCOL.md); the user's supported changes live in [Requirements](REQUIREMENTS.md).
Do not duplicate those schemas here.

## Retirement and cancellation

Each launch has one retained retirement operation, shared by restart, EOF, startup failure, and recovery.
Later requests observe that operation; they do not renew budgets, resend controls to old PIDs, or join the same tasks independently.

Retirement closes admission, requests orderly shutdown, bounds command/output handling, settles owned I/O, and obtains native cleanup evidence.
Blocked writes, inherited descriptors, and partial frames must not keep shutdown waiting indefinitely.
Aborting a partial command ends that transport; no control can be inserted into its bytes.

Physical cleanup, protocol settlement, and output delivery are separate results.
Available output is drained within bounds even when cleanup fails.
Unconfirmed native retirement blocks replacement.
A forced launcher kill or pipe EOF cannot be promoted into proof of successful cleanup.

Connection closure cancels startup/preparation, retires execution resources, settles accepted responses, and bounds blocked MCP delivery.
Native runner loss retains the [documented lifetime limits](SANDBOX.md#supported-hosts-and-lifetime-limits); Console has no independent recovery daemon.

## Output and delivery

The server owns an ordered output tape, finite response cuts, text/image budgets, and generated notices.
Relays forward events; they do not decide MCP response boundaries or candidate acceptance.

One recoverable response remains owned until local delivery or cancellation settles it.
Failed delivery restores the entire claimed region, including output preceding a combined control-and-cell call.
This does not guarantee exactly-once observation: cancellation can race with bytes already visible to a client.

The journal records calls and assembled results.
Markdown and QMD are projections, not interpreter checkpoints; a journaled result does not prove client receipt.
See [Recordings](RECORDING.md).

## Where to look in source

| Concern                            | Entry point                                                                                                                     |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| MCP arguments and presentation     | [`src/server/arguments.rs`](../src/server/arguments.rs), [`src/server/presentation.rs`](../src/server/presentation.rs)          |
| Connection startup                 | [`src/server/startup.rs`](../src/server/startup.rs)                                                                             |
| Session and generation ownership   | [`src/worker_client.rs`](../src/worker_client.rs)                                                                               |
| Relay and direct worker            | [`src/worker_relay.rs`](../src/worker_relay.rs)                                                                                 |
| Interpreter coordination           | [`src/worker/coordinator.rs`](../src/worker/coordinator.rs), [`src/python.rs`](../src/python.rs), [`src/sql.rs`](../src/sql.rs) |
| Preparation                        | [`src/resolver/preparation.rs`](../src/resolver/preparation.rs)                                                                 |
| Launch and enforcement integration | [`src/worker_client/process.rs`](../src/worker_client/process.rs), [`src/sandbox.rs`](../src/sandbox.rs)                        |
| Recording                          | [`src/transcript.rs`](../src/transcript.rs)                                                                                     |

Follow these owners into their modules instead of maintaining an exhaustive file inventory.
[Boundary tests](../tests/boundaries/README.md) provide observable evidence; implementation comments hold local algorithms and exceptional cases.
