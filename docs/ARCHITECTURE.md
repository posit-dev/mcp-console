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

Windows uses the same preparation boundary through the pinned MXC companion.
Per-operation Jobs confirm resolver descendant retirement inside that boundary; native policy and managed-proxy ownership stay with the companion.

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

### Integrator control

The hidden, internal Unix listener in [`server/control.rs`](../src/server/control.rs) shares the existing server and worker owners and has no compatibility guarantee.
MCP input closure owns shutdown; control connections cannot keep the runtime alive.
Ordinary integrator evaluations acquire the same admission and preparation boundaries as MCP evaluations, retaining the generation they observed while waiting.
Their terminal collector owns response delivery independently of the socket lifetime and prevents MCP polling or recovery from claiming their output.
Previously pending MCP output stays with MCP.
Inspection copies bounded presentation state without draining output, sealing terminal frames, accounting omissions, or acknowledging delivery.
Subscriptions observe changes to that bounded pending view through a coalescing watch; they never queue unbounded output or block producers.
Delivery cancellation also invalidates the view when a claimed response returns to its evaluation.
Input and interruption pin the observed evaluation identity so a delayed control cannot affect its successor.
The socket does not select a new sandbox authority.

Control requests with `execution: "immediate"` use a separate connection-owned result slot and bounded output tape.
An immediate R request returns busy while an ordinary non-R cell is admitted.
It also returns busy until an admitted ordinary R cell has entered the worker.
Immediate admission holds the evaluation publication lock through command enqueue; ordinary admission checks the pending R request under that same lock.
New ordinary cells cannot be admitted until immediate R completes, so a targeted interrupt cannot be consumed by private R work ahead of its target.
Serial integrator waiters wake when the priority slot clears; ordinary MCP submissions fail without running their source.
The worker ingress thread dispatches Python requests to a background thread and queues R requests for the owning thread's event hook.
R requests share one pending coordinator wakeup; only consuming that wakeup clears it, so event-hook execution cannot fill the command pipe with unread notifications.
Neither path claims the ordinary evaluation or its output.
Restart and worker failure settle the priority slot through the same connection's retirement events.
Managed output follows the current thread's request identity; native streams retain foreground attribution.
Priority execution holds preparation admission until completion and cannot initiate dependency preparation or managed input.
Its request and result are journaled without creating a nested numbered cell interval.

## Startup and runtime ownership

The server captures configuration once and constructs tool presentation from it.
The R adapter sends optional configuration source paths and ordered overrides through a private JSON file using `--config-overrides-file`.
Native capture owns file parsing, layering, full materialization, and pruning; the adapter converts those results through ordinary S7 constructors.
Optional source paths require automatic discovery to be disabled and retain the same missing-file, regular-file, and deduplication rules as native discovery.
Overrides apply after the selected files and before ordinary `-c` overrides; explicit permission nodes retain replacement semantics.
The adapter removes the file after synchronous materialization or the server's initialization handshake, when native capture has finished reading it.
One connection-owned background task discovers runtimes, prepares defaults, and prelaunches the built-in worker.
MCP initialization, tool discovery, and pings do not wait.
Workers supplied through internal testing hooks are launched on demand.

With the `inherit_codex` sandbox profile, startup waits for the first `send` to capture client authority before creating any preparation or execution process.
Capture happens in transport request order before asynchronous handler dispatch; a cancelled first request cannot let a later request select the connection's permissions.
The server translates supported permissions or selects the configured fallback, defaulting to `read_only`, then composes local permissions and selects caches.
Absent metadata is silent; unparseable metadata warns on server stderr.
An untranslatable permission entry rejects the entire client profile rather than discarding a possible denial.
Only the selected base contributes permissions; the workspace fallback cannot widen a selected client policy.
The captured policy survives startup retries and worker replacement; changed recognized permissions require a new connection.
Connection closure before the first call settles pending startup without creating a runtime.
The metadata adapter and its supported limits live in [`src/server/sandbox.rs`](../src/server/sandbox.rs) and [sandbox permissions](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex).

Transport readiness connects command, input, output, and resolver services before built-in interpreter bootstrap.
It does not mean either interpreter is initialized.
Unchanged requirements do not wait for bootstrap.
When R is available, changed requirements wait for built-in bootstrap before reserving environment changes, including replacement of an unused worker.
This prevents retirement from interrupting a native R startup attempt that began after replacement was selected.
Python-only workers can replace an unused blocked bootstrap because they cannot enter native R startup.
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

