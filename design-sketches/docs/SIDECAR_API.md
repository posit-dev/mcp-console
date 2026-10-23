# Local sidecar and viewer API

**Status:** Draft v0.3 \
**Date:** 2026-07-27 \
**Scope:** Process-scoped observation, inspection, viewer, and external-control interface \
**Companion documents:** [`CLI.md`](CLI.md), [`MCP_INTERFACE.md`](MCP_INTERFACE.md), [`RUNTIME_BACKEND.md`](RUNTIME_BACKEND.md), [`ARCHITECTURE.md`](ARCHITECTURE.md)

## 1. Purpose

MCP Console needs a human-facing path that does not consume model context and does not require a persistent daemon.
A user should be able to attach to the `mcp-console` process already owned by an MCP client and:

- see which sessions and evaluations are active;
- follow output and state changes;
- read the generated transcript;
- view plots and artifacts at their original resolution;
- inspect R, Python, and SQL objects;
- open large tabular objects in an interactive data explorer;
- optionally submit attributed code or debugger input.

The sidecar API is a local integration surface for CLI commands, the bundled viewer, and third-party viewers.
It is not another MCP transport and it is not a daemon protocol.

## 2. Core distinction: observe, inspect, control

The API separates three operation classes.

### 2.1 Observe

Observation never enters a language runtime and never mutates session state.

Examples:

- list sessions;
- read current state and evaluation metadata;
- subscribe to events;
- read transcript excerpts;
- fetch already-retained stdout, stderr, plots, and artifacts;
- read requirement and runtime metadata.

### 2.2 Inspect

Inspection performs a bounded, structured query against live runtime state.
It may need to execute internal R or Python helpers on the runtime-owning thread, but the caller does not supply arbitrary source code.

Examples:

- list top-level variables;
- describe an object;
- read a supported table schema;
- open or refresh a revisioned live table view;
- create a point-in-time snapshot view;
- profile a column;
- inspect a bounded slice.

Inspection is not included in the agent transcript because it is tooling traffic rather than authored analysis.
It is recorded in the private operational journal with client attribution and timing.

“Inspection” is a behavioral restriction, not a mathematical guarantee of purity.
Dynamic-language operations can force promises, invoke active bindings, or dispatch user methods.
Implementations must prefer known concrete types, bounded native access, and explicit snapshotting; unsupported objects fail rather than running arbitrary display code.

### 2.3 Control

Control changes or may change the live session.

Examples:

- submit R, Python, or SQL;
- provide interactive stdin;
- interrupt, restart, prepare, or close a session.

Arbitrary code is always control.
It is assigned an origin, enters the chronological transcript, and is visible to other clients.
There is no invisible arbitrary-code channel presented as “read-only.” R and Python provide no general rollback or non-mutation guarantee.

## 3. No-daemon lifecycle

The MCP stdio server owns the local API and all worker processes:

```text
MCP client
  └── mcp-console serve
        ├── MCP stdio adapter
        ├── process-scoped local API
        ├── event broadcaster
        ├── session supervisor
        └── sandboxed session workers

optional sidecars
  ├── mcp-console view
  ├── mcp-console watch
  ├── mcp-console send
  └── third-party viewer
```

The local API starts and stops with `mcp-console serve`.

- It never detaches.
- It does not auto-start when a viewer runs.
- A viewer does not keep it alive.
- Sessions do not survive the MCP server process.
- Session files may remain after exit according to retention policy.

A future daemon would be a separate product decision, not an accidental consequence of the viewer interface.

## 4. Transport and discovery

### 4.1 Local transport

Initial transport:

```text
Unix:    HTTP/1.1 over a Unix-domain socket
Windows: HTTP semantics over a named pipe or authenticated loopback endpoint
```

The transport is local-user scoped.
No non-loopback TCP listener is supported in v1.

HTTP semantics are useful even on a local socket because they provide:

- ordinary request/response framing;
- status codes and content negotiation;
- Server-Sent Events for subscriptions;
- streaming and range responses;
- Arrow and image payloads;
- straightforward SDK generation later.

The service implementation should be listener-independent so tests and the optional browser proxy can mount the same router on another caller-owned transport.

### 4.2 Runtime record

Each server writes a protected ephemeral record, conceptually:

```json
{
  "schema": 1,
  "instance_id": "7ac91f2b",
  "pid": 48122,
  "started_at": "2026-07-26T22:14:00Z",
  "version": "0.1.0",
  "protocol_version": 1,
  "workspace": "/home/user/project",
  "transport": "unix",
  "endpoint": "/run/user/1000/mcp-console/7ac91f2b.sock"
}
```

The runtime directory is private to the current OS user.
On Unix, the directory should be mode `0700`, records `0600`, and sockets inaccessible to other users.
Windows uses an equivalent user-restricted ACL.

Discovery clients:

1. enumerate records;
2. reject malformed or unsupported records;
3. verify that the process is alive;
4. call the API handshake;
5. verify the returned instance ID and process start identity;
6. ignore or remove stale records.

A PID alone is not identity because PIDs are reused.

### 4.3 Browser bridge

Browsers do not connect directly to Unix sockets or named pipes.
`mcp-console view` starts a separate short-lived loopback process that serves the bundled UI and proxies to the selected local API.

The proxy:

- binds only to loopback;
- uses an unguessable URL or session credential;
- serves no permissive CORS headers;
- rejects unexpected `Origin` values;
- exits when the upstream server disappears or the user terminates it;
- does not expose the worker sandbox or arbitrary filesystem paths.

The MCP server itself does not open a browser-accessible TCP port by default.

### 4.4 Namespace reachability

Process-scoped attachment assumes the viewer shares the server's local OS and can reach its protected runtime directory and endpoint.
An MCP server launched inside a container, VM, remote development host, SSH session, or another user namespace may not be discoverable from the user's desktop.

V1 fails clearly rather than silently opening a broader listener.
Explicit socket forwarding, remote attachment, and authenticated cross-host viewing are future deployment features.
A host integration may mount or proxy the same listener-independent service when it owns the trust boundary, but that is not automatic CLI behavior.

## 5. Trust boundary

V1 trusts the local OS user, not arbitrary local web pages and not remote users.

Required protections:

- private runtime-directory and endpoint permissions;
- random instance identity and handshake verification;
- bearer or equivalent capability on transports that lack reliable peer credentials;
- no CORS by default;
- reject unexpected non-empty browser `Origin` headers on direct local endpoints;
- JSON content type required for mutations;
- bounded request and response sizes;
- no route that serves arbitrary filesystem paths;
- no secret values in discovery records committed to a workspace;
- audit attribution for inspection and control requests.

The API must not be advertised as safe to expose to a LAN.
Remote, multi-user, and browser-origin authorization are out of scope.

## 6. API shape

Representative endpoints:

```text
GET  /v1/ping
GET  /v1/server
GET  /v1/sessions
GET  /v1/sessions/{session}
GET  /v1/sessions/{session}/snapshot
GET  /v1/sessions/{session}/events

GET  /v1/sessions/{session}/transcript
GET  /v1/sessions/{session}/outputs/{output_id}
GET  /v1/sessions/{session}/artifacts/{artifact_id}

GET  /v1/sessions/{session}/objects
GET  /v1/sessions/{session}/objects/{object_id}
POST /v1/sessions/{session}/table-views
GET  /v1/sessions/{session}/table-views/{view_id}
POST /v1/sessions/{session}/table-views/{view_id}/slice
POST /v1/sessions/{session}/table-views/{view_id}/profile
POST /v1/sessions/{session}/table-views/{view_id}/code
POST /v1/sessions/{session}/table-views/{view_id}/refresh
DELETE /v1/sessions/{session}/table-views/{view_id}

POST /v1/sessions/{session}/evaluations
POST /v1/sessions/{session}/stdin
POST /v1/sessions/{session}/interrupt
POST /v1/sessions/{session}/prepare
POST /v1/sessions/{session}/restart
DELETE /v1/sessions/{session}
```

This is a conceptual resource map, not a commitment to exact URL spelling.
The stable contract should be generated from one versioned API description once implementation begins.

## 7. Snapshot and event stream

A viewer needs both a current snapshot and incremental events.

### 7.1 Snapshot

A session snapshot includes bounded current state:

```json
{
  "session": "default",
  "generation": 2,
  "state": "running",
  "requirements_revision": 3,
  "event_cursor": 1842,
  "active_evaluation": {
    "id": "e0047",
    "origin": "mcp",
    "language": "python",
    "label": "Fit candidate models",
    "started_at": "2026-07-26T22:20:11Z"
  },
  "transcript": "transcript.md",
  "artifact_count": 7
}
```

The snapshot does not contain complete outputs, full object values, or artifact bytes.

