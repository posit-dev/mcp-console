# AGENTS.md

This file records an exploratory product sketch for MCP Console.
It is not repository-wide implementation guidance or documentation of current behavior.
Files in this directory may describe different directions and need not be complete or mutually consistent.
Production correctness and consistency requirements apply when a proposal is implemented; a sketch does not need to settle every edge case or update every related draft.
Use the repository-root `AGENTS.md` and `docs/README.md` for the implemented system.

## Product intent

MCP Console is a persistent, sandboxed R, Python, and DuckDB SQL console exposed through MCP.
A named session contains one R process, one Python interpreter embedded through reticulate, and one persistent DuckDB connection.
The agent can alternate among R, Python, and SQL while retaining objects, imports, package state, database state, debugger state, and files.

The public abstraction is a **console session**.
Normal top-level input is one complete R, Python, or SQL cell.
`stdin` is a distinct worker stream that may be queued whether the session is evaluating or idle, including for R `readline()` or `browser()`, Python `input()` or a debugger, and background runtime jobs.

The tool must be cheap enough to remain globally enabled.
Common calls should look like:

```text
{"python":"..."}
{"r":"..."}
{"sql":"..."}
{}
```

The empty object waits for or polls the default session.
Replies are bounded text.
Complete explicit stream output and generated artifacts live in managed session files.
Each server lifetime maintains one durable JSONL journal across worker restarts.
Every worker generation has its own generated `transcript.md` that an agent can read after context compaction, plus an executable `transcript.qmd` containing only that generation's submitted code cells for later source reuse or reproducible rendering.
The nested layout and retention rules are described in [`CONFIGURATION.md`](CONFIGURATION.md).

Humans may attach to the live MCP server through a process-scoped local API.
That API supports observation, structured inspection, plot viewing, bounded live-table exploration, point-in-time snapshots, and explicitly attributed external control without adding more MCP tools or flooding model context.
It is not a persistent daemon and does not keep the MCP server alive.

MCP Console is effectively shell-class capability.
Safety is enforced around the worker process and its descendants, not by filtering language source.

## Decisions in this sketch

- Product and binary name: `mcp-console`.
- MCP initialization identity: `mcp-console`.
- CLI operations require explicit subcommands.
  The MCP stdio server command is `mcp-console serve`, and the default packaged server command is `uvx mcp-console serve`.
- Default client registration name: `console`.
  With an installed binary, the Codex registration command is `codex mcp add console -- mcp-console serve`.
- Frequent MCP tool: `send`.
- Low-frequency environment and lifecycle tool: `session`.
  Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
- Language is selected by the present object key: `r`, `python`, or `sql`.
- Top-level submissions are complete cells, not line-by-line parser input.
- `stdin` may accompany a code cell or be sent on its own.
  It is queued to the session worker immediately whether the session is evaluating or idle and may remain available to later reads.
  It is exact stream text, may contain multiple lines, receives no implicit newline, and is not acknowledged as consumed.
- Runtime input requests are provisional state events.
  A matching receipt means that read succeeded; it does not identify which queued payload or bytes satisfied it.
- A named session runs at most one top-level evaluation at a time.
- A logical session is created by its first code submission, nonempty stdin submission, or successful `prepare` action; `prepare` may leave it configured without a worker until code or stdin is submitted.
- Independent or parallel work uses separately named sessions.
- MCP results are text-only; v1 has no `outputSchema`, structured-content mirror, MCP resource dependency, or inline image result.
- Oversized explicit output is retained in bounded session spools; each response contains only a bounded current excerpt.
- Large values and SQL relations are previewed structurally before full textual materialization.
- The agent-facing durable records are the retained generations' `transcript.md` files; each `transcript.qmd` is a source-only code-cell projection, and one granular JSONL journal spans the server lifetime.
- Requirements are additive logical-session configuration managed by `session`.
  They survive runtime restarts and are not accepted on ordinary `send` calls.
