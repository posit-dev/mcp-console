# MCP Console Architecture

**Status:** Draft implementation scaffold v0.3 \
**Date:** 2026-07-27 \
**Companion documents:** [`../VISION.md`](../VISION.md), [`MCP_INTERFACE.md`](MCP_INTERFACE.md), [`SIDECAR_API.md`](SIDECAR_API.md), [`RUNTIME_BACKEND.md`](RUNTIME_BACKEND.md)

## 1. Purpose

This document describes the implementation boundaries that support the public MCP Console behavior.
It is intentionally more specific than a vision document but less rigid than a private wire-protocol specification.

The architecture must support:

- a compact, high-frequency MCP interface;
- persistent R, Python, and SQL state in one session process;
- complete-cell evaluation plus genuine interactive stdin;
- precise state, interrupt, and failure behavior;
- bounded model-facing output with complete retained streams;
- generated Markdown and source-only Quarto documents;
- process-scoped human observation and typed large-object inspection;
- process-level isolation around arbitrary code.

## 2. Architectural summary

MCP Console ships one user-facing Rust binary and launches one sandboxed worker process per session.
The implemented text R console uses the built-in purpose-specific worker; the broader runtime remains behind the full-runtime backend decision:

```text
mcp-console                        MCP supervisor/server
mcp-console worker ...             current native text-R worker
ark or an Ark-linked worker mode   Ark-backed candidate
```

The final package may bundle an internal companion executable or compile an Ark mode into the same distribution.
This must not add another user-facing installation or configuration step.

The supervisor owns MCP, named sessions, package preparation, worker lifecycle, output budgets, session files, and transcript projection.
It never loads R, Python, DuckDB, or arbitrary user native libraries.

Each named session owns one worker process.
The worker embeds R on its main thread.
Reticulate embeds one Python interpreter inside that R process.
The DuckDB R package and DBI initially own one persistent in-memory SQL connection.

```text
MCP client
    │ MCP stdio
    ▼
Rust supervisor
    ├── MCP adapter
    ├── session manager
    ├── dependency resolver
    ├── worker supervisors
    ├── output/transcript pipeline
    └── sandbox policy
          │
          ├── worker: "default"
          │     └── embedded R
          │           ├── persistent R user environment
          │           ├── reticulate Python __main__
          │           └── persistent DuckDB connection
          │
          └── worker: "model-fit"
                └── embedded R + Python + DuckDB
```

The supervisor consumes a normalized runtime service specialized for evaluation, output, interactive input, inspection, and control.
A purpose-built backend may implement that service with a small private protocol.
An Ark-backed backend may translate Jupyter messages and Ark comms inside its adapter.
Backend transport must not leak into MCP, session, transcript, or local sidecar behavior.

## 3. Core invariants

1. Arbitrary user code runs only in a sandboxed worker.
2. One logical session maps to one worker process and one active generation.
3. R, Python, and SQL for a session inhabit the same worker process.
4. One backend-owned thread owns all direct calls into R.
5. A worker executes at most one top-level evaluation at a time.
6. Complete cells and interactive stdin are different internal command types.
7. Complete cells and worker stdin remain distinct commands and streams, even when a native DLL-REPL adapter obtains cell source through `ReadConsole`.
8. R cells do not acquire a console-owned interpreted R frame.
9. Runtime state comes from structured events, never prompt-string matching.
10. Interrupt and termination control cannot wait behind the evaluation command queue.
11. Every MCP response is bounded.
12. Full explicit stream output remains outside model context.
13. Known large values are previewed before full textual materialization.
14. Restart and crash create explicit state-loss boundaries.
15. The Markdown transcript and source-only QMD are generated from a more authoritative internal record.
16. Runtime transport is encapsulated.
    Ark/Jupyter or a native `harp`/`libr` worker may implement the same service, but backend-specific types never cross the adapter boundary.
17. Live-object inspection is typed, bounded, revisioned, and distinct from arbitrary evaluation.
18. The public MCP schema grows only when language code, runtime helpers, or files cannot express the workflow.

## 4. Runtime backend decision gate

### 4.1 Status

The current text R console uses the implemented native DLL-REPL worker.
An Ark-backed R-only prototype was also implemented and evaluated; the native worker was selected for that slice because Ark required the full Jupyter adapter and a local browser-prompt API addition.
The eventual R/Python/SQL and inspection backend remains open until the remaining implementation work is evaluated.
The two serious candidates are:

1. an **Ark-backed worker**, with the supervisor acting as a Jupyter client and translating Ark's execution, stdin, control, display, and custom comm messages into MCP Console's normalized runtime events;
2. the **purpose-built native worker**, built on `harp`, `libr`, and libR's DLL-REPL API, with a narrower private protocol designed directly around MCP Console's multi-language semantics.

A third outcome—extracting a reusable lower-level runtime shared by Ark and MCP Console—is preferred when practical, but it cannot be assumed before the remaining full-runtime evaluation.

The comparison and acceptance matrix live in [`RUNTIME_BACKEND.md`](RUNTIME_BACKEND.md).
Do not generalize the initial text R decision into a repository-wide backend invariant before the remaining decision is recorded.

### 4.2 Stable runtime service

The session manager depends on behavior, not transport:

```rust
trait RuntimeBackend {
    fn capabilities(&self) -> RuntimeCapabilities;
    fn evaluate(&mut self, request: EvaluateCell) -> EvaluationHandle;
    fn queue_input(&mut self, request: QueueInput) -> Result<()>;
    fn interrupt(&mut self, evaluation: Option<EvaluationId>) -> Result<()>;
    fn inspect(&mut self, request: InspectionRequest) -> InspectionHandle;
    fn restart(&mut self, environment: ResolvedEnvironment) -> Result<()>;
    fn shutdown(&mut self, reason: ShutdownReason) -> Result<()>;
}
```

The concrete interface may be asynchronous and message-driven, but it must normalize:

- readiness, busy, idle, input-required, stopped, and crash states;
- R, Python, and SQL cell execution;
- stdout, stderr, display values, plots, help, and artifacts;
- interactive input and debugger commands;
- interrupt and shutdown control;
- object inventory, opaque references, bounded slices, profiles, and invalidation;
- evaluation and inspection attribution.

MCP tools and sidecar clients never send or receive Jupyter messages, Ark comm payloads, `harp` objects, or raw worker protocol frames.

### 4.3 What Ark may buy

Ark already contains substantial behavior relevant to this product:

- robust R discovery, startup, event processing, and platform handling;
- structured complete-cell execution with busy/idle and parent-message identity;
- stdin and separate control channels;
- stdout, errors, plots, help, Variables, debugger, and other IDE integrations;
- retained object references and a mature Data Explorer backend/comm for bounded row, column, filter, sort, and profile requests;
- potential compatibility with existing Positron or Canvas frontend code.