### 7.2 Event subscription

```text
GET /v1/sessions/{session}/events
Accept: text/event-stream
Last-Event-ID: 1842
```

Every event has a monotonically increasing cursor within the server instance.

Representative event names:

```text
session.created
session.state_changed
session.requirements_changed
session.restarted
session.stopped
session.closed

evaluation.started
evaluation.output
evaluation.input_requested
evaluation.input_received
evaluation.finished

artifact.created
object_catalog.invalidated
table_view.ready
table_view.stale
server.shutting_down
sync.resync_required
```

Events carry bounded metadata.
Large output, images, tables, and files are fetched separately by stable IDs.

### 7.3 Replay and reconnection

A reconnecting client resumes with `Last-Event-ID` or an explicit cursor, but never both.

The server keeps enough replay state to cover ordinary disconnects.
Replay may be backed by the private event journal, bounded in-memory history, or a projection over durable session records.
That storage is an implementation detail; the public contract is cursor-based.

If a client is older than retained replay state, the stream emits `sync.resync_required` and closes.
The client fetches a new snapshot, rebuilds its caches, and reconnects from the snapshot cursor.

### 7.4 Race-free catch-up

Subscription must not lose an event between snapshot/cursor capture and live registration.
Implement one of these equivalent patterns:

- register the subscriber, capture a high-water mark, replay through that mark, then enter live mode;
- return a snapshot with a cursor backed by replayable storage, then replay all events after that cursor.

### 7.5 Slow subscribers

Every subscriber has a bounded buffer.
Broadcasting never blocks session execution or transcript writing.

When a subscriber falls behind:

1. disconnect it;
2. retain no unbounded per-client queue;
3. let it reconnect with its last cursor;
4. replay or require resynchronization.

Output spools remain the source for complete text; the event stream is not an infinite log transport.

## 8. Object inventory

Object inventory is computed only at safe runtime boundaries, normally while the session is idle.

Supported namespaces may include:

```text
r       bindings in the console R environment
python  bindings in Python __main__
sql     DuckDB catalog tables and views
```

An object summary is bounded:

```json
{
  "object_id": "obj_7e13",
  "generation": 2,
  "revision": 11,
  "language": "r",
  "binding": "df",
  "kind": "table",
  "type": "tbl_df",
  "summary": "10241 rows x 8 columns",
  "capabilities": ["describe", "table_view"]
}
```

Object IDs are opaque, generation-scoped handles.
They do not grant access to arbitrary R or Python expressions.

A handle becomes stale when:

- the session restarts;
- the binding is removed or replaced;
- the object revision changes incompatibly;
- the server can no longer guarantee that it identifies the same object.

A stale request returns an explicit conflict containing the current revision where available.

## 9. Inspection scheduling

R and its embedded Python interpreter are owned by one runtime thread.
Inspection must not execute concurrently with a primary evaluation.

V1 scheduling rules:

- inspection runs only while the session is `idle`;
- it is lower priority than accepted primary evaluations and control operations;
- requests are bounded and cancellable;
- the server does not accumulate an unbounded inspection queue;
- a busy session returns `session_busy` plus the current state;
- viewers retain their last snapshot and retry after an idle event;
- inspection is not permitted merely because the runtime is waiting in `browser()` or `input()`;
- already-materialized table views and artifacts remain usable while the runtime is busy.

A future runtime may expose safe debugger-frame inspection or cooperative safe points.
That is an explicit capability, not assumed by the generic API.

## 10. Data explorer design

### 10.1 Why not arbitrary sideband code

A viewer could theoretically request:

```r
df[1001:1100, c("x", "y")]
```

through an invisible evaluator.
This is too weak a contract:

- arbitrary code can mutate state;
- indexing and printing may dispatch user methods;
- errors and prompts complicate viewer behavior;
- a human could silently invalidate the agent's assumptions;
- repeated viewport requests would contend with the runtime thread;
- the API would become another general execution surface.

Instead, the viewer sends typed data operations and the runtime backend chooses a supported implementation.
Ark's Data Explorer comm/backend is one candidate implementation; it is not itself the public sidecar protocol.

### 10.2 Two explicit view modes

The API supports `live` and `snapshot` modes:

```json
{
  "object_id": "obj_7e13",
  "expected_revision": 11,
  "mode": "live"
}
```

```json
{
  "object_id": "obj_7e13",
  "expected_revision": 11,
  "mode": "snapshot"
}
```

