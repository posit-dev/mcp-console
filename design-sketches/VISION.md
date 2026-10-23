# MCP Console Vision

MCP Console is a persistent, sandboxed R, Python, and DuckDB SQL console for AI agents.
A console session hosts R, Python, and SQL in one process: R is the host runtime, Python is embedded through reticulate, and SQL is backed by a persistent DuckDB connection.
The agent can load data once, move among the three front ends, inspect and transform live state, and continue across tool calls.

Supporting Python-only and Python-with-SQL sessions is a future direction.
The R-hosted architecture and the rest of this design remain the current direction.

The product is a **console**, not a shell wrapper, a conventional line-oriented REPL, or a notebook kernel.
Normal work is submitted as complete R, Python, or SQL cells.
Real line-oriented input appears only when the running program requests it, including `readline()`, `input()`, `browser()`, and debuggers.

MCP Console also gives humans visibility into an agent's live computational work without injecting that visibility into model context.
A process-scoped local API supports observers, plot and transcript viewers, bounded live-object inspection, and point-in-time snapshots.
It is owned by the MCP server process rather than a persistent daemon.

## Why this should exist

A shell can launch R or Python, but a fresh process discards the objects, imports, packages, debugger state, and context established by the previous turn.
Separate language tools preserve some state but make interchange explicit and file-oriented.
A notebook preserves cells and outputs, but it is primarily a document abstraction and carries kernel, MIME, editing, and execution-order conventions that are unnecessary for a high-frequency agent tool.

MCP Console should make exploratory data work feel like a conversation with one live computational environment.
Used this way, the console acts as a computational workbench for exploratory data work.
The agent should choose the most useful language for each step without repeatedly reconstructing state or flooding its context with runtime details.
A human should be able to observe and inspect the work at full fidelity without turning every plot, table, or progress update into LLM tokens.

## Design goals

### 1. Cheap enough to keep globally enabled

The tool definitions, common calls, and ordinary replies must remain small.
A broadly useful capability loses much of its value when every conversation pays for a large schema or verbose response envelope.

### 2. One persistent environment with three first-class front ends

R, Python, and SQL should share one process, workspace, dependency context, and explicit object bridges.
The agent should be able to load data in Python, manipulate it in R, and query it in SQL without starting another process or serializing through user-managed files.

### 3. Native language behavior

Each frontend should behave like its own language rather than a string hidden inside another language's visible wrapper.
R cells should have top-level R evaluation semantics.
Python should preserve persistent `__main__`, language-native tracebacks, and final-expression display.
SQL should expose DuckDB diagnostics and persistent catalog state.
Necessary R/Python or R/SQL bridge frames should be minimal and truthful rather than disguised.

### 4. Cell-oriented evaluation with real console interactivity

Top-level code is submitted as a complete cell.
Incomplete parser input is an error rather than an invitation to continue a program over several MCP calls.
The session accepts exact, optionally multiline `stdin` text whether it is evaluating or idle.
Paired request and receipt events expose whether a supported runtime read remains unsatisfied without gating delivery.

### 5. Exact runtime state, never prompt inference

The server must know whether a session is preparing, starting, idle, running, waiting for input, stopped, or absent.
State transitions come from structured runtime events.
Prompts such as `>`, `...`, and `Browse[1]>` are output, not protocol.

### 6. Fast synchronous turns with bounded waiting

Most evaluations should complete in the initiating tool call.
A wait limit ends only that MCP request; it does not terminate the computation.
A later empty call waits for new output or completion.
Ordinary work should not require a polling ceremony, while long work remains observable and interruptible.

### 7. Useful output without context flooding

Small results appear directly.
Large values receive bounded structural previews before full stringification.
Tables are limited by rows, columns, cell width, and total reply size.
Explicit stdout and stderr are retained in bounded session spools, while each MCP reply contains only a bounded current excerpt.

### 8. Durable context outside the conversation

Each session maintains a generated `transcript.md` containing submitted code, labels, bounded output, errors, input interactions, origins, and artifact paths, plus a source-only `transcript.qmd` containing submitted code cells.
After context compaction, the agent can recover what happened using ordinary file tools.

The transcript is a chronological execution record, not a polished or necessarily reproducible notebook.
Refined `.qmd`, `.R`, `.py`, or `.ipynb` artifacts are created separately.
A granular event journal may support recovery internally, but it is not the default agent-facing artifact.

### 9. Persistent, explicit session environments

R and Python requirements are relatively infrequent session configuration, not modifiers on ordinary code cells.
The agent prepares additive requirements through the session-control tool.
Requirements survive runtime restarts, while R objects, Python objects, loaded imports, debugger state, and the in-memory DuckDB catalog do not.
Environment changes must be explicit and must never silently destroy live state.

### 10. Explicit and predictable interoperability

Language selection is encoded by the input key: `r`, `python`, or `sql`.
The server never guesses from source text.
R and Python use reticulate's object bridge.
SQL uses persistent DuckDB state, live R relation discovery where safe, and explicit registration for stable R, Python, or Arrow-backed relations.

### 11. Honest lifecycle and failure behavior

A session runs at most one top-level evaluation at a time.
Interrupt attempts to preserve state.
Restart deliberately starts a new runtime generation and loses in-memory state while retaining session requirements, workspace files, and transcript.
A worker crash fails the active evaluation and is recorded in the transcript.
The server remains available, and the next evaluation starts a fresh worker generation without implying that state survived.

### 12. Process-level safety