These capabilities are especially relevant now that human inspection is a defining product feature rather than an optional viewer enhancement.

### 4.4 Costs and constraints of Ark

Ark remains an R Jupyter kernel, not a polyglot MCP Console runtime.
An Ark-backed implementation must still provide:

- first-class R, Python, and SQL submission identity;
- minimal and truthful Python/SQL bridge frames and source locations;
- compact MCP wait and polling semantics;
- bounded text output and managed sidecars;
- named sessions, requirement manifests, sandbox policy, and generated session documents;
- a process-scoped local API that proxies or translates Ark comms rather than exposing them directly;
- an upgrade strategy that avoids an unmaintainable fork.

It also brings Jupyter connection management, ZeroMQ, comm lifecycle, and IDE-oriented components.
Those costs are acceptable only if reused behavior—especially Data Explorer and R frontend correctness—substantially exceeds the integration burden.

### 4.5 Costs and constraints of a native worker

A purpose-built worker gives MCP Console a small multi-language request model and direct control over sandboxing, output, transcript boundaries, and sidecar semantics.
It avoids translating through an R-kernel abstraction.

It also makes this project responsible for difficult native-R behavior that Ark already implements: startup, event processing, input, interrupts, plots, help, source references, debugging, platform differences, object references, and a capable large-data inspection backend.
Basic row slicing is not an adequate substitute for the mature Data Explorer behavior users may expect.

### 4.6 Decision rule

Select Ark when the spike shows that:

- complete-cell, stdin, debugger, interrupt, and stack semantics satisfy the public contract;
- Python and SQL can be made first-class without invasive Ark changes or confusing diagnostics;
- an independent sidecar can reuse the Data Explorer and plot/help machinery through a stable adapter;
- packaging, startup, sandboxing, and version compatibility are acceptable;
- required changes can be upstreamed or maintained without a long-lived fork.

Select the native worker when Ark's full-kernel integration or required extensions outweigh the capabilities MCP Console would reuse, and a narrower live/snapshot inspection backend can meet the product requirements with materially less complexity.

Regardless of the outcome, record the decision as an ADR and retain the backend-neutral service boundary.

## 5. Repository layout

Begin as one Cargo package.
Split crates only when reuse, dependency isolation, or build performance makes the boundary real.

```text
.
├── Cargo.toml
├── Cargo.lock
├── README.md
├── VISION.md
├── AGENTS.md
├── docs/
│   ├── MCP_INTERFACE.md
│   ├── SIDECAR_API.md
│   ├── RUNTIME_BACKEND.md
│   └── ARCHITECTURE.md
├── src/
│   ├── main.rs
│   ├── cli.rs
│   ├── config.rs
│   ├── error.rs
│   ├── mcp/
│   │   ├── mod.rs
│   │   ├── server.rs
│   │   ├── tools.rs
│   │   ├── send.rs
│   │   └── session.rs
│   ├── session/
│   │   ├── mod.rs
│   │   ├── id.rs
│   │   ├── manager.rs
│   │   ├── state.rs
│   │   ├── generation.rs
│   │   └── paths.rs
│   ├── worker/
│   │   ├── mod.rs
│   │   ├── main.rs
│   │   ├── process.rs
│   │   ├── supervisor.rs
│   │   ├── protocol.rs
│   │   └── control.rs
│   ├── runtime/
│   │   ├── mod.rs
│   │   ├── backend.rs
│   │   ├── capabilities.rs
│   │   ├── cell.rs
│   │   ├── input.rs
│   │   ├── display.rs
│   │   ├── inspection.rs
│   │   ├── native/
│   │   │   ├── mod.rs
│   │   │   ├── ffi.rs
│   │   │   ├── startup.rs
│   │   │   ├── eval.rs
│   │   │   ├── console.rs
│   │   │   ├── interrupt.rs
│   │   │   └── graphics.rs
│   │   ├── ark/
│   │   │   ├── mod.rs
│   │   │   ├── kernel.rs
│   │   │   ├── messages.rs
│   │   │   ├── comms.rs
│   │   │   └── translate.rs
│   │   ├── python/
│   │   │   ├── mod.rs
│   │   │   ├── bridge.rs
│   │   │   ├── cell.py
│   │   │   └── input.py
│   │   └── sql/
│   │       ├── mod.rs
│   │       ├── bridge.R
│   │       ├── connection.rs
│   │       ├── fetch.rs
│   │       └── register.rs
│   ├── environment/
│   │   ├── mod.rs
│   │   ├── manifest.rs
│   │   ├── resolver.rs
│   │   ├── r.rs
│   │   ├── python.rs
│   │   └── cache.rs
│   ├── output/
│   │   ├── mod.rs
│   │   ├── timeline.rs
│   │   ├── spool.rs
│   │   ├── preview.rs
│   │   ├── table.rs
│   │   └── response.rs
│   ├── transcript/
│   │   ├── mod.rs
│   │   ├── journal.rs
│   │   ├── model.rs
│   │   └── qmd.rs
│   └── sandbox/
│       ├── mod.rs
│       ├── policy.rs
│       ├── linux.rs
│       ├── macos.rs
│       ├── windows.rs
│       └── codex.rs
├── tests/
│   ├── common/
│   ├── interface/
│   ├── runtime/
│   ├── lifecycle/
│   ├── output/
│   └── sandbox/
├── fixtures/
└── scripts/
```

Embedded R, Python, and SQL helper source belongs next to the Rust adapter that owns its behavior.
It is implementation code and must be tested and versioned like Rust code.

## 6. Process model

### 6.1 Supervisor

The supervisor is the long-lived MCP server and trusted control plane.

Responsibilities:

- speak MCP over stdio initially;
- expose and validate the two public tools;
- manage logical session names, states, and generations;
- prepare additive package requirements;
- launch workers under explicit sandbox policy;
- own command, sideband, control, stdout, and stderr channels;
- enforce wait, polling, cancellation, and output-budget behavior;
- write output spools, internal records, and document projections;
- report worker crashes without corrupting MCP stdout;
- restart, close, and retain session files according to policy.

The supervisor must never load user packages or native extensions.

### 6.2 Worker backend

The selected worker backend is the arbitrary-code boundary.
It may be a purpose-built embedded-R process or an Ark kernel process, but it must present the same normalized service to the supervisor.

Responsibilities:

- initialize and own R on the correct thread;
- create a persistent R user environment and embed reticulate Python and DuckDB;
- dispatch complete R, Python, and SQL cells;
- queue genuine interactive input to worker fd 0 whether the session is evaluating or idle;
- emit structured state, output, display, plot, help, object, inspection, and completion events;
- cooperate with interrupt, restart, shutdown, and sandbox policy;
- retain private runtime and object-reference handles without exposing them to ordinary user code or sidecar clients.

