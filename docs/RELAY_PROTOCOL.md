# Server-relay protocol

This private JSONL interface connects one server generation to its relay and evolves in lockstep.
[relay_protocol.rs](../src/relay_protocol.rs) owns the schema; [worker_relay.rs](../src/worker_relay.rs) and [worker_client/process.rs](../src/worker_client/process.rs) implement the endpoints.

## Process boundary

The relay owns the direct worker's descriptors, standard streams, sideband, interruption, and reaping.
The native runner owns enforcement and descendant cleanup.
Resolution, candidate acceptance, response budgets, and session policy stay with the server.
The runner inherits streams without proxying their contents; direct mode omits it.
Windows uses the same message shapes over native pipes, process handles, and interrupt events.

## Framing and raw bytes

Each direction has one writer emitting complete UTF-8 JSONL frames.
Unknown kinds/fields, wrong types, malformed JSON, and partial-frame EOF fail an open command transport.
The first explicit shutdown ends command parsing; trailing command bytes are ignored.
Worker output remains strictly framed through retirement.

Raw stream chunks use text events when the complete chunk is valid UTF-8 and padded standard base64 otherwise.
Incremental decoding belongs to the server.
Stderr can carry infrastructure diagnostics; a framed fatal event is authoritative when present.

## Server commands and relay events

Semantic [worker messages](WORKER_PROTOCOL.md#message-schemas) cross this boundary without an extra wrapper.
The relay adds stdin forwarding, interrupt requests and receipts, timed shutdown, worker acquisition/exit, raw stream data/closure, and fatal diagnostics.
Exact fields and payload-free variants live in the source enums.

Interrupt receipts match the request ID and acknowledge signal delivery, not execution completion.
Resolver-targeted interrupts never cross this boundary.
Worker shutdown's payload-free message becomes a relay request carrying the remaining worker-exit allowance.

## Event production and ordering

Sideband, stdout, stderr, and lifecycle producers publish complete frames through one ordered writer.
Each producer preserves its own order; cross-stream order is observation order.
Output written before semantic completion can arrive afterward.

After direct-child acquisition, `worker_started` precedes output readers.
Launch failure before acquisition emits no such event.
Readiness is a separate worker receipt, and the final exit/signal event reports the actual child outcome.
These observations establish recording lifetimes independently of generation identity.
The relay keeps reading after operation results without waiting for MCP polling.

## Output backpressure

Queues are bounded and readers wait for capacity.
One oversized frame can be admitted alone; readers can each retain a waiting frame, so queue capacity is not a total memory ceiling.
Supervisor events have a separate allowance without overtaking earlier output.

Retirement bounds downstream writes and queue admission.
Pipes, FIFOs, and sockets support cancellable nonblocking output; regular files retain filesystem blocking behavior.
Capacities and timing constants are implementation parameters.

## Interruption and shutdown

The server has one generation-owned command writer.
Forced retirement closes admission and aborts pending writes or queue waits.
Aborting a partial frame ends the transport; shutdown or interrupt bytes cannot be inserted into that frame.

The first shutdown/EOF fixes the worker deadline.
The relay closes stdin and requests shutdown concurrently, then terminates and reaps the direct child if needed.
Fatal transport failures retire immediately.
Later observations cannot renew the deadline.
Public interrupt grace and follow-up cells belong to [send ordering](API.md#operations).

## Retirement and failure

Retirement cancels local I/O before joining tasks and drains available output within bounds.
It cannot wait indefinitely for descendants holding streams or a blocked consumer.
Forced retirement can abandon an incomplete trailing sideband frame; malformed ordinary traffic still fails the boundary.

No bytes follow their stream closure, and no event follows the final child exit/signal event.
Clean output EOF requires expected stream closures and the worker outcome.
Shutdown acceptance, controller EOF, sideband EOF, child exit, and cancellation completion remain distinct evidence.
Replacement also needs the owning launch's cleanup and I/O results; direct-worker exit is not native cleanup.
See [retirement ownership](ARCHITECTURE.md#retirement-and-cancellation) and the [boundary tests](../tests/boundaries/README.md).