- Interrupt, restart, close, and worker crash are distinct observable events.
  `restart` loses in-memory R, Python, SQL, debugger, and process state while retaining requirements, workspace files, the server journal, and all earlier generation projections.
  A crash fails the active evaluation and is recorded before the next evaluation starts a fresh worker generation.

See [`VISION.md`](VISION.md) for the fuller rationale and [`docs/MCP_INTERFACE.md`](docs/MCP_INTERFACE.md) for normative MCP behavior.

## Runtime requirements and open backend decision

The product-level runtime requirements are settled:

- One logical session maps to one sandboxed worker process and one runtime generation.
- R is the host runtime; Python is embedded into that R process through reticulate.
- SQL initially uses one persistent DuckDB connection through the DuckDB R package and DBI.
- The DuckDB CLI is not run or embedded.
  Its bounded table presentation and interactive ergonomics are references only.
- The supervisor consumes one normalized runtime service for evaluation, output, input, interrupt, lifecycle, plots, help, object inventory, and typed inspection.
- The public MCP and local sidecar contracts must not expose the selected backend transport.

The implemented text R console uses the purpose-built native worker.
An Ark-backed R-only prototype was also implemented and evaluated before that choice.
The broader R/Python/SQL and inspection backend remains open pending the remaining work in [`docs/RUNTIME_BACKEND.md`](docs/RUNTIME_BACKEND.md):

- **Ark-backed worker:** use Ark's Jupyter kernel and mature R-side facilities, including Data Explorer comms, where they can be consumed without an invasive fork.
- **Native worker:** build a purpose-specific runtime from `mcp-repl`, `harp`, and `libr`, implementing the same normalized service directly.
- **Shared extraction:** prefer an upstreamable reusable R-runtime layer when feasible; do not maintain two divergent native R frontends casually.

Running Ark may justify Jupyter as a private adapter transport.
A custom worker should use a smaller purpose-built protocol.
Neither choice may change agent-visible semantics, sidecar authorization, attribution, output bounds, or transcript behavior.

### Evaluation boundaries

The worker receives structured commands such as `EvaluateCell`, `QueueInput`, `Inspect`, and `Interrupt`.
Complete code cells and interactive input must remain distinct commands and state queues.

R cells must have native top-level semantics regardless of backend.
Preserve console visible-value behavior without placing a user-visible MCP Console wrapper around the entire cell.
A user call to `sys.calls()` should not contain an MCP Console dispatcher frame merely because the code came from the tool.
The initial comparison verified this behavior for the native worker and found that Ark's value proxy changes the expression received by top-level task callbacks.

A native DLL-REPL backend may route cell source through a custom `ReadConsole` callback while retaining stdin on the worker's fd 0 stream.
It must use interpreter state rather than prompt text to feed cell source only at primary or continuation reads.
Runtime input request events describe supported console reads; they do not gate stdin delivery or cover direct fd-0 reads.

Python uses a persistent cell executor in `__main__`, with a synthetic source filename, statement support, final-expression display, and language-native tracebacks.
Do not use `reticulate::repl_python(input = ...)` as the core cell evaluator.
A minimal R/reticulate bridge frame is acceptable in the initial implementation and must be treated as an explicit boundary.

SQL initially crosses a minimal private R bridge into DBI/DuckDB.
During an SQL evaluation, R introspection reached through a callback may therefore see the console SQL bridge and DBI frames.
Do not fabricate or rewrite `sys.calls()` to hide real boundaries.
Store source out of band and pass a short evaluation ID through bridge calls so large cells do not appear in R call expressions.

## Settled sidecar and viewer decisions

- The MCP stdio server owns the local API and all session workers.
  The API starts and stops with that process; no command auto-starts a detached daemon, and viewers do not prolong server lifetime.
- A sidecar target is `(instance_id, session_name)`, not merely a session name.
  Several MCP clients may run independent servers containing a session named `default`.
- Use a protected local transport: HTTP semantics over a Unix-domain socket on Unix and a user-restricted named pipe or authenticated loopback endpoint on Windows.
  The browser viewer connects through a short-lived loopback proxy started by `mcp-console view`.