The backend reports runtime facts and capabilities.
It does not decide MCP wording, response budgets, transcript prose, local API authorization, or file-retention policy.

### 6.3 Session generations

A logical session can exist without a worker after successful environment preparation and can outlive multiple worker incarnations.

```text
analysis / prepared       no worker yet
default / generation 1
default / generation 2   after restart
```

In-memory R, Python, SQL, debugger, and native-library state never crosses a generation boundary.
Declared requirements and workspace files may persist.
Evaluation IDs must remain unambiguous across generations.

## 7. Worker threading and event loop

R must be initialized and called from one owning thread.
The exact loop is backend-specific:

- the native backend may run an MCP Console dispatch loop around embedded R, with helper threads for IPC and high-priority control;
- the Ark backend uses Ark's R execution thread and Jupyter/kernel event machinery, with the adapter correlating messages to normalized requests and events.

Both backends must preserve these properties:

- one active top-level evaluation per session;
- complete cells are distinct from interactive input;
- typed inspection is serialized onto the R-owning thread unless a capability explicitly provides a safe alternative;
- interrupt and forced termination do not wait behind ordinary evaluation traffic;
- sidecar subscribers and output consumers cannot block runtime progress.

Do not make the session manager depend on whether the backend's idle loop is a custom command loop or Ark's kernel loop.

## 8. Runtime adapter protocol

The supervisor–backend boundary is private, versioned, and stricter than the MCP schema.
A native backend may use JSON Lines over dedicated pipes, with large binary data in files or a separate binary path.
An Ark backend may use Jupyter channels and custom comms internally, but its adapter must translate them into the same normalized commands and events described here.

### 8.1 Logical channels

```text
evaluation:    supervisor -> backend
events:        backend -> supervisor
inspection:    supervisor <-> backend
stdin:         supervisor -> session worker
control:       supervisor -> backend or OS runtime interrupt
raw stdout:    worker and descendants -> supervisor when applicable
raw stderr:    worker and descendants -> supervisor when applicable
```

These are semantic channels, not necessarily separate file descriptors.
The control path must remain usable while the R-owning thread is blocked in evaluation.

Stdin is owned by the worker generation, not an evaluation.
Unread bytes remain queued after an evaluation completes and may be consumed by a background job or later evaluation.
Restart, close, or worker failure discards them with that generation.
Supported console callbacks emit evaluation-scoped `InputRequested` and `InputReceived` events; direct fd-0 reads emit neither.

### 8.2 Representative commands

```rust
enum WorkerCommand {
    Initialize {
        protocol_version: u32,
        session: SessionId,
        generation: u64,
        workspace: PathBuf,
        environment: ResolvedEnvironment,
    },
    EvaluateCell {
        evaluation_id: EvaluationId,
        language: Language,
        source: String,
        label: Option<String>,
    },
    QueueInput {
        text: String,
    },
    PrepareShutdown {
        reason: ShutdownReason,
    },
}
```

Evaluation source and interactive input never share a generic input queue.

### 8.3 Representative events

```rust
enum WorkerEvent {
    Ready { capabilities: Capabilities },
    EvaluationStarted { evaluation_id: EvaluationId, language: Language },
    OutputText { evaluation_id: EvaluationId, stream: Stream, bytes: Vec<u8> },
    DisplayValue { evaluation_id: EvaluationId, value: DisplayValue },
    ArtifactCreated { evaluation_id: EvaluationId, artifact: Artifact },
    InputRequested {
        evaluation_id: EvaluationId,
        input_request_id: InputRequestId,
        origin: InputOrigin,
        prompt: String,
        echo: bool,
    },
    InputReceived { evaluation_id: EvaluationId, input_request_id: InputRequestId },
    EvaluationFinished { evaluation_id: EvaluationId, outcome: EvaluationOutcome },
    InterruptAcknowledged { evaluation_id: Option<EvaluationId> },
    SessionEnded { reason: EndReason, message: Option<String> },
    ProtocolWarning { message: String },
}
```

`DisplayValue` is a bounded or structured description, not an arbitrary serialized language object.

### 8.4 Completion rules

An evaluation is complete only after `EvaluationFinished` or worker termination.
A quiet pipe, a familiar prompt, or a short settling delay is never sufficient evidence.

`InputRequested` is provisional for a short grace window.
If its matching `InputReceived` arrives in that window, the supervisor continues waiting because the runtime read already succeeded.
Otherwise the prompt and `[input]` may return before the MCP wait deadline.
This grace applies only to input state; it is never evidence that evaluation completed.

`InputReceived` pairs with the runtime request ID.
It does not acknowledge a `QueueInput` command, identify which payload supplied the bytes, or report direct fd-0 reads.

The grace window is a latency heuristic, not proof that the read remains blocked.
From `InputRequested` alone, the supervisor cannot distinguish incomplete input from a delayed receipt.
Guaranteeing suppression would require waiting until the MCP deadline or adding another worker event that proves the read is still blocked; the initial design accepts an occasional extra `[input]` boundary instead.

Move exact message schemas and ordering constraints into `docs/WORKER_PROTOCOL.md` once implementation begins.

## 9. Runtime dispatch and stack semantics

The selected backend dispatches each accepted cell by language, directly or through an adapter extension:

```rust
match cell.language {
    Language::R => r.evaluate_cell(cell),
    Language::Python => python.evaluate_cell(cell),
    Language::Sql => sql.evaluate_cell(cell),
}
```

There is no universal interpreted R call such as `.mcp_console_eval(id)` around every language.

The stack contract is intentionally asymmetric:

| Input  | Required semantic boundary       | Console-owned interpreted R frame |
| ------ | -------------------------------- | --------------------------------: |
| R      | native top-level R cell behavior |                                no |
| Python | private reticulate cell boundary |   minimal and truthful if present |
| SQL    | private DBI/DuckDB boundary      |   minimal and truthful if present |
| stdin  | append to worker input stream    |            no new top-level frame |

This asymmetry follows the actual implementation boundaries and should be documented rather than concealed.

## 10. R runtime semantics

### 10.1 Initialization

Initialize R once per worker generation.
A native backend uses the same class of frontend APIs used by Ark and `mcp-repl`; an Ark backend delegates startup to Ark and proves equivalent externally observable behavior.

The backend should:

- discover and configure `R_HOME` and library paths;
- initialize R as interactive but disable automatic workspace restore/save;
- honor or deliberately configure startup files and repositories;
- install `WriteConsoleEx`, `ReadConsole`, message, busy, callback, and shutdown hooks as appropriate per platform;
- initialize graphics, help, object-inspection, and debugger integration required by its declared capabilities;
- create a persistent user environment;
- create a private environment for console-owned state and bridge functions;
- process platform event hooks required by embedded R.