Each SQL provider retains one native result and its bounded preview/lookahead rows.
The public last-result function consumes those rows before fetching more in its owning runtime, with an optional row limit.
It returns fetched data and automatically releases native resources on exhaustion, replacement, or retrieval failure.
Cleanup errors are reported after invalidation and do not cancel replacement SQL or connection selection.
Successive calls advance the result; repeatedly returning a prefix would require replay or unbounded retention.
Native DBI/DB-API queries remain available when callers want to own their result lifecycle directly.
R DBI connections use `dbSendQuery()` and bounded `dbFetch(n)` calls and retain the driver's data frames unchanged, apart from the managed DuckDB workaround below.
Python returns driver rows with ordered column names and closes only result objects distinct from the selected connection.
When a DB-API connection is itself the cursor, generic cleanup releases Console's references without closing the connection.
For Python DuckDB, an empty `execute("")` also supersedes the native result on both 1.x and 2.x: the driver releases unfinished reads and completes pending writes without returning their rows or committing the transaction.
This keeps DuckDB 2.x connection operations usable after Console discards a result; `abort()` would instead invalidate an open transaction whose unfinished statement writes.
Each provider initially fetches at most 21 preview/lookahead rows and requests only the remaining row allowance on later bounded fetches.
Console does not eagerly fetch, copy, count, or spool the complete result; engines and drivers still own their internal buffering.
An empty fetch or `fetchall()` establishes DB-API exhaustion; a short nonempty batch does not.
The router and selection callbacks invalidate results across language boundaries without initializing another runtime.
Observable consumption, conversion, and lifetime rules belong in [Language behavior](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result).

Configured SQL startup code runs once per worker after its helpers are installed.
A failed startup cannot be silently replayed or replaced with a fallback connection.
Native R profiles run earlier, inside the worker boundary, before Console's managed graphics device.