- Local attachment assumes the viewer shares a reachable OS or container namespace with the server.
  Do not silently broaden the listener when the server is remote or container-isolated; fail clearly and treat explicit forwarding as a future host-integration feature.
- Sidecar clients use a bounded session snapshot plus a resumable event stream.
  Events have cursors, replay is bounded, slow subscribers are disconnected rather than blocking execution, and stale cursors produce an explicit resynchronization request.
- Event messages carry bounded metadata and managed IDs or offsets.
  Full output, tables, plots, and files are fetched separately.
- The API has three operation classes:
  - **Observe:** reads supervisor-owned state and managed files; never enters the runtime.
  - **Inspect:** sends typed, bounded requests to supported runtime adapters; accepts no caller-supplied R, Python, or SQL source.
  - **Control:** submits code, stdin, environment changes, or lifecycle operations through the same session state machine as MCP.
- Arbitrary external code is always a primary attributed evaluation.
  It receives an evaluation ID, appears in `transcript.md`, emits ordinary events, and causes a compact notice to the MCP-side agent before later state-dependent work.
  Never add a hidden or nominally “read-only” arbitrary-code path.
- R and embedded Python are owned by one runtime thread.
  Generic inspection runs only while the session is idle and returns `session_busy` otherwise.
  Waiting in a debugger or `input()` is not idle.
- Object handles are opaque and scoped to server instance, session generation, and object revision.
  Restart, rebinding, or incompatible mutation makes them stale.
- The data explorer supports two explicit modes.
  A **live view** retains a revisioned object reference and serves bounded typed viewport/profile requests through the runtime backend; it normally requires an idle runtime.
  A **snapshot view** materializes immutable data for stable, concurrent exploration outside the live runtime.
- Ark's Data Explorer comm/backend is a candidate implementation of live views, not the public sidecar protocol.
  A native backend must provide equivalent typed capabilities or declare narrower support.
- Prefer Arrow IPC, Parquet, or a read-only DuckDB-backed representation for snapshots.
  A dedicated view engine or helper process may own them; do not load arbitrary user packages into the supervisor.
- Supported ephemeral view filters, sorts, and projections may be converted to source code for the original table type.
  Conversion returns text only; execution must use the attributed primary evaluation path.
- Plot and artifact routes resolve only managed artifact IDs.
  Never expose arbitrary filesystem paths, and render active formats such as SVG under a restrictive policy.

See [`docs/SIDECAR_API.md`](docs/SIDECAR_API.md) for the local protocol and [`docs/CLI.md`](docs/CLI.md) for the user-facing commands.

## Repository sitemap

The exact tree may evolve.
Update this map whenever ownership moves.

### Root documents

- `README.md` — user-facing overview, status, examples, installation, and document index.
- `VISION.md` — product purpose, goals, non-goals, and success criteria.
- `AGENTS.md` — durable agent context, settled decisions, repository map, and working rules.
- `CONFIGURATION.md` — proposed `.agents/console/config.yaml`, session profiles, sandbox-owned policy validation, and centralized Console records and storage; design sketch only.
- `docs/MCP_INTERFACE.md` — normative agent-facing schema and observable behavior.
- `docs/TOOL_DESCRIPTIONS.md` — exact registered tool and property descriptions.
- `docs/CLI.md` — standalone binary, installation, diagnostics, viewer, watch, and sidecar-control commands.
- `docs/SIDECAR_API.md` — process-scoped local API, event model, inspection, live and snapshot data views, plots, and external control.
- `docs/RUNTIME_BACKEND.md` — initial Ark-versus-native worker evaluation, remaining full-runtime work, and decision gate.
- `docs/R_REPL_DLL_ITERATOR.md` — native DLL-REPL findings, decision, and implementation record.
- `docs/ARCHITECTURE.md` — implementation architecture and staged plan.

Create focused documents only when a subsystem has enough detail to justify a separate source of truth:

- `docs/WORKER_PROTOCOL.md` — exact private IPC messages and ordering.
- `docs/LOCAL_API_PROTOCOL.md` — generated local API contract once endpoint names stabilize.
- `docs/OUTPUT_AND_TRANSCRIPTS.md` — output timeline, spools, previews, document generation, and retention.
- `docs/SANDBOX.md` — platform policies and inherited-host sandbox behavior.
- `docs/DEPENDENCIES.md` — R/Python requirement grammar, resolution, caching, and activation.
- `docs/TESTING.md` — supported runtime matrix, integration harness, fixtures, and snapshot rules.
- `docs/adr/` — small decisions whose alternatives and consequences need a durable record.

### Planned source layout

- `src/main.rs` — dispatch MCP supervisor, worker, installation, diagnostics, and sidecar CLI modes.
- `src/cli.rs` — command-line options and target selection.
- `src/config.rs` — validated server, output, runtime, retention, local API, and sandbox configuration.

- `src/mcp/` — thin MCP transport and tool adapter.
  - Defines `send` and `session` schemas.
  - Validates public arguments.
  - Converts service results to bounded MCP text.
  - Contains no interpreter mechanics.

- `src/session/` — product-level named-session state machine.
  - Owns generations, lazy creation, state transitions, waiters, evaluation origins, and lifecycle operations.
  - Rejects new code while a session is running or waiting for input.

- `src/worker/` — worker process launch, supervision, private protocol, and control path.
  - The normal command path may block behind evaluation.
  - Interrupt and forced termination must have an independent high-priority path.

- `src/runtime/backend.rs` — normalized runtime service and capability model consumed by the session manager.

- `src/runtime/native/` — candidate purpose-built backend using `mcp-repl`, `harp`, and `libr`: R startup, native cell evaluation, callbacks, graphics, help, inspection, and interrupt recovery.

- `src/runtime/ark/` — candidate Ark/Jupyter adapter: kernel lifecycle, message correlation, stdin/control, comm translation, and capability negotiation.
  Keep Jupyter types inside this module.

- `src/runtime/python/` — shared reticulate cell semantics, exceptions, display hook, stdin/debugger bridge, and R/Python object access used or validated by either backend.

- `src/runtime/sql/` — shared DuckDB/DBI semantics, bounded fetching, table previews, environment scanning, and explicit relation registration used or validated by either backend.

- `src/runtime/` shared modules — submission IDs, language-neutral input requests, display values, object handles, inspection requests, runtime capabilities, and outcomes.

- `src/environment/` — additive R/Python manifests, resolver process, immutable caches, activation, and provenance.

- `src/output/` — managed and raw stream intake, per-evaluation spools, reply cursors, value/table previews, and final response budgets.

- `src/transcript/` — internal journal model, generated Markdown ledger, and source-only Quarto projection.
  Users and agents must not edit the live generated documents; the server appends Markdown and regenerates the QMD from incremental source and requirement state.

- `src/local_api/` — process-scoped service router, local transports, protected discovery records, handshake, authorization, snapshots, event replay, and managed-file delivery.

- `src/inspection/` — typed object inventory, opaque handles, runtime-thread scheduling, live-view operations, snapshot creation, staleness, invalidation, and quotas.
  It contains no generic caller-supplied evaluator.

- `src/view/` — live/snapshot table-view catalog, Arrow/Parquet/DuckDB-backed snapshot engine, typed filter grammar, profiling, and view retention.
  Live operations delegate through the selected runtime backend; snapshot operations stay outside the live R/Python runtime where possible.

- `src/viewer/` — bundled static UI and short-lived loopback browser proxy used by `mcp-console view`.

- `src/sandbox/` — worker-process filesystem, network, subprocess, resource, and host-policy enforcement.

- `tests/` — real-binary MCP and local-API integration tests grouped by public behavior.
- `fixtures/` — deterministic data, fake workers, fake sidecars, and normalized expected output.
- `scripts/` — local equivalents of CI integration and session-inspection workflows.

## Working rules

1. Preserve the two-tool MCP surface.
   Viewer and integration capabilities belong on the local sidecar API, not as more globally visible MCP tools.