Cross-platform startup behavior and mature IDE sidebands are major reasons to evaluate Ark rather than beginning from raw `libR` examples.
A native backend should still build from `mcp-repl`/Ark patterns rather than re-derive them casually.

### 10.2 Complete-cell evaluation

The native backend uses R's public `R_ReplDLLinit()` and `R_ReplDLLdo1()` pseudo-console API.
For each R cell it:

1. establishes a fresh DLL parser and top-level jump boundary without resetting the persistent global environment;
2. feeds complete-cell source only when the outer DLL REPL requests primary or continuation input;
3. lets R parse and evaluate each top-level expression, update `.Last.value`, auto-print visible values, print warnings, and invoke task callbacks;
4. treats source EOF after a primary status as completion and source EOF after a continuation status as incomplete input;
5. emits conditions, errors, artifacts, and completion under the evaluation ID;
6. restores the per-cell source queue after completion or error without claiming that queued stdin was consumed.

The implementation should retain source references and a synthetic source name when the DLL embedding API can support them without replacing R's native top-level loop.

Earlier expressions in a multi-expression cell may have changed state before a later error.
Do not pretend the whole cell is transactional.

Do not make `source()`, `withAutoprint()`, or `eval(str2expression(...))` the fundamental evaluator.
They can add interpreted helper frames and make source, visibility, and error behavior harder to control.
The target is a native frontend evaluator equivalent in spirit to what a real console does.

### 10.3 R call-stack contract

Because R's DLL REPL evaluates the parsed expressions directly, ordinary R stack introspection should not contain a console-owned interpreted frame.

For example:

```r
f <- function() sys.calls()
f()
```

should show user R calls equivalent to `f()`, not an outer `.mcp_console_eval(...)`, `source(...)`, or `withAutoprint(...)` call.

Native error-catching functions and C/Rust frames are not represented by `sys.calls()` and are acceptable.

This contract requires direct integration tests because subtle evaluation helpers can change stack and traceback behavior.

### 10.4 `ReadConsole` and interactive input

The native DLL-REPL adapter uses `ReadConsole` for two separately owned queues:

- primary and continuation reads consume source from the active complete cell;
- reads after top-level evaluation begins consume genuine runtime input.

Runtime input includes:

- `readline()`;
- `browser()` and `recover()`;
- package code that reads from the R console;
- startup code that deliberately requests input, if supported.

When called during an active evaluation, the callback:

1. allocates an `InputRequestId`;
2. emits `InputRequested` with prompt and origin;
3. blocks while reading fd 0 through one newline or the supplied callback buffer;
4. emits the matching `InputReceived` after a nonempty read succeeds;
5. returns that chunk to R and leaves additional bytes in the pipe for later console or direct reads.

The supervisor may queue fd-0 bytes before `InputRequested`.
The receipt describes the callback read, not consumption of a particular queued payload.

The callback uses a Busy-based evaluation latch rather than prompt comparison to select the queue.
The cell-source queue and interactive-input queue must never be merged.

## 11. Python runtime through reticulate

### 11.1 Initialization and persistent state

Python is initialized lazily after applicable requirements have been declared.
Reticulate owns one interpreter and persistent Python `__main__` module inside R.

R code accesses Python objects through reticulate's `py` object.
Python accesses R through reticulate's `r` object.
Do not create another global namespace protocol unless a concrete interoperability gap requires it.

### 11.2 Cell evaluator

Do not use `py_eval()` for general cells; it accepts expressions, not assignments and statements.
Do not use a generic line-fed `repl_python(input = ...)` loop as the fundamental cell transport, because nested `input()` or debugger reads must not consume remaining source lines.

Install a small Python cell executor that:

1. compiles the complete source using a synthetic filename such as `<mcp-console:python:g1:e18>`;
2. rejects incomplete parser input;
3. executes statements in persistent `__main__.__dict__`;
4. evaluates and displays a final expression when present;
5. preserves Python exceptions and traceback locations;
6. routes standard input and debugger reads through the active MCP Console input bridge;
7. leaves imports and globals persistent.

A common implementation is to parse the cell with Python's `ast` module, execute all but a final `Expr`, then evaluate and display that final expression.
The exact helper belongs in `runtime/python/cell.py` and must be exercised through the public MCP interface.

### 11.3 R bridge and stack behavior

Reticulate's supported entry points are R APIs.
A practical v1 can call one private R helper, for example conceptually:

```r
.mcp_console_private$eval_python(evaluation_id)
```

The source should be stored out of band under the evaluation ID rather than interpolated into the R call.
The helper invokes the installed Python cell executor and returns only runtime-neutral results.

While Python is active, the R call stack therefore contains one console bridge plus reticulate frames.
If Python calls an R function, raw `sys.calls()` in that callback may show those outer frames.
This is truthful and acceptable.

Curated diagnostics may collapse known internal frames for readability, but raw R introspection must not be falsified.
A later supported native reticulate entry point could remove the console-owned R helper without changing the MCP interface.

### 11.4 Python stdin and debuggers

Install a Python `sys.stdin` or `builtins.input` bridge that uses the same fd-0 stream and paired `InputRequested`/`InputReceived` events as R.
It should support at least ordinary `input()` and line-oriented debugger commands.

Do not assume that R's `ReadConsole` automatically provides correct Python stdin semantics.
Verify `input()`, `pdb`, nested R callbacks, interruption, and EOF behavior in an implementation spike.

## 12. SQL runtime through DuckDB and DBI

### 12.1 Initial ownership

The initial SQL implementation uses the DuckDB R package and DBI in the same worker process.
It does not launch the DuckDB CLI and does not implement a new SQL engine.

Create one connection per worker generation, conceptually:

```r
DBI::dbConnect(
  duckdb::duckdb(
    dbdir = ":memory:",
    environment_scan = TRUE,
    shared_home = FALSE
  )
)
```

Pin exact supported DuckDB arguments and versions in code and tests.
Direct DuckDB storage, extension, and secret paths into session-controlled locations rather than ambient user state.

### 12.2 SQL bridge and R stack

A practical v1 calls one private R helper, conceptually:

```r
.mcp_console_private$eval_sql(evaluation_id)
```

The helper retrieves the SQL source from private state and calls DBI against the persistent connection.

While the SQL evaluation is active, an R callback or debugger may observe:

- the private SQL bridge frame;
- DBI generic and method frames;
- DuckDB R backend frames.

SQL itself has no `sys.calls()` equivalent tied to R.
After the query returns, these R frames are gone.
This is an explicit cost of using the R/DBI integration and should be covered by stack-behavior tests.