The remaining process-environment constraint during late R initialization is recorded in [TODO](TODO.md#runtime-and-resource-limits).
Splitting interpreter threads would not solve process-wide environment mutation.

#### Managed DuckDB streaming workaround

DuckDB R 1.5.6 materializes ordinary DBI data-frame results before `dbFetch(n)` slices them.
Managed R DuckDB therefore uses `dbSendQueryArrow()` and bounded `dbFetchArrowChunk()` calls in `fetch_duckdb()`.
`duckdb_result_data_frame()` isolates the temporary conversion adapter: it passes only fetched batches through `query_arrow()` in a separate scratch catalog, then calls `dbGetQuery()` there for DuckDB's R column conversions.
The conversion query limits output to the rows consumed by the call, avoiding manual Arrow array slicing and passing oversized row allowances to SQL `LIMIT`.
The scratch connection explicitly enables `preserve_insertion_order`; the offset only skips consumed rows in saved batches.
The adapter never replays the submitted SQL, converts the unread result, or depends on the R `arrow` package.
Selected DBI connections continue through `fetch_dbi()` and keep their driver's types and connection options.

When the supported DuckDB R versions include [streaming data-frame results](https://github.com/duckdb/duckdb-r/pull/2587), enable that mode and use ordinary DBI fetching for managed retrieval too.
Remove `duckdb_result_data_frame()` and the Arrow-specific retained-result buffer and conversion branches.
Keep the public row limits, successive consumption, exact preview formatting, and automatic cleanup unchanged.
The existing public SQL result cases cover those contracts and driver conversion parity.
Track this replacement in [TODO](TODO.md#runtime-and-resource-limits).

Exact preview formatting also uses `query_arrow()` and the scratch catalog so formatting cannot invalidate a retained source query.
Those shared preview facilities are separate from the temporary retrieval adapter and remain needed after it is removed.
Each scratch query registers one fixed relation and unregisters it on exit; the private catalog contains no user tables that could collide with it.
The scratch connection lives until worker shutdown.

## Generations and operations

An evaluation, input write, control target, resolver callback, candidate, and commit belong to the generation that admitted them.
Old work must never reach or commit into a replacement.

A cell is admitted once.
Its retained server admission owner exists before a worker does and records pending, dispatched, or withheld code separately from the worker's bootstrap receipt.
Worker terminal receipts or terminal generation failure settle execution; output observation grace and response delivery do not.
Completed but unclaimed responses retain the ordinary cell slot until collected.
Control-and-cell calls retain that admission across their ordered steps.
Restart input/code targets only the replacement; an interrupt's follow-up cannot silently migrate after a failure.
[Send operations](API.md#operations) owns the public ordering and partial-effect rules.

Restart prepares a changed environment before retiring the current worker.
Failure before acceptance preserves the old declaration and worker.
Failure after retirement cannot restore live state.
An established worker failure can trigger one replacement attempt, but never replay of the failed cell or input.

Interrupt selection retains one resolver operation, startup admission, or worker connection and generation through acknowledgment and observation.
A selected resolver that already completed does not authorize selecting another target.
Automatic replacement can reuse a generation, so bundled follow-up work also checks the selected connection and prior cell outcome.

## Preparation and activation

The hidden `resolve` process materializes environments under the separate [resolver policy](PACKAGE_CONFIGURATION.md).
The preparation owner publishes a result only after its process and I/O cleanup contract completes.
The server owns accepted declarations, not the materializer.

Installation warmup captures a private launch hint and gives preparation host permissions without changing cache selection or worker enforcement.
The controller removes that hint from resolver and worker launches so their subprocesses do not inherit it.
Older selected releases may ignore the hint and retain ordinary preparation.

Windows preparation adapts Rscript read/search checks with actual opens and directory listing because MXC denies R's impersonation-based ACL query even for readable files.
The adapter retains the existing preparation Job, profile-free startup, and selected Rscript identity.
It does not change existence, write, or file-execute checks; native operations continue to enforce explicit filesystem restrictions.

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
The local launch adapter retains raw exit status and requested termination independently of diagnostic suppression.
The runner owns managed temporary storage; direct workers have no general-purpose Console-owned temp root.
Outside managed sandbox storage, native Python SQL uses DuckDB's own storage defaults and lifetime.
Graceful worker shutdown uses R's normal embedded shutdown sequence, including finalizers, device closure, and best-effort session-tempdir cleanup.
Only an active sandbox with managed storage adds a temporary-storage cleanup guarantee; the runner owns that retirement, including any files R leaves behind.
Readiness and the origin of an operation failure affect error reporting, not those observations.

The existing failure-recovery barrier requires direct-launcher reaping and settled I/O.
Recovery after runner loss can still permit replacement without confirming native descendant or runner-owned storage cleanup.
A forced launcher kill, pipe EOF, or suppressed status diagnostic cannot prove that cleanup.
The pinned runner has no separate Unix cleanup receipt; a nonzero status can represent workload failure or a cleanup error.

Connection closure cancels startup/preparation, retires execution resources, settles accepted responses, and bounds blocked MCP delivery.
Physical EOF can precede transport consumption of queued requests; response settlement waits for the transport's input drain within the same delivery deadline.
Before session initialization, EOF succeeds after a complete bootstrap response and transport input drain; receiving another message clears that completion.
Native runner loss retains the [documented lifetime limits](SANDBOX_CONFIGURATION.md#supported-hosts-and-lifetime-limits); Console has no independent recovery daemon.

## Output and delivery

The server owns an ordered output tape, finite response cuts, text/image budgets, and generated notices.
Relays forward events; they do not decide MCP response boundaries or candidate acceptance.

One recoverable response remains owned until local delivery or cancellation settles it.
Failed delivery restores the entire claimed region, including output preceding a combined control-and-cell call.
An admitted poll waits within its existing deadline for pending response delivery before claiming output, including when a later cell began before the poll's handler ran.
If another call delivered the terminal result, the poll rechecks the active slot and follows the ordinary idle path, including any submitted input.
This does not guarantee exactly-once observation: cancellation can race with bytes already visible to a client.

The journal records protocol traffic, canonical calls and assembled results, and actual worker lifetimes.
Transport receipt reserves send capture in wire order without waiting for the recorder mutex or journal I/O.
The bounded capture queue retains its ownership across handler and delivery cancellation and releases request payloads after capture.
If 64 captures remain unfinished, the next send is retained in the bounded protocol queue without a canonical call record, and the transport reports the overload on stderr before retiring the connection.
Protocol receipt is queued before the corresponding send capture starts.
Response references carry the originating call's identity through handler completion, even after cancellation removes delivery ownership.
EOF shutdown closes capture admission before settling its final tail, then publishes outstanding output receipts before recording finalization.
Recording settlement has no abandonment deadline: every received send must finish capture before `recording_ended` and process exit.
This assumes filesystem operations eventually return; an indefinitely blocked recording filesystem can keep the Console process alive after transport and worker retirement.
Fail-open handling applies to reported recording errors and bounded queue overload, not cancellation of an operating-system filesystem call already in progress.
Closing the delivery gate also settles failed or abandoned protocol writes, including discovery and ping responses, before recording ends.
Markdown and QMD are projections, not interpreter checkpoints; a journaled result does not prove client receipt.
See [Recording reference](RECORDING_REFERENCE.md).

### Why native R graphics callbacks

Console retains the managed PNG device's native `newPage` and `close` callbacks.
R's `plot.new()` and `grid.newpage()` hooks do not observe every physical page transition.
For example, on an open PNG device:

```r
dev.control(displaylist = "enable")
plot(1:3)
saved <- recordPlot()
replayPlot(saved)
cat("after first replay\n")
replayPlot(saved)
cat("after second replay\n")
```

The initial plot and two replays start three pages, but the replay transitions bypass the R page hooks.
Observing only those hooks and cell end would miss the intermediate publication points and move completed images past intervening R console text.
The device callbacks publish after the backend finalizes each page, without replaying plots or capturing incomplete pages.

The [R bridge](../src/r_graphics/bridge.R) assigns each managed device a registration ID unique within its worker.
The same counter keeps managed filename templates unique across cells, since preserved user devices may still be using an earlier template.
The native close callback retires that registration; cell cleanup closes only registrations still present in the native registry.
An explicit user device that reuses a closed managed device's number remains open and uncaptured, even when it also reuses the filename template exposed by `.Devices`.
The public [plot cases](../tests/boundaries/client_server/r/test_plots.py) cover both distinct and reused templates, drawing in a later cell, and explicit closure.
The [C trampoline](../src/r_graphics.c) invokes the backend outside Rust lookup/notification frames and locks because R callbacks can `longjmp`; Rust is notified only after normal return.
Any replacement must preserve these boundaries and the [existing plot contract](RUNTIME_REFERENCE.md#plots-and-images).

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

## Application policy and launch

Console selects policy and verifies its installed, pinned native runner.
The runner owns OS enforcement, private storage, signals/terminals, and descendant retirement.
The relay owns only its direct worker, not a second process-tree supervisor.

```text
MCP server → sandbox frontend → native runner → relay → worker
                                              or standalone command
```

There is no runtime runner download or automatic retry with weaker enforcement.
[Ownership](#ownership) describes component responsibilities; [Release](../RELEASE.md#private-sandbox-executable) describes bundle verification.

## Policy extensions and compatibility

The macOS extension in [`src/sandbox/policy_extensions.sbpl`](../src/sandbox/policy_extensions.sbpl) contains Console-specific allowances and their evidence.
Host terminal reads and mutating terminal ioctls remain denied, while sandbox-created PTYs support interactive children.

For maintenance, distinguish a rule duplicated by the pinned runner from one genuinely unnecessary for the workload.
Removing an allowance needs evidence from the actual sandboxed workflow and relevant platform/runtime versions.
Passing host-resolver tests is not worker-sandbox evidence.
Historical observations and uncertain callers belong beside the rule in source, not as asserted public guarantees.

The remaining exception-evidence review is listed in [TODO](TODO.md#sandbox-and-platforms).

## Runtime integration details

R and Python initialization and reentrant bridge calls use the worker's owning thread.
R startup can mutate the native process environment; neither that thread nor the GIL serializes arbitrary native threads accessing it.

R opens installed vignette files using native filename handling and passes their bytes to a callback using Rust's `html2text` crate, without Pandoc, knitr, rmarkdown, or a browser.
Console removes the temporary converted file if a function pager raises an error.
Observable text and encoding behavior belongs in [Language behavior](RUNTIME_REFERENCE.md#installed-documentation).

## Internal variables: leave unset

Internal transport/activation names are not supported configuration.
In particular, do not use the following to choose permissions or runtimes:

`MCP_CONSOLE_LANGUAGES`, `MCP_CONSOLE_SANDBOX`, `MCP_CONSOLE_SANDBOX_SETTINGS`, `MCP_CONSOLE_SANDBOX_CONFIG`, `MCP_CONSOLE_STARTUP_FILE`, `MCP_CONSOLE_LOCAL_RUNTIME`, `MCP_CONSOLE_MANAGED_PYTHON`, `MCP_CONSOLE_R_LIBRARY`, `MCP_CONSOLE_DYNAMIC_ENVIRONMENT_RESOLUTION`, `MCP_CONSOLE_MATPLOTLIB_CACHE`, `MCP_CONSOLE_MANAGED_TEMPDIR`, or `MCP_CONSOLE_TRUSTED_WARMUP`.

The `MCP_CONSOLE_SIDEBAND_*`, `MCP_CONSOLE_INTERRUPT_HANDLE`, and `MCP_CONSOLE_INPUT_READY_HANDLE` families belong to the [worker protocol](WORKER_PROTOCOL.md).
A marker does not create a sandbox.
Use public `languages`, `python`, `r`, `requirements`, and sandbox configuration instead.

Build-time `MCP_CONSOLE_SANDBOX_SOURCE` selects a pinned companion checkout during staging; it does not replace an installed runner.
Cargo and `MCP_CONSOLE_TEST_*` values belong to build/test workflows, not this runtime interface.

Implementation owners are [`console_paths.rs`](../src/console_paths.rs), [`resolver/cache.rs`](../src/resolver/cache.rs), [`resolver/python_configuration.rs`](../src/resolver/python_configuration.rs), [`python/startup.rs`](../src/python/startup.rs), and [`worker/coordinator.rs`](../src/worker/coordinator.rs).
Keep detailed compatibility handling there rather than reproducing it across guides.

## Recording ownership and failures

One recording belongs to one Console process; numbered sessions identify actual worker lifetimes, including automatic replacements.
The relay publishes `worker_started` only after acquiring its direct child.
The server requires exactly one acquisition before readiness and rejects duplicate acquisition events as private-protocol failures.
The server queues acquisition, readiness, and exit/signal evidence through the bounded recorder drain without changing generation ownership or retirement barriers.
Journal writes do not run on the worker event dispatcher or delay its retirement join.
Observed session, cell, and image identities are reserved when events enter the bounded recorder queue, independently of the journal writer.
Consecutive pending text ranges in the same interval can share an event with the first observation timestamp; images and other events preserve their intervening boundaries.
The output tape caches interval ownership, so collecting retained restart output does not query the journal.
Raw capture handles and their publication registry use a separate lock from journal state; creating, appending, and closing interval files do not acquire the journal mutex.
Image capture reserves bytes, file count, and MIME metadata against its own interval budget before preview admission; preview omission retains the saved file receipt.
Assembled results can also contain images rejected from raw capture, recorded without image data or a fabricated file reference.
Startup failure records carry a session number only when that startup acquired a worker.

Before send, the recorder uses private temporary storage for the same journal, output logs, images, and projections.
First-send publication relocates shared output writers while preserving producer ownership, sequence numbers, and observation timestamps.
Output receipts resolve their public paths only after successful publication; failed publication leaves omitted text unavailable.
It preserves the existing storage-root selection, identifier generation, permissions, and retention limits.
`capture/events.jsonl` is the append-only journal underlying the Markdown and QMD projections.

The output tape owns observed startup, cell, and post-cell boundaries within each worker lifetime.
The worker's `cell_started` receipt begins execution capture at evaluator entry, after admission; a withheld source gets no executed-cell log.
The `cell_started` capture event records the call and interval owner; withheld source remains in `tool_call` without an executed-cell association.
Source admission, worker association, and settlement use that same bounded recorder queue without blocking MCP reception or worker event dispatch on journal I/O.
Admitted source survives an initial response until association or evaluation settlement.
`source_admitted` and `source_finished` follow server admission, which can begin before worker acquisition and settle without execution.
The admission owner records settlement at its first terminal outcome, including operation failure and retirement before evaluation completion.
They cannot be inferred from `cell_started` and `cell_ended`, which describe actual worker execution.
`source_associated` identifies the owning worker without claiming that the source ran.
Actual acquisition and dispatch associate source with its worker; validation rejection can associate otherwise unclaimed source only with the still-surviving addressed worker.
Calls without source do not acquire source associations.
Unsettled associations retain call identity and settlement flags, and the source projection retains source rather than complete requests or stdin.
Both release settled call entries.
Completion closes the cell interval, and the final startup or post-cell interval includes shutdown output.
Confirmed completion follows protocol validation; cell entry observed during retirement still starts its capture interval.
Worker termination closes the final interval and clears its owner before later process diagnostics arrive.
If the relay omits a worker outcome, launcher retirement and joined output producers close that interval before replacement preparation or launch; no worker-outcome event is fabricated.
The `image_retained` capture event records each saved image's owner, artifact ID, and file path.
Capture events carry explicit owners and raw text byte ranges; preview receipts carry the same owners into assembled response fragments, including later polls and replacement responses.
Combined text/image omission notices retain their common observed interval owner; mixed or unknown ownership is recorded as null.
Image omission ownership is aggregated before per-interval receipts retire, with bounded state independent of the number of omitted images.
Response assembly uses those receipts without waiting for the journal to rediscover ownership.

Raw files live under `capture/sessions/NNN/`; recording-wide `transcript.md` and source-only `sessions/session-NNN.qmd` remain projections of the journal.
Session documents follow actual worker lifetimes, including automatic replacements, and are created only when source is associated.
Their metadata follows accepted live requirements and acquisition snapshots, including retained R declarations; replacement preparation does not rewrite a retiring session.
The Markdown projection retains calls and assembled results, including previews and omission notices, while the journal retains routine protocol, delivery, and low-level lifecycle evidence.
Integrator calls may omit their human-readable projections while preserving journal and raw capture.
That choice moves from the recorded call into its evaluation owner, then into observed cell/post interval owners, so admission settlement cannot erase it before a late cell-entry receipt.
Result fragments retain the same choice when later responses include their output; projection writers filter those fragments without changing the journal or delivery data.
Completed interval files and images become read-only after their writers retire.
The temporary-publication registry keeps weak references to active file handles and releases completed entries; publication copies completed files while relocating live producers.
Process-wide files finalize only after retirement, delivery settlement, queued initial capture, and blocking result writers settle.
Session QMD permissions finalize at that same barrier because source associations and metadata updates can follow worker termination.
Capture completion ownership survives handler cancellation and delivery deadlines, including after a result writer releases the journal mutex.
Finalization completes permissions on retained files even after recording or projection failure, without inventing a successful `recording_ended` event for disabled capture.

A journal or artifact failure disables further recording without stopping computation.
Queued and in-flight protocol, worker lifecycle, and output capture events share a bound of 1,024 events or 8 MiB of serialized event data.
Exceeding either limit discards pending capture events and disables recording; it does not wait for the journal writer or close MCP input.
The current write can finish before the recorder reports the failure, so the retained journal may end partway through the exchange.
A raw-log failure affects that file.
Failure of a derived projection disables the projections but can leave the journal and artifacts available.
Diagnostics and notices report unavailable or partial retention.

Journal events and generated projections are internal formats, not independently versioned public APIs.
A journaled result records response assembly.
Separate delivery events record whether a local transport write completed successfully, without claiming client acknowledgment or model observation.
See [Output and delivery](#output-and-delivery) for ownership and [Open work](TODO.md) for retention and export gaps.

## Developer terms

**Server / controller:** The process speaking MCP.
It owns admission, retained declarations, bounded responses, and recording.

**Worker:** The process holding interpreter and database state.
The built-in worker coordinates ordinary R, Python, and SQL cells on one execution thread.
Opt-in `execution: "immediate"` can reenter R on that thread or execute Python on a background thread, according to the request's language.

**Generation:** The admission and callback identity retained by lifecycle operations.
Old input, callbacks, evaluations, and candidate commits must not reach its replacement.
Automatic recovery can replace a worker within a generation; recording session numbers identify those distinct worker lifetimes.
Connection checks continue to fence work when the generation is reused.

**Relay:** The process translating worker sideband and standard streams, delivering interruption, and reaping the direct worker.
It is not the native sandbox supervisor.

**Sideband:** Private JSONL commands and semantic events, separate from stdin/stdout/stderr.

**Native runner:** The verified private executable owning native policy enforcement, temporary storage, and its descendant-cleanup contract.

**Preparation owner:** The machinery owning discovery/materializer processes and their cleanup.
The server, not this owner, accepts declarations.

**Candidate / activation:** A proposed environment and the step making it usable by a worker.
Materialization, activation, and server acceptance are distinct.

**Retirement / cleanup receipt:** Stopping an owned resource and obtaining confirmation that its owner's cleanup contract completed.
Stream closure alone is not that confirmation.

**Output cut:** A finite boundary selected for one response.
It does not establish chronology across independent streams.

**Execution host:** The machine or environment where the client, Console, and its files/processes run.