Every response identifies the mode, source generation, source revision, backend capabilities, and view revision.
The UI must not make a live reference look like an immutable snapshot or silently substitute one mode for the other.

### 10.3 Live views

A live view retains an opaque backend-owned reference to a supported R, Python, SQL, Arrow, or file-backed object.
Viewport and profile requests fetch only the requested rows, columns, summaries, filters, or sort results.

Properties:

- avoids eagerly collecting or copying an entire large object;
- can reflect the current explicitly revisioned object state;
- normally requires the session to be idle because operations enter the runtime-owning thread;
- returns `session_busy` rather than accumulating an unbounded queue;
- expires or becomes stale on restart, rebinding, unsupported mutation, reference loss, or capability change;
- accepts only typed operations, never caller-provided R, Python, or SQL source;
- may be implemented by Ark Data Explorer comms or an equivalent native inspection backend.

A live `view_id` never silently begins referring to a different object.
Each successful request returns the observed object revision.
If the binding or object revision changed incompatibly, the request returns `object_stale` or `view_stale`, and the user explicitly refreshes or reopens the view.

### 10.4 Snapshot views

A snapshot view records a stable point in time with:

- an immutable view ID;
- source object identity and revision;
- schema and row-count metadata when cheaply available;
- a materialized representation or read-only query plan;
- explicit storage and expiration accounting.

Preferred representations, selected by source type and size:

```text
R/Pandas in-memory frame  -> Arrow IPC, Parquet, or view-engine DuckDB snapshot
Arrow table/dataset       -> Arrow-backed snapshot where lifetime is safe
DuckDB table or query     -> snapshot or read-only relation in the view engine
file-backed table         -> file/query handle with explicit source metadata
```

Creating the snapshot may require one structured runtime task.
Once created, filtering, sorting, slicing, and profiling should run outside R/Python whenever possible.
This lets the user continue exploring an opened table while the agent performs another evaluation.

The preferred boundary is a supervisor-managed **view engine** that can query only immutable managed snapshots.
It may be a Rust component or a separate helper process.
It never loads user R/Python packages and never receives arbitrary language source.

Snapshot creation is explicit because copying a multi-gigabyte in-memory object may be expensive.
The viewer shows progress, size estimates where available, cancellation, and quota failures.

### 10.5 Backend-neutral inspection contract

The local API exposes a typed table-view protocol independent of the runtime substrate.
Conceptually:

```rust
trait TableInspectionBackend {
    fn open_live(&mut self, object: ObjectHandle, expected: Revision) -> LiveView;
    fn slice_live(&mut self, view: LiveViewHandle, request: SliceRequest) -> TableBatch;
    fn profile_live(&mut self, view: LiveViewHandle, request: ProfileRequest) -> ColumnProfile;
    fn snapshot(&mut self, object: ObjectHandle, expected: Revision) -> SnapshotDescriptor;
    fn close(&mut self, view: ViewHandle);
}
```

An Ark adapter may translate these operations to Data Explorer comm messages.
A native backend may call bounded internal helpers.
The browser, CLI, session manager, and MCP adapter never depend on Ark comm schemas or direct R expressions.

Backends declare capabilities per object, for example:

```json
{
  "modes": ["live", "snapshot"],
  "operations": ["slice", "sort", "filter", "profile", "code"],
  "max_live_rows": 1000,
  "supports_busy_runtime": false
}
```

Unsupported operations fail explicitly rather than falling back to invisible arbitrary evaluation.

### 10.6 Slice request

```json
{
  "offset": 1000,
  "limit": 100,
  "columns": ["timestamp", "level", "message"],
  "sort": [{ "column": "timestamp", "direction": "desc", "nulls": "last" }],
  "filters": [{ "column": "level", "operator": "eq", "value": "ERROR" }]
}
```

Filters use a small typed expression grammar.
They are not raw SQL, R, or Python strings.

Responses support content negotiation:

```text
application/json                      small metadata and small slices
application/vnd.apache.arrow.stream  efficient typed table data
```

The API enforces row, column, byte, time, and complexity limits.
A live request also reports the observed source revision; a snapshot request reports the immutable snapshot identity.

### 10.7 Profiles

Column profiling is explicit and bounded.
Potential operations include:

- null count;
- distinct estimate or count under a limit;
- numeric min, max, mean, quantiles, and histogram;
- string length and top values;
- datetime range;
- boolean counts.