Do not place the entire SQL source literal in the helper call, where it could make `sys.calls()` and diagnostics unwieldy.

### 12.3 Why use the R connection first

The R integration provides the shortest path to useful shared state:

- DuckDB catalog state persists in the worker;
- R data frames can be discovered through environment scanning;
- `duckdb_register()` can expose R data frames without copying;
- `duckdb_register_arrow()` can expose Arrow-backed sources;
- Python objects can initially cross reticulate conversion or an Arrow bridge.

The adapter must deliberately arrange the evaluation environment used for environment scanning and test name precedence, rebinding, and object lifetime.
Do not rely on accidental internal call frames.

### 12.4 Query execution and bounded fetching

The SQL adapter owns:

- execution of complete SQL cells;
- result-set versus statement behavior;
- bounded fetching and previews;
- affected-row or statement summaries;
- reliable result cleanup;
- SQL errors and source locations;
- relation registration helpers;
- cooperative interruption where the R backend permits it.

Use `dbSendQuery()`/`dbSendStatement()` plus bounded `dbFetch(n = ...)`, or DBI Arrow/record-batch APIs.
Never call `dbGetQuery()` on arbitrary agent SQL when it can collect an unbounded result into R.

If backend-specific statement metadata is required, isolate all unstable DuckDB-R access behind one adapter and pin a compatibility test.

### 12.5 Relation visibility

V1 rules:

1. persistent DuckDB tables and views use ordinary SQL names;
2. when no catalog relation exists, environment scanning may resolve an R data frame from the intended user environment;
3. a runtime helper such as `sql_register(name, object)` explicitly publishes a stable relation;
4. R data frames use `duckdb_register()` where appropriate;
5. Arrow-backed R objects use `duckdb_register_arrow()`;
6. Python data frames initially pass through reticulate conversion or Arrow registration;
7. automatic scanning of all Python globals is deferred.

Document and test precedence between catalog relations, explicit registrations, and scanned R variables.

### 12.6 DuckDB CLI relationship

The CLI is a behavioral reference only.
Borrow ideas such as:

- bounded duckbox-like previews;
- SQL-native discovery workflows;
- useful progress and interruption behavior;
- perhaps a previous-result relation in a later version.

Do not expose dot commands, terminal modes, line-oriented continuation, shell escapes, mutable output redirection, or the CLI executable as the SQL transport.

### 12.7 Possible future native DuckDB ownership

Rust could later own DuckDB directly through its C or Rust API.
That would remove SQL's R bridge and improve access to progress, interruption, data chunks, and result metadata.

It would also lose automatic R environment scanning and require an explicit Arrow or table-function bridge with careful R object lifetime management.
Adopt native ownership only after measurements show that the R/DBI boundary is the limiting factor.

## 13. Runtime helper API

Prefer in-language helpers over more MCP tools.
Possible R helpers include:

```r
sql_query(sql) # deliberately collect a SQL result into R
sql_exec(sql) # execute a SQL statement
sql_register(name, x) # publish a relation
sql_unregister(name)
sql_tables()
console_transcript()
console_artifact_path(name)
```

Python can call them through reticulate's `r` object.
These helpers are a runtime API and should eventually receive focused documentation and compatibility tests.

## 14. Dependency architecture

Each logical session owns a dependency manifest.
The implemented Python manifest records package requirements, Python-version constraints, and an optional `exclude_newer` value.

The supervisor owns the host resolver for both initial preparation and supported runtime additions.
The arbitrary-code worker remains network denied, but it can send a structured requirement manifest and worker-controlled `UV_*` settings to the supervisor for managed resolution.
The resolver deliberately runs outside the worker sandbox, where it can access package indexes, write normal host caches, and execute package build backends.
It receives no submitted cell source; evaluated R code or a package load hook can trigger the request through `py_require()`.
The worker's `UV_*` settings, except `UV_OFFLINE`, apply only to the current resolution and are not retained or replayed.

### 14.1 Python

Use reticulate's managed environment and `py_require()` semantics where practical.
Requirements must be finalized before first Python initialization whenever they constrain interpreter choice.
After initialization, v1 permits additive requirements only.

R packages may declare requirements by calling `py_require()` from load hooks.
For a server-managed worker, seed reticulate's manifest and intercept its internal `uv_get_or_create_env` and `resolve_python_version` bindings rather than wrapping `py_require()`.
This retains originating-package attribution, manifest history, and reticulate's native validation and activation behavior within the live R process.
The environment binding reports the physical resolver manifest and the logical manifest to retain if accepted, together with the worker's `UV_*` settings except `UV_OFFLINE`, and waits for the host resolver to return an interpreter path.
The version binding reports only the requested constraints and `uv` settings, then waits for the host resolver to return the selected version without creating a candidate environment.

Before initialization, a lazy `py_require()` declaration remains worker-owned until Python initializes or explicit preparation materializes it.
Losing the worker before either boundary can therefore lose that declaration.
After initialization, reticulate resolves late additions with the exact active Python patch version while leaving the logical `py_require()` Python constraints unchanged, and each host result remains only a candidate until reticulate accepts it.
Reticulate must verify that it uses the exact live `libpython`, run the candidate's `activate_this.py`, swap its Python configuration, and update its manifest.
The worker then reports the normalized logical manifest through a standalone `python_activated` event.
The supervisor immediately accepts the matching candidate, or its prior environment when that manifest did not change.
Acceptance and the restart-generation check are atomic; a receipt still pending when restart claims the generation is discarded with that worker.
The `completed` and `python_prepared` operation receipts carry no Python manifest.
The Python interpreter and its live objects remain in place throughout a successful activation.
Reticulate owns the live manifest, while the supervisor owns the last reported activation between worker generations.

Only newly added package requirements fit the v1 late-layering contract.
Removals and constraint changes remain unsupported after initialization.
Idle `session prepare` uses this path for compatible Python additions; mixed R and Python preparation coordinates it with the R library update as one public operation.

### 14.2 R

The implemented implicit session uses `ir` for explicit R requirements.
The supervisor requires `ir --version` from `PATH` to report 0.4.0 or later, then runs `ir run` outside the worker sandbox with the same Rscript selection as the worker and one `--with` argument per exact requirement.
It validates `ir`'s returned library and prepends it to inherited `R_LIBS` before each worker generation initializes R.
The prepared library persists across explicit restart and crash replacement.
While the built-in worker is idle, the supervisor can resolve the complete additive R requirement set and ask a fixed private R bridge to prepend the candidate to `.libPaths()` and remove the previous managed entry.
Each candidate contains the complete retained R requirement set, so replacing the previous managed entry keeps live package lookup consistent with restart and crash replacement instead of accumulating stale candidates.
The bridge preserves the other live library paths and in-memory state.
Only after the worker confirms the normalized library path does the supervisor retain that library for later generations.
The supervisor sets `IR_NO_LOCAL_SOURCES` for every invocation.
`ir` owns package-reference parsing and prevents direct or transitive local package installation before materialization.
This does not sandbox build code from accepted repository or remote packages, and `ir` may reuse an already materialized library.
The exact package-reference grammar belongs in a later `docs/DEPENDENCIES.md`.