2. `docs/MCP_INTERFACE.md` and `docs/SIDECAR_API.md` are related interface sketches, not production contracts.
   Reconcile the relevant interface, integration tests, and implemented documentation when implementing a proposal; exploratory edits do not require synchronizing every draft.
3. Keep complete cells separate from `stdin`.
   Never route top-level code through the runtime input queue.
4. Never infer idle, completion, debugger state, or input state from visible prompt strings.
5. Keep arbitrary R, Python, SQL, native-library, and child-process execution inside the sandboxed worker.
6. Keep the MCP supervisor responsive and free of user-loaded native libraries.
   If table exploration needs a heavy query engine, isolate it behind a bounded view component or helper process.
7. Bound output before it becomes MCP text or a sidecar event.
   Do not stringify or collect an arbitrary object or relation in full and truncate afterward.
8. Polls return only newly observed bounded output and current state.
   Unseen overflow stays in the evaluation spool and is not forced through later calls.
9. Keep the internal journal and generated documents separate.
   The journal may be granular; Markdown is the readable agent artifact and QMD contains only source cells.
10. Record a worker crash before automatically starting a fresh worker.
    Preserve honest state-loss and generation boundaries.
11. Observation must never enter the runtime.
    Structured inspection must not accept caller-provided language source.
    Arbitrary external code must use the primary evaluation path and be attributed.
12. Serialize inspection on the runtime-owning thread and reject it while busy unless a runtime-specific capability proves a safe cooperative boundary.
13. Slow or disconnected viewers must never block evaluation, transcript writing, or event publication.
    Use bounded queues, cursors, replay, and explicit resynchronization.
14. Never let object handles, table views, or artifacts escape their instance, session, generation, revision, and retention boundaries.
    These are local-API identity boundaries; managed files remain readable across sessions and generations unless the user explicitly restricts filesystem reads, as described in [`CONFIGURATION.md`](CONFIGURATION.md).
15. Do not serve arbitrary paths.
    Local API clients fetch only supervisor-managed output, artifact, transcript, and snapshot IDs.
16. Test through the built binary and real runtimes.
    Use unit tests for local algorithms, not as a replacement for MCP and local-API integration tests.
17. Add acceptance tests for R stack fidelity: ordinary top-level R evaluation must not introduce a console-owned R closure frame.
18. Keep the session manager, MCP adapter, transcript pipeline, and local sidecar API backend-neutral.
    Jupyter messages and Ark comm payloads must not escape `runtime/ark`; `harp`/`libr` types must not escape `runtime/native`.
19. Isolate backend-specific and unstable API use behind one adapter with pinned compatibility tests.
20. Do not generalize the native text-R decision into a repository-wide backend assumption.
    Complete the remaining full-runtime evaluation, record the result as an ADR, and update this file.
21. Record other substantial unresolved choices as focused spikes or ADRs, then update this file when they become settled.
22. Keep this sitemap accurate.
    An agent should be able to find the owner of a behavior without broad exploratory search.

## First implementation priorities

1. Build the session state machine, normalized runtime interface, and deterministic fake backend around the implemented persistent R worker.
2. Complete the remaining backend evaluation covering interrupts, plots/help, Python and SQL dispatch, and an independent large-table viewer using live viewport requests.
3. Record the full-runtime backend decision, then extend the selected worker with structured interrupt, restart, crash reporting, and capability negotiation.
4. Bounded output spools, reply cursors, value previews, generated `transcript.md` and source-only `transcript.qmd`, and managed artifacts.
5. Reticulate Python cell execution and interactive input.
6. Persistent DuckDB through R/DBI, bounded SQL results, R environment scanning, and explicit registration.
7. Atomic session requirement preparation and restart.
8. Process-scoped local service, protected discovery, bounded resumable events, `list`, and `watch`.
9. Structured object inspection, live table views, immutable snapshots, and the bundled plot/data viewer.
10. Cross-platform sandbox, local-web trust boundary, quota, and resource hardening.