Expensive exact operations require an explicit request and may return a progress event or cost estimate.

### 10.8 Convert ephemeral exploration to code

Filters, sorts, projections, and other supported view operations can be rendered as reproducible code for the source table type:

```text
R data frame or tibble  -> dplyr/base R expression
pandas DataFrame        -> pandas expression
Polars DataFrame        -> Polars expression
DuckDB relation/table   -> DuckDB SQL
```

The conversion endpoint returns source text and required binding metadata; it does not execute the text.
The UI may let the user copy it or explicitly submit it through the ordinary attributed control path.
Unsupported or lossy conversions fail or are marked approximate rather than silently changing semantics.

This preserves a useful motion from ephemeral visual exploration back to code without turning viewport traffic into hidden session mutations.

### 10.9 Refresh and staleness

For a live view, Refresh validates the source binding and opens or advances to a new explicit revision.
For a snapshot view, Refresh creates a new immutable snapshot revision.
Existing snapshots may remain usable until retention expires.

The server never silently changes the object identity or point-in-time data behind a `view_id`.

### 10.10 Retention

Live references and snapshots are subject to different limits:

- live references are generation-scoped, idle-time-limited, and released on restart or server exit;
- snapshots have per-view, per-session, and global storage quotas;
- both support explicit close and least-recently-used eviction;
- retained exports are created only when the user asks for them.

A viewer view is tooling state, not part of the refined analysis artifact unless explicitly exported.

## 11. Plots and artifacts

Plots are published as artifact metadata events:

```json
{
  "artifact_id": "art_31b2",
  "evaluation_id": "e0047",
  "kind": "plot",
  "mime_type": "image/png",
  "width": 2400,
  "height": 1600,
  "byte_length": 481992,
  "created_at": "2026-07-26T22:24:09Z"
}
```

The viewer fetches the original artifact by ID and may request a server-generated thumbnail.
Artifact IDs resolve only to files managed by the session; callers cannot turn the route into arbitrary path access.

SVG and other active formats require safe rendering policy.
The bundled viewer should not execute untrusted script content embedded in an artifact.

## 12. External evaluations

A control client submits:

```json
{
  "language": "r",
  "source": "summary(df)",
  "label": "Inspect model input",
  "client": {
    "id": "viewer-a7d9",
    "kind": "human_viewer",
    "display_name": "MCP Console Viewer"
  }
}
```

The session manager applies the same acceptance rules as MCP:

- one primary evaluation at a time;
- code is rejected while busy rather than silently interleaved;
- stdin is queued to the session worker FIFO whether it is evaluating or idle;
- wait and cancellation do not imply process termination;
- output is spooled and bounded;
- lifecycle controls use the same state transitions.

The transcript clearly marks origin, for example:

````markdown
::: {#e0048 .mcp-console-evaluation origin="viewer-a7d9" language="r"}

### Inspect model input

```{r}
summary(df)
```

:::
````

The MCP client maintains its own event cursor.
Before its next evaluation or state-dependent response, it receives a compact notice if external control activity occurred since that cursor.
Passive observation and structured inspection do not create such notices.

## 13. Internal worker requests

The local API does not talk directly to R or Python.
It asks the session manager, which may send a structured worker request:

```rust
enum InspectionRequest {
    ListObjects {
        namespace: Option<Namespace>,
    },
    DescribeObject {
        object: ObjectHandle,
    },
    OpenLiveTable {
        object: ObjectHandle,
        expected_revision: Revision,
    },
    SliceLiveTable {
        view: LiveViewHandle,
        request: SliceRequest,
    },
    ProfileLiveTable {
        view: LiveViewHandle,
        request: ProfileRequest,
    },
    SnapshotTable {
        object: ObjectHandle,
        expected_revision: Revision,
        destination: SnapshotDestination,
        limits: SnapshotLimits,
    },
    CloseView {
        view: ViewHandle,
    },
}
```

There is deliberately no general `EvaluateHidden { source }` request in the public sidecar protocol.

Runtime-specific internal helpers must:

- run on the owning runtime thread;
- use bounded operations;
- identify supported concrete types;
- avoid arbitrary print methods and user expressions where possible;
- restore temporary options and hooks;
- report whether they forced or materialized data;
- remain distinguishable from authored evaluations in logs and metrics.

## 14. Viewer cache model

The viewer treats events primarily as invalidation and lifecycle notifications, not as the sole source of truth.

- Session snapshot is authoritative for current state.
- Transcript and output spools are authoritative for retained textual history.
- Artifact records are authoritative for managed files.
- Object handles are valid only for their generation and revision.
- Live views are valid only for their retained object identity and explicit revision.
- Table snapshots are immutable under a view ID.

On `sync.resync_required`, session restart, or contradictory state, the viewer discards derived caches and fetches a new snapshot.

## 15. Protocol versioning

The server handshake reports:

```json
{
  "service": "mcp-console",
  "version": "0.1.0",
  "protocol_version": 1,
  "instance_id": "7ac91f2b",
  "pid": 48122,
  "capabilities": {
    "events": true,
    "inspection": true,
    "live_table_views": true,
    "snapshot_table_views": true,
    "arrow_stream": true,
    "control": true
  }
}
```

Clients reject unsupported major protocol versions.
Optional features are capability-negotiated rather than inferred from binary version.

This matters because `uvx mcp-console view` may run a different package version from the already-running MCP server.

## 16. Failure behavior

Representative errors:

```text
instance_not_found
protocol_incompatible
session_not_found
session_busy
input_not_requested
object_not_found
object_stale
view_stale
object_unsupported
inspection_unavailable
snapshot_too_large
live_view_unavailable
view_expired
replay_unavailable
permission_denied
```

Errors are bounded and structured for API clients.
They do not expose arbitrary runtime stack traces, filesystem paths outside managed roots, tokens, or object contents.

## 17. Acceptance criteria

The sidecar design is ready for initial implementation when tests demonstrate:

1. a viewer can discover and verify a live server without starting a daemon;
2. multiple server processes and identical session names are not confused;
3. a subscriber can disconnect, resume by cursor, or resynchronize explicitly;
4. a slow subscriber cannot block an evaluation or grow memory without bound;
5. passive viewing never changes the transcript or runtime;
6. arbitrary external code is attributed and enters the transcript;
7. inspection requests never accept caller-provided language source;
8. live inspection is serialized on the runtime thread and rejected while busy unless an explicit backend capability proves otherwise;
9. a large live table can be browsed by bounded viewport requests without retrieving or copying it in full;
10. an opened table snapshot remains browsable while the agent later runs code;
11. supported ephemeral filters and sorts can be converted to source code without executing it;
12. stale object, generation, live-view, and snapshot handles fail explicitly;
13. plots can be viewed at original resolution without entering MCP context;
14. local web origins cannot drive the control API without the viewer bridge's credential;
15. server exit closes streams, removes live discovery, and leaves no orphan worker;
16. an older or newer viewer negotiates protocol capabilities or fails clearly;
17. a viewer outside the server's OS or container namespace fails with a reachability diagnostic rather than causing a broader listener to appear.

## 18. Deliberately omitted from v1

- a persistent daemon;
- remote viewers or automatic container/VM forwarding;
- multi-user access control;
- direct browser access to the supervisor's Unix socket;
- arbitrary invisible sideband code;
- concurrent R/Python inspection during a running evaluation;
- silent live-view mutation without explicit revision or invalidation semantics;
- unbounded event replay;
- direct access to worker IPC or internal JSONL records;
- a guarantee that every dynamic-language inspection is free of all side effects.


## 19. Design influences

The local-service shape borrows several proven patterns from [kata](https://github.com/kenn-io/kata): one service API shared by machine and human clients, protected runtime discovery, snapshot-plus-event-stream clients, resumable cursors, bounded subscriber queues, and explicit resynchronization.
MCP Console deliberately does **not** copy kata's detached daemon, auto-start, persistent shared database, remote mode, or federation.

The data-view behavior is informed by [Positron's Data Explorer](https://positron.posit.co/data-explorer.html) and Ark's Data Explorer backend: retained live-object references, viewport-oriented row and column retrieval, ephemeral sorting and filtering, bounded column summaries, and code-first rather than document-owning semantics.
MCP Console also adds explicit immutable snapshot mode so exploration can continue while the live runtime is busy.
Plot history and original-resolution viewing follow the same separation used by [Positron's Plots pane](https://positron.posit.co/plots-pane.html).

These are product and protocol influences.
Ark is also a candidate runtime dependency under evaluation, but its comm schemas must remain behind a backend adapter.
MCP Console retains its own process-scoped lifecycle, sandbox boundary, multi-language session identity, local authorization, and attribution rules regardless of the selected backend.