### 14.3 Atomic public behavior

Before worker startup, an explicit `prepare` action remains atomic:

1. merge the requested additions with the current manifest;
2. resolve and prepare the candidate environment outside the arbitrary-code worker;
3. commit the R library, Python interpreter, and manifests only after every requested resolution succeeds.

After startup, live preparation has one immediate Python commitment boundary:

1. merge additions into the complete retained R and Python requirement sets;
2. resolve each new candidate through its language-specific host resolver;
3. apply the R candidate through the private `.libPaths()` bridge and compatible Python additions through reticulate;
4. retain a Python environment immediately when the worker reports `python_activated` or successful explicit materialization;
5. retain the R and DuckDB configurations only after every requested live change succeeds and the worker generation is still current.

A later R failure does not roll back a Python environment already accepted during the same mixed operation.
If a synchronized failure may have partially changed the live worker, the supervisor keeps it available for evaluation but rejects new requirement additions until a successful explicit restart.
This gives the caller an opportunity to save in-memory state before replacing the worker.
Transport or protocol failures still stop a worker whose usability is unknown.
Preparation during evaluation remains rejected.

A stopped worker cannot receive live environment updates.
The current implicit-session implementation returns `[restart required]` for new preparation additions in that state and does not stage them for the next generation.
Its `restart` action reuses the retained R library and accepts only optional Python additions.

An explicit `restart` with requirements is atomic until the worker replacement boundary:

1. merge additions into the complete retained manifest;
2. resolve the candidate outside the arbitrary-code worker while the current worker remains intact;
3. leave the current worker, manifest, and interpreter unchanged if resolution fails;
4. after resolution succeeds, commit the candidate and replace the worker.

The ordered dispatcher still validates semantic events queued before the old generation's retirement marker.
If restart reuses the retained environment, successful old-generation preparation results and managed-Python activations commit before replacement.
If restart has already committed a newly resolved environment, those successful old-generation environment commits are discarded so they cannot overwrite the replacement, and a discarded preparation reports restart cancellation.
Worker-reported preparation failures and protocol, resolver, or environment-commit failures remain failures.

During a server-managed worker evaluation, runtime layering follows activation events:

1. reticulate updates its live manifest and requests host resolution when it needs an environment;
2. the supervisor retains each resolved result as a candidate;
3. reticulate performs its exact-`libpython` check, activation, and configuration swap when Python is already initialized;
4. the worker reports the accepted logical manifest through `python_activated`;
5. the supervisor immediately retains the matching candidate or its unchanged prior environment.

A later normal-language, infrastructure, or protocol failure does not roll back an already reported activation.
Candidates without a matching activation event are discarded when the operation ends.

## 15. Output architecture

### 15.1 Sources

Output can arrive through:

- R `WriteConsoleEx` and message hooks;
- Python stdout/stderr and display hooks;
- SQL result/display events;
- graphics or artifact callbacks;
- raw worker or child-process file descriptors.

Managed hooks provide evaluation attribution.
Raw pipes are the fallback for native libraries and descendants that bypass language hooks.

### 15.2 Per-evaluation spool

Each evaluation owns an append-only output spool.
Preserve complete explicit stream output when practical, including output omitted from MCP replies.

The supervisor owns a reply cursor per active evaluation.
A response snapshots output from the cursor to the current end, applies a global budget, writes a truncation marker and path if needed, then advances the cursor to that end.

The cursor advances past omitted bytes.
Future polls return new activity rather than requiring textual pagination through an old flood.

### 15.3 Ordering

Managed events and raw file-descriptor output can race.
Define one visible output timeline with monotonic sequence numbers assigned at ingestion.
Do not claim impossible byte-perfect ordering across independent OS streams.

If a PTY merges stdout and stderr, preserve the merged truth rather than inventing stream identity.

### 15.4 Structural previews

Recognized values should become a runtime-neutral `DisplayValue` before final rendering:

```rust
enum DisplayValue {
    Text(String),
    Table(TablePreview),
    Array(ArrayPreview),
    Mapping(MappingPreview),
    Artifact(Artifact),
    Summary(ObjectSummary),
}
```

A preview must already be bounded in rows, columns, elements, and cell widths.
The supervisor then applies the final response-byte budget.

Unknown classes may invoke arbitrary print methods.
Capture that output to the spool and truncate it normally rather than pretending it is safely previewable.

### 15.5 SQL previews

Fetch only `preview_rows + 1` or enough record batches to determine that additional rows exist.
Do not count an entire arbitrary relation solely to print an exact total.
Report exact dimensions only when cheaply available from metadata or a deliberate count.

### 15.6 Plots and artifacts

Save plots and binary artifacts beneath the session directory and emit relative paths.
The MCP response remains text-only.
The transcript links to the same files.

## 16. Session records and transcript

### 16.1 Directory layout

The implemented slice creates one run-specific directory at `.agents/console/sessions/<UTC-first-use>-<pid>/` when the first ordinary `send` call arrives, with `transcript.md`, `transcript.qmd`, `artifacts/`, and `internal/events.jsonl` beneath it.
Initialization, tool listing, unknown tool calls, and an unused server process create no record.
On Unix, it creates the record directories with mode `0700` and journal, document, and artifact files with mode `0600`.
Its journal records MCP tool calls and results plus image artifacts when worker frames arrive; it does not yet implement the complete evaluation-event vocabulary below.
Recording is optional: the first journal or artifact failure disables it for that server process, emits one diagnostic to standard error, and does not change console results or worker lifecycle.
Failure of either generated document disables both projections while the journal and artifacts continue.
An existing journal may therefore end with the last successfully flushed event.
The named-session design will replace that temporary run identity with the planned layout below:

```text
.agents/console/sessions/default/
├── transcript.md
├── transcript.qmd
├── environment.json
├── outputs/
│   ├── g1-e0017.log
│   └── g1-e0018.log
├── artifacts/
│   └── g1-e0021-plot-1.png
└── internal/
    └── events.jsonl
```

Use sanitized or encoded path components rather than trusting a logical session name as a path.

### 16.2 Internal journal

The internal journal is the authoritative durable event record for reconstruction and diagnostics.
JSONL is a reasonable v1 format because it is append-friendly and can discard a partial final line after a crash.

