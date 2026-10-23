# MCP Console Interface

**Status:** Draft v0.3 \
**Date:** 2026-07-27 \
**Scope:** Agent-facing MCP tools and observable behavior

This is an exploratory interface sketch, not the current public contract.
Its transcript paths reflect an earlier layout; [`CONFIGURATION.md`](../CONFIGURATION.md#17-logs-caches-and-retention) proposes the canonical Console directory and a journal spanning each server lifetime.
These drafts need not be synchronized before implementation; current behavior is documented under [`docs/`](../../docs/README.md).

## 1. Interface summary

MCP Console exposes two text-returning tools:

```text
send
session
```

`send` handles the frequent path: evaluate one R, Python, or SQL cell; supply interactive stdin; or wait for a running evaluation.
`session` handles infrequent environment and lifecycle operations.
The MCP initialization identity is `mcp-console`, and the intended default client registration name is `console`.
Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.

The interface is optimized for frequent use and global enablement:

- language is encoded by the object key;
- the default session and wait behavior are implicit;
- ordinary success contains only useful console text;
- state markers appear only when needed;
- all responses are bounded;
- larger output and artifacts are ordinary workspace files;
- v1 does not require structured MCP output, resources, or inline images.

## 2. Tool: `send`

### 2.1 Draft schema

```json
{
  "name": "send",
  "description": "Persistent R, Python, and DuckDB SQL console. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. State persists across calls; R and Python exchange objects, and SQL queries live or registered tabular data. Language-native help, introspection, interactive input, and debuggers work. Send exactly one complete r, python, or sql cell, optionally with stdin; send stdin on its own to queue exact text to the session worker; send neither to wait/poll. Large values are previewed; oversized stdout/stderr, plots, artifacts, the Markdown transcript, and the source-only Quarto document are saved in the workspace.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "properties": {
      "r": {
        "type": "string",
        "description": "Complete multiline R cell in persistent state. Python objects are available through py; R help, browser(), and recover() work."
      },
      "python": {
        "type": "string",
        "description": "Complete multiline Python cell in persistent state. R objects are available through r; help(), breakpoint(), and pdb work."
      },
      "sql": {
        "type": "string",
        "description": "Complete DuckDB SQL cell in the persistent catalog. Query live or registered tabular data; use SHOW TABLES, DESCRIBE, SUMMARIZE, and EXPLAIN for discovery. CLI dot commands are not supported."
      },
      "stdin": {
        "type": "string",
        "description": "Raw text queued to the session worker's standard input, whether it is evaluating or idle. A single value may satisfy multiple reads; newlines are significant and are not added automatically. Queuing does not acknowledge consumption, and unread text may satisfy later reads."
      },
      "session": {
        "type": "string",
        "default": "default",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        "description": "Persistent named session; defaults to default. Use another name for independent or concurrent state. A missing session is created by a code cell or nonempty stdin."
      },
      "label": {
        "type": "string",
        "minLength": 1,
        "maxLength": 160,
        "description": "Optional short heading for this cell in the Markdown transcript; it has no effect on execution."
      },
      "wait_ms": {
        "type": "integer",
        "minimum": 0,
        "maximum": 300000,
        "default": 30000,
        "description": "Maximum time this call waits for output or a state change. It never limits or cancels the computation."
      }
    }
  }
}
```

The schema intentionally avoids a large `oneOf`.
The server performs semantic mode validation and returns a short tool error for invalid combinations.

### 2.2 Modes

| Present mode fields                                          | Operation                              |
| ------------------------------------------------------------ | -------------------------------------- |
| exactly one of `r`, `python`, `sql`, optionally with `stdin` | Evaluate one complete cell             |
| `stdin` only                                                 | Queue exact text to the session worker |
| none of `r`, `python`, `sql`, `stdin`                        | Wait for or poll the session           |
| any other combination                                        | Tool error                             |

`session`, `label`, and `wait_ms` are modifiers, not modes.

Additional rules:

- `label` is accepted only with a code cell.
- Within `send`, a missing session is created by a code cell or nonempty `stdin`.
  The `session` `prepare` action may also create it.
- Polling and empty `stdin` do not create a missing session.
- New code is accepted only while the session is idle.
- A session runs one top-level evaluation at a time; code sent while it is busy is rejected rather than queued.
- Bundled `stdin` is queued after the submitted cell starts, without waiting for an input request.
- Standalone `stdin` is accepted whether the worker is evaluating or idle.
  It is queued to the same worker stream and may be consumed by a later evaluation or background runtime job.
- `wait_ms` bounds waiting on an evaluation; it does not bound lazy worker startup for an idle stdin-only call.

### 2.3 Common calls

```json
{ "python": "import json\nlogs = json.load(open('logs.json'))" }
```

```json
{ "r": "df <- tibble::as_tibble(py$logs)" }
```

```json
{ "sql": "select level, count(*) as n from df group by level order by n desc" }
```

```json
{}
```

A non-default session is explicit only when needed:

```json
{ "python": "fit_model()", "session": "model-fit" }
```

A label is optional editorial metadata for the generated transcript:

```json
{
  "sql": "select level, count(*) as n from df group by level",
  "label": "Count records by level"
}
```

It does not affect evaluation or define a runtime identifier.

## 3. Cell semantics

An `r`, `python`, or `sql` value is one complete top-level submission.

- Multiline cells are supported.
- The property name determines the language; source text is never inspected to infer it.
- Incomplete R or Python parser input is an error, not a continuation prompt awaiting another tool call.
- A SQL cell may contain multiple statements if the SQL adapter supports them, but it has one evaluation identity and one final state transition.
- Assignments and state changes performed before a later language error remain unless the language or database transaction semantics roll them back.
- R evaluates top-level expressions in order and auto-prints each visible result, stopping at the first error.
- Python executes statements in persistent `__main__` state and displays the final expression through Python's display hook.
- SQL returns compact statement summaries or a bounded table preview.

The public abstraction is a cell-oriented console, not a notebook document.
Submissions are chronological execution messages and are not editable cells owned by the tool.

### 3.1 Execution identity and call stacks

Submitted source remains the source identity for diagnostics, tracebacks, and transcripts.
Internal dispatch code must not replace it with a large generated wrapper expression.

R cells are parsed and evaluated at a native top-level boundary.
A user call such as:

```r
f <- function() sys.calls()
f()
```

must not contain an MCP Console R closure merely because the cell arrived through MCP.
Native top-level error and interrupt contexts may exist internally, but the visible R call stack should begin with user calls.

Python cells use a synthetic filename such as `<mcp-console:python:e0017>` and preserve Python-native traceback locations.
The initial implementation may cross a minimal reticulate/R bridge.
If Python calls back into R, that real bridge may appear in R introspection; the server does not falsify it.

SQL cells use a synthetic evaluation identity and DuckDB diagnostics.
The initial R/DBI implementation may place a small console SQL bridge and DBI/backend calls on the R stack while SQL is active.
This is observable only when execution re-enters R or is inspected during that call.
Source is stored out of band and bridge calls use a short evaluation ID rather than embedding the complete SQL string.

## 4. Interactive input and debuggers

When evaluated code invokes a supported input consumer—such as R `readline()`, Python `input()`, R `browser()`, `recover()`, or a Python debugger—the session may enter `input_required` if queued stdin does not satisfy it.

The initiating call returns the prompt and an explicit marker:

```text
Browse[2]>
[input]
```

A call can queue exact text while the evaluation is active or idle:

```json
{ "stdin": "where\nn\nc\n" }
```

The text may contain one or more complete or partial lines.
Newlines are significant and are not added automatically; send `"\n"` to submit a blank line.
Queuing input does not acknowledge that the runtime consumed it.
Unread queued text may satisfy later reads or evaluations, including direct fd-0 reads by background jobs, and is discarded when the worker stops.

The runtime emits `InputRequested` before a supported console read and `InputReceived` after that read succeeds.
The supervisor treats the request as provisional for a short grace window.
If the receipt arrives in that window, it continues waiting for output, another request, completion, or the MCP deadline; otherwise it returns the prompt and `[input]` early.
The receipt belongs to the runtime read, not to a particular submitted stdin value or byte range.
Direct fd-0 reads emit neither event.
The grace is intentionally a latency heuristic: a delayed receipt may expose an extra `[input]` boundary, while a longer wait would make genuinely incomplete input less responsive.

`stdin` is not a new code cell and is not owned by an evaluation.
The runtime decides whether the bytes are debugger commands, expressions accepted by the debugger, ordinary program input, or input for a background job.
When an evaluation is active, its identity remains active until that evaluation ends.

The implementation must distinguish cell source from interactive input structurally.
It must not preload a cell as generic console lines that a nested `readline()` or `input()` could accidentally consume.

## 5. Waiting and polling

The initiating code call waits until one of these occurs:

- the evaluation completes or errors;
- a runtime input request remains unreceived past its grace window;
- the worker stops;
- `wait_ms` expires.

`wait_ms` limits the MCP wait only.
It is not an execution timeout and does not interrupt the runtime.

If the wait expires, the result contains any new bounded output and ends with:

```text
[running]
```

A call with no mode field waits for the selected session:

```json
{}
```

It returns when new output appears, an input request remains outstanding past its grace window or the call deadline, the evaluation completes, the worker stops, or its own `wait_ms` expires.
`wait_ms: 0` is a nonblocking drain.

An idle session returns:

```text
[idle]
```

The leading newline is part of each server-owned `[running]`, `[idle]`, and `[input]` banner, even when no output precedes it.

A poll against a missing session is a tool error.

MCP request cancellation is distinct from wait expiry:

- cancelling the request that initiated an active evaluation should request interruption of that evaluation;
- cancelling a later poll cancels only that waiter;
- `wait_ms` expiry never cancels the evaluation.

## 6. Session requirements

Requirements are additive logical-session configuration managed by `session`, not modifiers on ordinary code cells.

- Python entries are PEP 508 requirement strings.
- R entries use the resolver grammar configured by the implementation.
- Requirements make packages available; they do not import or attach them.
- Requirements persist across runtime restarts.
- Repeating an already-satisfied requirement is idempotent.
- Removing packages, downgrading versions, changing interpreter versions, or replacing the complete manifest is outside the v1 tool interface.
  Close and recreate the session when necessary.

`prepare` resolves and adds requirements without replacing an existing runtime.
It creates the logical session if needed but may leave it in `prepared` state without a worker.
For an idle server-managed runtime, prepare can add R packages by prepending the new managed library to the live `.libPaths()` and removing its predecessor, and can add Python packages through reticulate without replacing the runtime.
It preserves the other R library paths and in-memory runtime state, and retains the confirmed R library for later generations.
Before Python initializes, it updates and materializes the manifest; after initialization, it activates a candidate that uses the same `libpython`.
A mixed R and Python preparation commits both retained configurations only after both live changes succeed.
A failed resolution or activation leaves the retained configuration unchanged.
If a synchronized failure may have partially changed the live runtime, evaluation remains available so the caller can save state, but new requirement additions report that restart is required until a successful explicit restart.
Transport or protocol failures still stop a runtime whose usability is unknown.
Preparation while the runtime is evaluating is an error.

`restart` may include additive requirements.
Resolution occurs before the old runtime is terminated.
If resolution fails, the current runtime remains intact.
After successful resolution, restart creates a new runtime generation with the retained merged manifest.
The current implicit-session implementation accepts only Python additions with `restart`; it reuses the R library retained by an earlier successful `prepare`.
A stopped worker cannot apply live additions, so the current implementation reports that a restart is required without retaining new requirements.

The resolver may run outside the arbitrary-code worker and populate immutable caches.
Package download access does not imply general network access for user code.

## 7. Tool: `session`

### 7.1 Draft schema

```json
{
  "name": "session",
  "description": "Prepare, inspect, or control persistent console sessions; normal evaluation and polling use send. Requirements are additive session configuration and survive runtime restarts. prepare creates the session if needed and adds requirements without replacing an existing runtime. An idle server-managed runtime can activate new R and compatible Python packages in place while preserving live state. restart starts a fresh runtime generation; any existing in-memory R, Python, and SQL state is lost, while requirements, workspace files, and the transcript are retained. close ends the logical session.",
  "inputSchema": {
    "type": "object",
    "additionalProperties": false,
    "required": ["action"],
    "properties": {
      "action": {
        "type": "string",
        "enum": ["list", "status", "prepare", "interrupt", "restart", "close"],
        "description": "Session operation: list, status, prepare, interrupt, restart, or close."
      },
      "session": {
        "type": "string",
        "default": "default",
        "minLength": 1,
        "maxLength": 64,
        "pattern": "^[A-Za-z0-9][A-Za-z0-9_.-]*$",
        "description": "Target session; defaults to default."
      },
      "requirements": {
        "type": "object",
        "additionalProperties": false,
        "description": "Additive package requirements, valid with prepare or restart. Resolution runs outside the worker sandbox, where package installation or build code may execute on the host.",
        "properties": {
          "r": {
            "type": "array",
            "items": { "type": "string", "minLength": 1 },
            "maxItems": 64,
            "description": "R package requirement strings. `ir` prevents installation from local package sources because it runs with server permissions. prepare can add them to an idle built-in runtime without replacing it."
          },
          "python": {
            "type": "array",
            "items": { "type": "string", "minLength": 1 },
            "maxItems": 64,
            "description": "PEP 508 Python requirement strings. prepare can activate compatible additions in an idle server-managed runtime."
          }
        }
      }
    }
  }
}
```

### 7.2 Validation

- `requirements` is valid only with `prepare` or `restart`.
- `list` is global and should omit `session`; other actions target one session.
- `prepare` requires at least one nonempty R or Python requirement.
- `restart` may omit requirements and retain the existing manifest.
- `interrupt` requires an active worker.
  `restart` and `close` require an existing logical session.

### 7.3 Actions

`list` returns one compact line per existing session:

```text
default    idle
analysis   prepared
model-fit  running  python  18s
```

`status` returns only facts useful for deciding the next action:

```text
default  generation=1  input_required  r
requirements: r=2 python=1
transcript: .agents/console/sessions/default/transcript.md
```

`prepare` follows the requirement semantics above.
For a missing session it retains configuration without starting a worker; the first cell or nonempty stdin submission starts it.
For an idle running session it preserves the worker generation and in-memory state.

```json
{
  "action": "prepare",
  "requirements": { "r": ["tibble", "dplyr"], "python": ["polars>=1"] }
}
```

`interrupt` requests a cooperative interrupt through a control path that is not queued behind the evaluation.
It preserves the worker and runtime state when recovery succeeds.
It never silently escalates to restart.

`restart` resolves any supplied additive requirements before changing runtime state, then increments the session generation and starts a fresh runtime with the retained merged manifest and workspace policy.
If a worker exists, it is terminated and all in-memory R, Python, SQL, debugger, and process state is lost.
On a prepared session with no worker, restart simply starts the first generation.
The transcript records the generation boundary.
The current implicit-session implementation accepts only Python additions on this action and reuses its retained R library.

`close` terminates the worker and removes the logical session and its requirement manifest.
Retained workspace files and transcripts follow explicit retention policy; closing a session must not silently delete unrelated user files.

## 8. Session state model

Externally meaningful states are:

```text
preparing
prepared
starting
idle
running
input_required
stopped
```

A missing session is absent, not another state.

```text
absent
  ├─ code cell ─────> preparing? ─> starting ─> running
  ├─ nonempty stdin ──> preparing? ─> starting ─> idle
  └─ prepare ───────> preparing ──> prepared

prepared
  ├─ code cell ─────> starting ─> running
  ├─ nonempty stdin ──> starting ─> idle
  └─ close ─────────> absent

idle
  ├─ code cell ─> running
  └─ stdin ────> idle

running
  ├─ success/error/interrupt ─> idle
  ├─ stdin ─> running
  ├─ input request without receipt after grace ─> input_required
  ├─ input receipt ─────────────────────────────> running
  └─ crash/exit/kill ─────────> stopped

input_required
  ├─ stdin ─> input_required until a runtime event
  └─ input receipt ─> running

stopped
  ├─ restart ─> preparing? ─> starting
  └─ close ─> absent
```

Visible prompt strings are output.
They are never used to infer these states.
An input request is provisional internal state during its grace window; a receipt in that window keeps the externally meaningful state `running`.

## 9. Text result contract

Both tools return MCP text content and advertise no `outputSchema` in v1.

### 9.1 Completed evaluation

Return useful visible output directly:

```text
[1] 2
```

If a completed evaluation has no visible output, return:

```text
[done]
```

### 9.2 Running evaluation

Return the bounded output delta followed by:

```text
[running]
```

### 9.3 Input required

Return the prompt and preceding bounded output followed by:

```text
[input]
```

### 9.4 Stopped worker

```text
[stopped: worker exited with status 1]
```

### 9.5 Language outcomes and tool errors

R errors, Python exceptions, and SQL errors are normal evaluation outcomes.
The tool successfully delivered the cell and collected the runtime response, so they normally use `isError: false`.

Use `isError: true` for failures to use or operate the tool, including:

- conflicting mode fields;
- code sent to a busy session;
- poll or control against an unknown session;
- dependency preparation failure;
- worker startup or private-protocol failure;
- sandbox or authorization failure.

## 10. Oversized output

A single global response budget applies after all text and markers are assembled.
The implementation should distinguish three output classes.

### 10.1 Explicit stdout and stderr

Emitted cell streams are appended to the existing per-evaluation raw file, up to 1 GiB, for example:

```text
.agents/console/sessions/<run ID>/outputs/call-000017.log
```

Each MCP reply considers only bytes produced since the previous sealed reply for that evaluation.

If the new delta fits, return it.
If it exceeds the budget:

1. return a bounded useful excerpt, normally preserving both the beginning and the most recent tail;
2. state how much was omitted;
3. include the retained-file path and distinguish a complete retained stream, a retained prefix, and sources with no cell log;
4. advance the reply cursor to the current end of the spool.

Example:

```text
Loading partition 1...
Loading partition 2...

... 171,204 bytes omitted ...

Loading partition 98...
Loading partition 99...

[truncated: .agents/console/sessions/<run ID>/outputs/call-000017.log]
[running]
```

A later poll reports only output created after that reply.
It does not force the agent to page through an old truncated backlog.
Retained prior output remains available through ordinary file tools with access to the Console server recording workspace.
The path is controller-owned for SSH, Docker, and Docker Sandbox; worker-only file access is insufficient.
Omitted output beyond a retention limit or write failure, and omitted output from sources without a cell log, is unavailable.

When an evaluation completes, unread omitted bytes are not injected into later unrelated evaluations.
The file path is the continuation mechanism for clients with access to that recording workspace; v1 does not add an output-reading protocol.
Clients without that access still receive bounded previews and final diagnostics; their retrieval workflow remains deferred.

### 10.2 Large returned values

Known large values should be summarized before full textual materialization:

- tables and relations: bounded rows, columns, cell widths, and bytes;
- arrays: dimensions, type, and a bounded sample;
- mappings and sequences: type, size, and bounded elements;
- SQL results: fetch only enough rows to make the preview.

```text
10,241 rows × 8 columns

 timestamp             level  message
 2026-07-21 12:01:04   INFO   Started
 2026-07-21 12:01:05   ERROR  Connection failed
 ...

[showing 20 rows × 8 columns]
```

The underlying value remains in runtime state for another expression, query, summary, sample, or explicit export.

For an unknown class with an arbitrary user-defined print method, capture its output like any other stream and apply the spool-and-truncate policy.
Do not claim a structural preview that the runtime cannot safely produce.

### 10.3 Plots and binary artifacts

Current plot responses use whole MCP image blocks under independent image budgets.
Images admitted for recording are also saved in the session artifact directory.
Text-preview exhaustion does not suppress a later image that fits its own budget.
Images rejected at ingestion are not retained; broader binary-artifact retention and retrieval remain future work.

Viewing a retained file requires ordinary file or image tools with access to the Console server recording workspace.

### 10.4 Initial default budgets

The selected initial text default is 8 KiB across the complete result, with approximately equal head and tail space after reserving notices.
It has no per-call control or configuration field in the current implementation; configuration remains future work.
Other suggested printer defaults in this sketch remain exploratory:

```text
inline text per reply:  8 KiB of rendered UTF-8, including notices
preview rows:           20
preview columns:        12
maximum cell width:     160 characters
```

Retained-file continuation uses the existing raw per-cell logs and their 1 GiB limit.
Advertised paths belong to the Console server recording workspace, on the controller for SSH, Docker, and Docker Sandbox.
Full retained text requires existing filesystem tools with access there; tools confined to another host cannot retrieve it.
A retained prefix is not the complete emitted output, and preview tails remain independent of retention failures.
Console-owned read/search, file transfer, export, and a retrieval workflow for clients without suitable file access remain unresolved and deferred.

Tests should assert hard bounds and semantic markers, not incidental table glyphs or terminal widths.

## 11. Transcript contract

Each session exposes:

```text
.agents/console/sessions/<session>/transcript.md
```

The transcript is append-only and grows as recorded events arrive.
The planned evaluation-level event vocabulary makes completion, interruption, restart, stop, and crash explicit boundaries.
It contains:

- session and generation metadata;
- stable evaluation IDs;
- optional labels;
- complete submitted source;
- bounded stdout, stderr, result, and error excerpts;
- paths to complete output and artifacts;
- input prompts and supplied interactive lines without redaction;
- restart, stop, and crash boundaries.

It is a chronological execution record, not a promise of reproducibility and not a polished notebook.
The companion `transcript.qmd` contains executable submitted code cells plus `ir` front matter for declared R and Python requirements.
Users can reuse its source or run `ir render` to execute the client-authored action stream in a fresh environment and export a new report.
The goal is to reproduce the analysis represented by the Markdown transcript, although not every runtime detail can be reconstructed yet.
Agents create refined `.qmd`, `.R`, `.py`, or `.ipynb` files separately.

A granular event journal may back transcript recovery.
It is internal implementation state, is not advertised as the normal agent artifact, and need not share the Markdown format's compatibility guarantees.

## 12. SQL behavior

The SQL frontend uses one persistent DuckDB connection per worker generation.
The initial implementation uses the DuckDB R package and DBI; that bridge is not exposed as another agent-facing language.

- DuckDB catalog tables and views persist until restart or worker loss.
- Live R data frames may be visible through DuckDB's R environment scanning.
- A catalog table or view takes precedence over a scanned R object with the same unqualified name.
- Runtime helpers can explicitly register stable R, Python, or Arrow-backed relations under SQL names.
- Python objects may initially cross reticulate/R for conversion or Arrow registration.
- Result-producing statements fetch only enough data to build the bounded preview; they are not automatically collected or spooled in full as text.
- DuckDB CLI dot commands are not SQL and are not supported by the `sql` field.
  Use SQL-native commands such as `SHOW TABLES`, `DESCRIBE`, and `SUMMARIZE`.
- The DuckDB CLI is a behavioral reference for useful previews and discovery, not an embedded subprocess.

The interface does not promise that every object transfer is zero-copy.
Conversions and registration lifetimes must be explicit when they matter.

## 13. Complete interaction example

```json
{
  "action": "prepare",
  "requirements": { "r": ["tibble", "dplyr"] }
}
```

```text
[prepared]
```

```json
{ "python": "import json\nlogs = json.load(open('logs.json'))" }
```

```text
[done]
```

```json
{
  "r": "df <- tibble::as_tibble(py$logs)\ndplyr::glimpse(df)",
  "label": "Convert logs to a tibble"
}
```

```text
Rows: 10,241
Columns: 8
$ timestamp <chr> ...
$ level     <chr> ...
...
```

```json
{
  "sql": "select level, count(*) as n from df group by level order by n desc",
  "label": "Count records by level"
}
```

```text
level     n
INFO   8231
WARN   1450
ERROR   560
```

```json
{ "r": "browser()" }
```

```text
Called from: top level
Browse[1]>
[input]
```

```json
{ "stdin": "where\n" }
```

```text
where 1: browser()
Browse[1]>
[input]
```

```json
{ "stdin": "c\n" }
```

```text
[done]
```

## 14. Deliberately omitted from v1

- MCP resources as the primary oversized-output mechanism;
- a generic `read_output` operation when host file tools can read session files;
- structured output and `outputSchema`;
- inline MCP images;
- a notebook-export MCP tool;
- prose-only transcript cells;
- per-submission include/omit flags for a future curated artifact;
- automatic variable or package inventories;
- line-by-line parser continuation for top-level code;
- DuckDB CLI dot commands;
- automatic scanning of every Python global as a SQL relation;
- silent worker restart;
- public Jupyter-kernel or Ark-specific operations;
- MCP task augmentation.

Reconsider an omitted feature only when a concrete client or workflow cannot be served by the two-tool, text-and-files model.
