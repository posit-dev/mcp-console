# Server-relay protocol

This private JSONL interface connects one server generation to its relay.
It evolves in lockstep without independent negotiation.
[`src/relay_protocol.rs`](../src/relay_protocol.rs) owns the exact schema; [`src/worker_relay.rs`](../src/worker_relay.rs) and [`src/worker_client/process.rs`](../src/worker_client/process.rs) implement the endpoints.

## Process boundary

The relay owns the direct worker's descriptors, standard streams, sideband, interruption, and reaping.
The native runner owns enforcement and descendant cleanup.
Dependency resolution, candidate commits, MCP response budgets, and session policy belong to the server and preparation owners.

A native runner inherits the streams without proxying their contents.
Direct mode omits it.
Windows uses the same message shapes with native pipes, process handles, and interrupt events.

## Framing and raw bytes

Each direction is UTF-8 JSONL, flushed per complete frame, with one writer.
Frames must never interleave.
Unknown kinds/fields, wrong types, malformed JSON, and partial-frame EOF fail an open command transport.
The first explicit shutdown ends command parsing; trailing bytes are ignored.
Worker output remains strictly framed through retirement.

Raw stdout/stderr chunks use text events when the entire chunk is valid UTF-8, otherwise padded standard base64.
UTF-8 decoding across raw chunks belongs to the server, not the relay.
Stderr carries infrastructure diagnostics; a framed fatal event is authoritative when available.

## Server commands and relay events

Semantic [worker messages](WORKER_PROTOCOL.md#message-schemas) cross this boundary flat, without a nested worker-message wrapper.
The outer transport adds these commands:

| Command     | Fields                                                                                  |
| ----------- | --------------------------------------------------------------------------------------- |
| `stdin`     | `data`: exact input string.                                                             |
| `interrupt` | `request_id`: integer identifying the signal attempt.                                   |
| `shutdown`  | `grace_millis`: remaining worker-exit allowance; replaces payload-free worker shutdown. |

Additional relay events are:

| Events                                                     | Payload                                            |
| ---------------------------------------------------------- | -------------------------------------------------- |
| `worker_started`                                           | Direct worker `pid`, after successful acquisition. |
| `stdout`, `stderr`                                         | UTF-8 `data`.                                      |
| `stdout_bytes`, `stderr_bytes`                             | Base64 `data`.                                     |
| `stdout_closed`, `stderr_closed`, `worker_sideband_closed` | None.                                              |
| `interrupt_result`                                         | Matching `request_id`, optional `error`.           |
| `shutdown_started`                                         | None; acceptance of the registered shutdown.       |
| `worker_exited`, `worker_signaled`                         | Exit `code` or termination `signal`.               |
| `fatal`                                                    | Diagnostic `message`.                              |

Payload-free frames contain only `kind`.
A successful interrupt receipt means delivery was accepted, not that execution stopped.
Resolver-targeted interrupts do not cross this boundary.

## Event production and ordering

Sideband, stdout, stderr, and lifecycle producers publish complete frames through one ordered writer.
Each producer preserves its own order; no cross-stream chronology is inferred.
Output written before a semantic completion can be observed afterward.

The first lifecycle event after successful child acquisition is `worker_started`, before output readers start.
A launch failure before acquisition emits no such event.
Readiness remains a separate worker event, and the final exit/signal frame describes the actual child's outcome.
These observations establish recording sessions independently of generation identity.

The relay continues reading after operation results without an acknowledgment.
It does not wait for MCP polls, divide output into responses, or decide environment acceptance.

## Output backpressure

Queues are bounded and readers wait for capacity.
Oversized single frames can be admitted alone; this is not a total-memory ceiling because readers may each hold a frame awaiting admission.
Supervisor events have a separate allowance but do not overtake earlier output.

Bound both downstream writes and queue admission during retirement.
Pipe/FIFO/socket output supports cancellable nonblocking handling; regular-file output retains filesystem blocking behavior.
Exact queue capacities and timing constants are implementation parameters, not public API promises.

## Interruption and shutdown

The server serializes bootstrap and commands with one generation-owned writer.
Forced retirement closes admission and aborts pending writes or queue waits.
An aborted partial frame ends that transport; shutdown or interruption must not be inserted into it.

The first shutdown/EOF retirement fixes the worker deadline.
Later observations cannot renew it.
The relay closes stdin and requests worker shutdown concurrently, then terminates and reaps the direct child if it does not exit.
Fatal transport failures retire immediately.
The server's interrupt grace and optional follow-up cell are defined in [send ordering](SEND_OPERATIONS.md), not by a new wire message.

Shutdown acceptance, controller EOF, worker-sideband EOF, child exit, and cancellation completion are distinct observations.
Do not use one as evidence for another.

## Retirement and failure

Retirement cancels local I/O before joining tasks and preserves a bounded drain of available output.
It cannot wait forever for descendants holding standard streams or for a blocked consumer.
Incomplete trailing sideband frames may be abandoned during forced retirement; malformed ordinary traffic still fails the boundary.

No raw bytes follow their stream closure, and no event follows the final worker exit/signal event.
Clean output EOF requires expected stream closures and the direct-worker outcome.
Available output and fatal diagnostics remain observable before retirement completes.

Direct-worker exit is not native descendant-cleanup evidence.
Replacement additionally requires the owning launch's cleanup and I/O results.
See [Architecture](ARCHITECTURE.md#retirement-and-cancellation) and the executable [boundary tests](../tests/boundaries/README.md).