It may record granular events such as:

```text
session_started
evaluation_accepted
evaluation_started
stream_appended
artifact_created
input_requested
input_supplied
evaluation_finished
worker_stopped
generation_started
```

This file is not the normal agent-facing artifact and need not expose Rust's internal serialization directly.
Give it an explicit private schema version.

### 16.3 Generated documents

`transcript.md` is the compact human- and agent-facing projection.
The current server appends once for each recorded session, call, artifact, or result event.
The planned evaluation-event vocabulary adds explicit completion, error, interruption, input-request, worker-stop, and generation-change boundaries.

It contains complete source, labels, bounded output, errors, supplied input, and relative artifact paths without redaction.
It is generated and formatted with Yamark without applying embedded formatters to submitted code or result content.

`transcript.qmd` contains source from calls with exactly one submitted R, Python, or SQL field in call order, including source from a call that later fails option validation or preparation.
It is regenerated from incremental source and requirement state when those inputs change, without rereading prior result events from the journal.
Its `ir` front matter records those declarations without selecting a Python version, so reticulate chooses its default managed Python when users run `ir render`.
Yamark formatting does not rewrite submitted cells, and rendering executes the captured client-authored action stream in a fresh environment.
The direction is to reproduce the analysis represented by the Markdown transcript, while accepting that runtime state, artifacts, and SQL connections are not fully reconstructable yet.

Do not continuously embed unlimited output.
Refer to full output sidecars when excerpts are insufficient.

Do not let agents edit the live generated documents in place.
Refined notebooks, reports, and scripts are separate files created from the transcript, code-cell projection, and runtime artifacts.

## 17. Session manager and concurrency

The supervisor owns the state machine:

```text
absent -> preparing -> prepared
absent/prepared -> starting -> idle -> running
                                      running -> input_required -> running
                                      running -> idle
                                      running -> stopped
                                      stopped -> starting   on restart
prepared/stopped -> absent            on close
```

One session accepts one active evaluation.
Independent parallel work uses separate named sessions and therefore separate worker processes.

Multiple MCP poll requests may wait on the same evaluation, but output cursor semantics must be defined so one waiter cannot cause another to receive an incoherent replay.
The simplest v1 policy is one active consumer/waiter per session; if multiple waiters are allowed, give each its own observation cursor.

Do not automatically evict local stdio sessions in v1 unless measured resource pressure requires it.

## 18. Interrupt, restart, and failure

### 18.1 Interrupt

Interrupt is cooperative and runtime-aware.
It must bypass the ordinary evaluation queue.
The implementation may combine:

- a helper thread setting R interrupt state;
- an OS signal where safe;
- Python interrupt signaling;
- DuckDB connection interruption where exposed.

After interruption, the worker reports whether it recovered to idle.
If cooperative recovery fails, report the failure before starting a fresh generation.

### 18.2 Restart

Restart terminates the worker, increments the generation, and starts a fresh worker with the retained dependency manifest and workspace policy.
It destroys all in-memory state.

### 18.3 Crash

A segfault, abort, OOM kill, or unrecoverable embedded-runtime failure stops the current generation and fails the active evaluation.
Preserve the transcript and output produced before death, and record the crash.
The MCP server remains available; the next evaluation starts a fresh generation without implying state continuity.

## 19. Sandbox and security

MCP Console provides shell-class capability.
Enforce policy around the entire worker process:

- read and write roots;
- network access;
- subprocess inheritance;
- environment and secret forwarding;
- CPU, memory, process-count, file-size, and output limits;
- extension and native-library loading consequences;
- package resolver access.

The supervisor remains trusted and should have a small dependency surface.
Worker descendants must inherit restrictions.

Where Codex provides per-call sandbox metadata, the supervisor may derive worker policy from it.
Missing or malformed inherited policy must fail closed.
Platform details belong in a future `docs/SANDBOX.md`.

## 20. MCP implementation

Use the official Rust MCP SDK (`rmcp`) unless a concrete gap appears.
Keep it behind a thin adapter:

```text
mcp/
  deserialize and validate public arguments
  call session services
  translate results into bounded text and isError

session/
  own product state and semantics

worker/ and runtime/
  own process and interpreter mechanics
```

The public MCP protocol supports richer content, but v1 deliberately uses plain text plus workspace files because that is the most predictable contract for the target client.

## 21. Error model

### Tool errors (`isError: true`)

- invalid mode combinations;
- code sent while busy;
- missing session for poll/control;
- dependency preparation failure;
- worker startup or protocol failure;
- sandbox setup or authorization failure.

### Language outcomes (`isError: false`)

- R parse errors and conditions;
- Python exceptions;
- SQL errors;
- recoverable interrupts.

The session returns to idle only if the runtime reports successful recovery.

### Fatal runtime failures

Worker process death produces a stopped session and an infrastructure diagnostic.
It is never formatted as an ordinary language error.

## 22. Testing strategy

### 22.1 Public integration tests

Build the binary, launch it as an MCP stdio server, and test real tool calls.
Required scenarios include:

- minimal R, Python, and SQL cells;
- persistent state across alternating languages;
- R-to-Python and Python-to-R access;
- R data frame queried in SQL;
- lazy session creation and named-session isolation;
- code rejected while busy;
- wait expiry without cancellation;
- R `readline()` and `browser()`;
- Python `input()` and debugger input;
- interrupt, restart, close, and crash behavior;
- additive requirements;
- bounded stdout/stderr and sidecars;
- bounded R, Python, and SQL values;
- plot paths;
- generated-document recovery;
- sandbox restrictions.

### 22.2 Stack-semantics tests

Explicitly test:

- R `sys.calls()` does not include a console-owned wrapper for an R cell;
- R tracebacks retain useful synthetic source references;
- Python-to-R callbacks show the documented bridge boundary;
- SQL-to-R callbacks, where possible, show the documented DBI boundary;
- source text is not duplicated into internal stack calls.

### 22.3 Fake-worker tests

Use a deterministic fake worker for races and malformed sequences:

- output before ready;
- input request after partial output;
- output flood between polls;
- interrupt during input;
- malformed event;
- crash with trailing bytes;
- cancellation races;
- partial internal journal records.

### 22.4 Snapshot discipline

Avoid snapshots tied to terminal width, ANSI color, absolute temporary paths, package download progress, nondeterministic ordering, or exact internal traceback frames across runtime patch versions.

## 23. Implementation sequence

### Milestone 0: contract and fake backend

- implement the `rmcp` stdio server;
- expose the two draft schemas;
- implement validation, session state, normalized runtime capabilities, and events;
- build a deterministic fake backend and end-to-end harness;
- keep MCP, output, transcript, and local API code independent of Ark/Jupyter and `harp`/`libr` types.