MCP Console is effectively an ergonomic shell.
Arbitrary R and Python can access files, native libraries, subprocesses, and networks when permitted.
Safety therefore belongs around the worker process and its descendants: filesystem policy, network policy, resource limits, secret isolation, and explicit host approval.

### 13. Human visibility without model-context cost

A user should be able to attach to a live server, see what an agent is running, follow bounded progress, browse the transcript, view original-resolution plots, and inspect supported objects without requiring those payloads to enter the conversation.

The visibility interface is process-scoped.
It starts and stops with the MCP server, uses protected local discovery and transport, and never silently creates a detached service.
Multiple live server instances remain distinct even when they contain sessions with the same name.

### 14. A safe distinction between observation, inspection, and control

Passive observation must not enter the runtime.
Structured inspection may execute bounded internal helpers on the runtime-owning thread, but callers supply typed requests rather than arbitrary source.
Arbitrary external R, Python, or SQL is always a primary evaluation: it is attributed, enters the transcript, and becomes visible to the agent.

There is no general invisible “read-only code” channel.
Dynamic-language evaluation cannot promise non-mutation or rollback.

### 15. Large-data exploration through live and snapshot views

A viewer should not need to retrieve or copy an entire table merely to display the current viewport.
A **live view** retains an opaque reference to a supported runtime object and requests only bounded rows, columns, profiles, and metadata through typed inspection operations.
It is revisioned, normally requires the runtime to be idle, and never accepts arbitrary caller-supplied language code.

A **snapshot view** materializes an immutable Arrow-, Parquet-, or DuckDB-backed representation.
It may cost more to open, but it remains stable and browsable outside the R/Python runtime while the agent continues computing.
The viewer should make the mode, source revision, staleness, and refresh behavior explicit.

### 16. A narrow public surface over extensible internals

New agent capabilities should usually appear as ordinary language code, runtime helpers, files, or private protocol extensions rather than additional globally visible MCP tools.
Human viewer capabilities belong on the local sidecar API, not in the MCP schema.
Every new public MCP field or tool has a permanent model-context cost.

## Product model

- Product and binary: `mcp-console`.
- MCP initialization identity: `mcp-console`.
- Default client registration name: `console`.
- Frequent MCP tool: `send`.
- Low-frequency environment and lifecycle tool: `session`.
- Codex tool names: `mcp__console.send` and `mcp__console.session`.
- Session: one sandboxed worker process containing R, reticulate Python, and DuckDB.
- Submission: one complete R, Python, or SQL cell.
- Evaluation: execution of one submission.
- `stdin`: exact stream text queued to the session worker, not another code submission; it may contain multiple lines, receives no implicit newline, and is not acknowledged as consumed.
- Durable record: generated `transcript.md`, source-only `transcript.qmd`, and retained output and artifact files.
- Refined notebook, report, or script: a separate user artifact.
- Local sidecar API: process-scoped observation, structured inspection, and attributed external control.
- Viewer: a short-lived client of an already-running server, never its owner.
- Table view: either a revisioned live reference served by typed runtime inspection or an immutable point-in-time snapshot.

## Non-goals

The initial design does not aim to provide:

- a general terminal or PTY tool;
- a public Jupyter-kernel or notebook-editing product surface;
- line-by-line construction of top-level programs;
- automatic language inference;
- a variable or package inventory on every MCP turn;
- automatic package installation after observing an import error;
- per-cell package requirements;
- arbitrary package removal or environment downgrades in a live session;
- automatic registration of every Python object as a SQL table;
- textual pagination through an entire large table in MCP output;
- guaranteed zero-copy transfer for every object type;
- MCP structured output, resource retrieval, or inline images in v1;
- silent worker restart after state loss;
- a persistent `mcp-console` daemon in v1;
- remote, cross-host, or automatic cross-container viewers;
- arbitrary invisible sideband code;
- concurrent R/Python inspection while a primary evaluation is running;
- silent changes behind a table-view ID without explicit revision, invalidation, or refresh semantics;
- direct viewer access to worker IPC, arbitrary filesystem paths, or internal JSONL records.

## Success criteria

The design is successful when:

1. Normal agent use reads like `{"python":"..."}`, `{"r":"..."}`, `{"sql":"..."}`, and `{}`.
2. The server exposes no more than two compact MCP tools.
3. Switching languages does not create another process or require a file round trip.
4. Ordinary evaluations finish in one call; long evaluations remain observable and interruptible.
5. R top-level evaluation does not introduce a console-owned R wrapper frame.
6. Debuggers and runtime input work without making normal code line-oriented.
7. No evaluation can inject unbounded text into model context.
8. After context compaction, the agent can inspect `transcript.md` and recover its work.
9. A worker crash cannot corrupt MCP transport or masquerade as preserved state.
10. A human viewer can discover and attach to a live server without starting or prolonging a daemon.
11. A disconnected or slow viewer can resume or resynchronize without blocking execution or causing unbounded memory growth.
12. A user can view original-resolution plots, browse a large live table by viewport without retrieving it in full, and continue exploring a snapshot while the agent computes, all without placing those payloads in model context.
13. Arbitrary external code is attributable, visible in the transcript, and reported to the agent before it relies on stale state.
14. Structured inspection accepts no caller-supplied R, Python, or SQL source and fails explicitly when the runtime is busy or the object is stale.
15. The public behavior is testable end-to-end independently of implementation internals.
16. A contributor can understand the product contract and locate subsystem ownership from `AGENTS.md`.