Exit: all public states, lifecycle actions, and basic sidecar events work without real R.

### Milestone 1: remaining runtime backend evaluation

The R-only Ark and native paths have been implemented and compared.
The native DLL-REPL worker was selected for the current text R slice.
Complete the comparison for the capabilities that may change the full-runtime decision:

- interrupt and restart behavior;
- plot and help publication;
- Python and SQL dispatch with correct source and error attribution;
- object inventory and an independent viewer fetching bounded rows/columns from a large live data frame;
- busy-runtime behavior and snapshot materialization;
- current packaging, startup, sandbox, dependency, and version-maintenance costs.

Record the result in an ADR and remove the losing backend from the critical path unless it remains useful as a test adapter.

Exit: one backend is selected with evidence against the criteria in `RUNTIME_BACKEND.md`.

### Milestone 2: extend the persistent R worker

- retain the implemented structured cell and exact-input behavior;
- complete the selected backend's lifecycle and capability adapter;
- implement inspection, interrupt, and crash reporting;
- validate R stack and source semantics across supported platforms;
- establish pinned compatibility tests for Ark/comm or `harp`/`libr` dependencies.

Exit: persistent R, visible values, `readline()`, `browser()`, large live-table viewport access, oversized output, and restart work through public interfaces.

### Milestone 3: output and transcript

- add output spools and reply cursors;
- add structural previews;
- extend the internal journal and generated documents with evaluation-level events and bounded output references;
- add plot/artifact files and full-resolution viewer delivery.

Exit: no tested reply exceeds its configured budget and the Markdown transcript reconstructs useful session history.

### Milestone 4: reticulate Python

- prepare Python requirements before initialization;
- install persistent Python cell and stdin bridges;
- support final-expression display and tracebacks;
- test Python input, debugger behavior, R callbacks, and stack contract.

Exit: alternating R and Python cells share state in one worker.

### Milestone 5: DuckDB SQL

- initialize persistent DuckDB through R/DBI;
- add private SQL bridge and bounded result fetching;
- enable and test R environment scanning;
- add explicit R/Arrow registration and Python conversion path;
- test SQL stack boundary and output limits.

Exit: data loaded in Python can be converted or registered, queried in SQL, and consumed from R or Python.

### Milestone 6: sidecar viewer and data explorer

- implement protected process discovery and the process-scoped local service;
- add snapshots plus bounded resumable event subscriptions;
- implement object handles, live table views, immutable snapshots, plots, and viewer capability negotiation;
- keep arbitrary external code on the attributed primary evaluation path.

Exit: a human can observe the agent, inspect a large live table by viewport, view plots at full resolution, and continue browsing a snapshot while the agent computes.

### Milestone 7: environment manager

- add additive R/Python manifests;
- add resolution caches and provenance;
- separate resolver network policy from worker policy.

Exit: session requirements are explicit, persistent across restart, and applied atomically.

### Milestone 8: sandbox and platform hardening

- implement macOS, Linux, and Windows policies;
- inherit host policy where supported;
- add resource quotas and escape tests;
- harden private IPC, Jupyter endpoints when used, local API, and session paths.

Exit: supported-platform security and resource tests pass in CI.

## 24. Required implementation spikes

1. **Ark-backed end-to-end path:** the text-R path is complete.
   Extend it only as needed to evaluate interrupt, plots/help, Python/SQL, and inspection through normalized events.
2. **Independent Ark Data Explorer client:** from a separate process, retain and browse a large R data frame by bounded row/column requests; determine which comm/backend APIs are reusable and stable.
3. **Native backend comparison:** cell and stdin behavior are complete.
   Implement enough plot/help, interrupt, and live-table behavior to compare the remaining full-runtime requirements.
4. **Polyglot dispatch:** verify Python and SQL cells under both candidates, including tracebacks, source names, input, interruption, and R stack boundaries.
5. **Remaining R semantics:** add submitted-source references and validate `recover()` and interrupt recovery under the selected path; retain the existing stack, traceback, error, visible-value, and `browser()` coverage.
6. **Busy inspection:** characterize Ark comm and native inspection behavior while R is running, waiting for input, or stopped; do not assume Jupyter channels imply concurrent R execution.
7. **Live versus snapshot views:** measure viewport latency, materialization cost, staleness, invalidation, and memory for representative large frames and Arrow/DuckDB sources.
8. **Packaging and maintenance:** measure startup time, binary/wheel size, process count, sandbox requirements, version skew, and whether changes require an Ark fork.
9. **Python cell evaluator:** compare a custom AST helper with reticulate internals; verify final-expression display without line-queue ambiguity.
10. **Python stdin:** verify `input()`, `pdb`, EOF, interrupt, and callbacks into R.
11. **DuckDB environment scan:** verify the exact R environment used, name precedence, rebinding, and registration lifetimes.
12. **DuckDB bounded fetch:** compare bounded DBI, Arrow, and record-batch paths and confirm interruption behavior.
13. **Output ordering:** define the merge contract for managed events and raw process streams.
14. **Transcript recovery:** define recovery for a partial final Markdown append or interrupted QMD replacement without mutating the authoritative journal.
15. **Cancellation:** define exact mapping between MCP cancellation, initiating calls, later poll waiters, and runtime interrupts.

Resolve the remaining portions of spikes 1–8 before treating the full backend substrate as settled.
Record the decision as a short ADR and update `AGENTS.md`, `README.md`, and this document rather than leaving contradictory alternatives in place.

## 25. External references

- MCP tools specification: <https://modelcontextprotocol.io/specification/2025-11-25/server/tools>
- Official Rust MCP SDK: <https://github.com/modelcontextprotocol/rust-sdk>
- `mcp-repl`: <https://github.com/posit-dev/mcp-repl>
- Ark: <https://github.com/posit-dev/ark>
- Runtime backend decision: [`RUNTIME_BACKEND.md`](RUNTIME_BACKEND.md)
- Reticulate: <https://rstudio.github.io/reticulate/>
- `py_run_string()`: <https://rstudio.github.io/reticulate/reference/py_run.html>
- `repl_python()`: <https://rstudio.github.io/reticulate/reference/repl_python.html>
- `py_require()`: <https://rstudio.github.io/reticulate/reference/py_require.html>
- DuckDB R client: <https://duckdb.org/docs/current/clients/r>
- DuckDB R connection and environment scan: <https://r.duckdb.org/reference/duckdb.html>
- DuckDB data-frame registration: <https://r.duckdb.org/reference/duckdb_register.html>
- DBI query lifecycle: <https://dbi.r-dbi.org/reference/dbSendQuery.html>
- Quarto Markdown: <https://quarto.org/docs/authoring/markdown-basics.html>
