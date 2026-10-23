# Runtime backend evaluation

**Status:** Initial native R worker selected; eventual full-runtime backend remains open \
**Date:** 2026-07-27 \
**Last evaluated:** 2026-07-30 \
**Companion documents:** [`ARCHITECTURE.md`](ARCHITECTURE.md), [`SIDECAR_API.md`](SIDECAR_API.md), [`MCP_INTERFACE.md`](MCP_INTERFACE.md)

## 1. Decision to make

MCP Console needs one sandboxed worker process per named session.
The current native R worker uses the second option below.
The first option was implemented as an R-only prototype and compared against it.
That evaluation selected the native worker for the implemented `send(r = ...)` and `send(stdin = ...)` slice, but did not settle the eventual full-runtime backend.

The remaining question is which component should own the complete R frontend and runtime event loop:

1. run **Ark** as the worker and adapt its Jupyter/kernel and custom-comm interfaces;
2. extend the **purpose-built native worker** built on `harp`, `libr`, and libR's DLL-REPL API;
3. extract or upstream a **shared lower-level R runtime** used by Ark and MCP Console.

This is an implementation decision, not a product-interface decision.
The MCP tools, session lifecycle, bounded output, transcript, process-scoped local API, and human-viewer behavior must remain stable under either backend.

## 2. Product requirements the backend must satisfy

The selected backend must support:

- complete R, Python, and SQL cells in one process;
- native top-level R behavior and truthful stack/source semantics;
- persistent reticulate Python and persistent DuckDB state;
- explicit structured busy, idle, input-required, stopped, and crash states;
- `readline()`, `browser()`, `recover()`, Python `input()`, and debuggers;
- cooperative interrupt plus forced worker termination;
- bounded stdout, stderr, values, tables, help, and diagnostics;
- plots and other display artifacts;
- object inventory and typed sideband inspection;
- retained object references and bounded viewport access for large live tables;
- immutable snapshots that can be explored while the runtime is busy;
- no arbitrary invisible viewer-supplied code;
- process-level sandboxing and honest restart/state-loss boundaries.

The backend may expose additional capabilities, but the supervisor must negotiate them rather than assume them.

## 3. Option A: Ark-backed worker

### 3.1 Shape

```text
MCP and local sidecar clients
          │
          ▼
Rust supervisor
  ├── product session state
  ├── output and transcript policy
  ├── local API and authorization
  └── Ark backend adapter
          │ Jupyter channels + custom comms
          ▼
        Ark
          └── R
                ├── reticulate Python
                └── DuckDB
```

The supervisor acts as the only Jupyter client.
Browser viewers and third-party sidecars use MCP Console's local API, not Ark's sockets or comm schemas directly.

### 3.2 Evaluation performed

The Ark-backed prototype embedded Ark in the sandboxed worker and made the MCP supervisor a private Jupyter frontend.
It passed the tested core R contract after a small Ark API addition:

- persistent cells and every visible top-level result;
- R errors with source-bearing tracebacks;
- `readline()`, `browser()`, `recover()`, menus, and the tested partial and multiple LF-delimited stdin chunks through the adapter;
- worker-failure reporting and macOS sandbox restrictions;
- adapter-provided synthetic source identities retained by Ark and direct subprocess-output capture.

The prototype also confirmed that Ark can emit plot MIME data and open a Data Explorer comm without a Positron frontend.
Those capabilities were not yet exposed through MCP Console's text-only `send` result.
Ark help depended on loopback HTTP listeners that the worker sandbox denied.

The difficulty was the integration boundary rather than the correctness of Ark's R frontend.
At the inspected revision, Ark's public entry point starts a complete Jupyter kernel, while its direct console startup, callbacks, request channels, parser, and evaluator are crate-private.
MCP Console therefore had to implement connection and authentication setup, ZeroMQ control, shell, stdin, and IOPub sockets, kernel startup negotiation, Jupyter message correlation, shell and IOPub completion, stdin translation, and worker lifecycle handling.

Ark's existing modes also split two required behaviors.
Notebook mode routed an unconnected `browser()` prompt through Jupyter stdin but suppressed intermediate visible values.
Console mode emitted every visible value but expected Positron debugger handling for that browser prompt.
The prototype added an explicit Ark option combining console-mode printing with structured browser stdin.

After both prototypes were updated to the same repository baseline, each passed its branch's full `scripts/check` run.
The native worker was selected for the current slice because it implements the required behavior through a narrower private sideband, uses unmodified pinned Harp and `libr` dependencies, preserves the submitted expression in top-level task callbacks, and does not require Jupyter or ZeroMQ inside the worker.

### 3.3 Potential gains

Ark already implements difficult and useful behavior:

- R discovery, startup, event processing, and cross-platform frontend handling;
- complete-cell execution with message identity and busy/idle state;
- stdin and a high-priority control path;
- stdout, conditions, errors, source references, plots, and help;
- debugger and Variables integrations;
- retained references plus a mature Data Explorer backend and custom comm protocol;
- existing frontend behavior used by Positron and potentially reusable by Canvas or another viewer.

The Data Explorer is the strongest reason to consider Ark.
It already models the interaction MCP Console needs: retain a large object in the R session, then fetch only the rows, columns, filters, sorts, profiles, or summaries the human currently needs.

### 3.4 Costs and constraints

Ark is an R Jupyter kernel.
MCP Console still owns or must add:

- first-class R, Python, and SQL submission identity;
- Python and SQL dispatch without misleading R wrappers or source locations;
- compact MCP polling and result formatting;
- named sessions and persistent requirement manifests;
- transcript and output-spool policy;
- sandbox inheritance and process ownership;
- the process-scoped local sidecar service and browser security boundary;
- translation between Ark comms and a stable backend-neutral inspection API.

Operational costs include Jupyter connection files, ZeroMQ channels, comm lifecycle, additional sockets and threads, a broader dependency graph, version skew, and potentially unused IDE-oriented components.
Ark is valuable only if its reusable behavior outweighs those costs.

The project should not depend on an undocumented internal comm protocol without either a compatibility commitment, pinned tests, or an upstreamed stable boundary.

### 3.5 Work required for an Ark-backed route

The prototype proves that Ark can back MCP Console through its existing Jupyter architecture.
A transport-neutral runtime is not a prerequisite for that route.
The required work depends on whether MCP Console accepts the full-kernel integration or first extracts a lower-level runtime.

#### Full-kernel integration

The demonstrated full-kernel route has two Ark behavior gaps relative to the current MCP Console contract:

- Make browser-prompt routing independent of notebook versus console session mode.
  This permits console-mode printing of every visible result together with structured browser stdin.
- Preserve the submitted expression in top-level task callbacks instead of substituting `.ark_last_value`, or explicitly change MCP Console's current callback contract.

The browser-prompt option must be committed in a clean Ark revision.
The remaining upstream-versus-adapter choices are about supportability rather than missing behavior:

- Ark could provide a supported embedder bootstrap, or MCP Console could continue to own and pin the signal, logger, trap-handler, and panic setup around the existing public kernel startup.
- Ark could publish versioned Positron comm schemas, or MCP Console could pin Ark and Amalthea revisions and protect the adopted Variables or Data Explorer schema with adapter conformance tests.

Help needs a separate decision.
Ark currently serves help through loopback HTTP and a comm.
MCP Console could allow narrowly scoped loopback access and implement that comm, or Ark could expose an injectable or non-HTTP help transport.
Similarly, automatic inline Data Explorer behavior may need an Ark policy independent of notebook mode if MCP Console adopts that behavior rather than opening explorers explicitly.

Ark already provides Jupyter stream, result, error, display, comm, busy/idle, stdin, and control messages, source attribution, and an interrupt request.
The MCP Console adapter would still own:

- connection files, authentication, the control, shell, stdin, and IOPub sockets, startup negotiation, message correlation, and completion detection;
- worker-scoped stdin-stream semantics, including combined, idle, and timed-out delivery, partial-line buffering, multiple-line carryover, and persistence of unread bytes across evaluations, despite Jupyter's line-oriented replies;
- background event consumption, bounded retention, polling cursors, and translation of MIME and comm messages;
- sending and observing interrupt and shutdown requests without blocking behind the active evaluation;
- matching the current incomplete-cell contract, either by segmenting requests or through an Ark execution option;
- MCP tools, first-class R/Python/SQL request identity, named sessions, requirement manifests, sandbox supervision, transcripts, the local sidecar API, and authorization.

#### Lower-level reuse without Jupyter

Avoiding that adapter would require a larger Ark refactor.
Ark, or a shared crate extracted below it, would need to expose:

- a transport-neutral runtime entry point with host-owned request, event, stdin, and control channels;
- normalized incremental output, input, completion, stopped, crash, interrupt, and shutdown events;
- configurable debugger, plots, help, Variables, and Data Explorer components without constructing the full Jupyter and Positron frontend;
- an inherited-pipe or other host-selected IPC transport;
- a versioned runtime capability and compatibility contract.

Merely making the current `Console` construction public would not provide this boundary because its state and channels remain coupled to Ark and Amalthea services.
This extraction could reduce duplicated native-R maintenance, but it is an architectural project rather than a missing Ark capability or a prerequisite for the proven full-kernel route.

## 4. Option B: purpose-built native worker

### 4.1 Shape

```text
MCP and local sidecar clients
          │
          ▼
Rust supervisor
  └── native backend adapter
          │ small private protocol
          ▼
Rust worker using harp / libr / libR DLL REPL
          └── R
                ├── reticulate Python
                └── DuckDB
```

### 4.2 Potential gains

- the internal request model is natively R/Python/SQL rather than R-kernel-centric;
- fewer transports and less translation;
- direct control over output budgets, transcript events, sandboxing, and lifecycle;
- easier to keep the worker minimal and purpose-built;
- no dependency on Ark's Jupyter-facing process shape.

### 4.3 Costs and constraints

`harp` and `libr` are low-level building blocks, not a complete R frontend.
The implemented worker now covers:

- R discovery, loading, initialization, and main-thread ownership;
- persistent top-level cells with native visible-value, warning, `.Last.value`, traceback, and task-callback behavior;
- exact worker-scoped stdin through fd 0 for `readline()`, `browser()`, and direct readers, with delivery alongside a cell, during a timed-out evaluation, or while idle, and with unread bytes retained across evaluations;
- timeout-bounded MCP waits and later polling without stopping the active evaluation;
- private sideband output, worker lifecycle, orderly shutdown, replacement after infrastructure failure, and the macOS sandbox boundary.

The project remains responsible for:

- platform support beyond the implemented macOS sandbox;
- incremental output polling with cursors, interrupts, direct subprocess capture, virtual source filenames, and richer condition events;
- debugger behavior and explicit crash reporting beyond the current discard-and-restart worker boundary;
- plots, help, viewer content, object references, and Variables behavior;
- a robust large-data inspection backend, including formatting, filtering, sorting, profiles, staleness, and invalidation.

A simple `df[rows, columns]` helper is not feature parity with Ark's Data Explorer.
The native worker should become the full-runtime backend only if the product can intentionally support a narrower v1 or if the equivalent backend proves tractable.

## 5. Option C: shared lower-level runtime

The preferred long-term architecture is a reusable R-runtime layer below Ark's Jupyter/Positron adapters and below MCP Console's MCP/local-API adapters:

```text
shared R runtime
  ├── startup and event loop
  ├── cell evaluation and stdin
  ├── interrupt and debugger integration
  ├── plots, help, objects, and inspection
  └── normalized events

Ark                 MCP Console
Jupyter/Positron     MCP/local sidecar
```

This reduces duplicated native-R maintenance.
It may require organizational coordination or upstream work and therefore cannot block an initial prototype indefinitely.

## 6. Evidence gathered and remaining evaluation

The R-only Ark and native prototypes have been implemented and compared.
That work settled the current text R console but did not cover the full backend requirements in section 2.

### 6.1 Core R behavior evaluated

The comparison established that:

- both prototypes can run persistent complete cells, print every visible result, report ordinary R errors, and support interactive input;
- the Ark adapter supports `readline()`, `browser()`, `recover()`, and menus, and the native worker currently supports `readline()` and `browser()`;
- the MCP adapter supplies a synthetic source URI that Ark retains in source references and tracebacks, and Ark captures direct subprocess output;
- the native DLL iterator preserves R's submitted expression in top-level task callbacks and its native stack does not contain an MCP evaluation wrapper;
- the native worker applies earlier complete expressions before reporting a trailing incomplete expression, whereas Ark pre-parses and rejects the complete request;
- the current MCP product surface exposes neither backend's interrupt path.

Interrupt recovery, nested debugger behavior, incremental output, and source filenames for the native worker remain part of the next runtime slice.

### 6.2 Python and SQL

- Can Python and SQL be first-class evaluation types rather than opaque user-visible R code?
- Are source filenames and diagnostics language-native?
- Do Python `input()` and `pdb` work?
- Can DuckDB use the intended persistent R/DBI connection and live R relations?
- What R frames are truthfully visible when Python or SQL calls back into R?

### 6.3 Human sideband

From an independent process:

1. create a large R data frame;
2. obtain an opaque table reference;
3. request schema and bounded visible rows/columns;
4. sort, filter, and profile without retrieving the full object;
5. scroll repeatedly and measure latency and allocations;
6. mutate or rebind the source and verify revision/invalidation behavior;
7. characterize requests while R is busy, in `browser()`, and waiting for input;
8. create a snapshot and continue browsing it during another evaluation;
9. view plots and help through the same sidecar service.

The Ark prototype confirmed that a Data Explorer comm can open without Positron.
For a supported Ark backend, determine which remaining comm APIs and backend components can be versioned and used independently of Positron.
For the native path, implement enough equivalent typed operations to compare honestly.

### 6.4 Operations

One local Apple Silicon release-build snapshot recorded:

| Measurement              | Ark prototype | Native prototype |
| ------------------------ | ------------: | ---------------: |
| First R evaluation       |       ~0.54 s |    0.155–0.177 s |
| Silent steady round trip |    1.4–1.8 ms |   0.086–0.089 ms |
| Worker threads           |            13 |                1 |
| Total RSS                |    ~117.8 MiB |    80.6–81.0 MiB |
| Release binary           |     27.55 MiB |         5.09 MiB |

These are historical comparison snapshots, not general benchmarks.
The native measurements predate the DLL iterator and should be repeated before making a current performance claim.
They do establish the different process, dependency, and transport shape of the two prototypes.

The remaining operational evaluation must cover:

- behavior on macOS, Linux, and Windows;
- version negotiation and compatibility testing;
- clean packaging from committed dependencies;
- expected maintenance ownership.

## 7. Decision matrix

Score each candidate against the following, with evidence rather than preference:

| Criterion                                                    |   Weight |
| ------------------------------------------------------------ | -------: |
| Correct R lifecycle, stdin, debugger, and interrupt behavior | critical |
| Large live-table inspection and plot/help reuse              | critical |
| First-class Python and SQL semantics                         | critical |
| Backend can be isolated behind the normalized service        | critical |
| No unmaintainable fork or unstable dependency boundary       | critical |
| Packaging, startup, and sandbox complexity                   |     high |
| Cross-platform support                                       |     high |
| Output/transcript integration                                |     high |
| Compatibility with existing Positron/Canvas viewers          |     high |
| Implementation effort and long-term maintenance              |     high |
| Binary size and incidental dependencies                      |   medium |

## 8. Decision rule

The current text R console selects the native worker.
Ark's additional capabilities are not exposed by the implemented tool, while the full-kernel route adds Jupyter transport, requires an unpublished Ark browser-prompt option, and changes the task-callback expression.
This is not the final full-runtime decision.

Choose Ark when its mature R and sideband behavior can be reused through a stable adapter, Python/SQL can remain first-class, and the integration does not require a long-lived invasive fork.
Plots, help, debugger support, Variables, and Data Explorer are the capabilities most likely to justify reconsideration.
Before that choice, the required Ark and MCP Console work in section 3.5 must be implemented or deliberately accepted.

Choose the native worker when Ark's R-kernel assumptions, transport, dependency graph, or extension requirements dominate, and a deliberately narrower but sufficient live/snapshot inspection backend can be implemented more cleanly.

Prefer shared extraction when it is feasible without stalling delivery.

After the remaining full-runtime evaluation:

1. write `docs/adr/0001-runtime-backend.md`;
2. update `AGENTS.md`, `README.md`, and `ARCHITECTURE.md` to mark the result settled;
3. delete or quarantine the losing prototype;
4. retain backend-neutral integration tests as the product conformance suite.

## 9. Non-negotiable abstraction boundary

Regardless of selection:

- MCP clients see only the two documented MCP tools;
- sidecar clients see only the process-scoped local API;
- viewer traffic is typed inspection, not arbitrary hidden code;
- external arbitrary code is attributed and enters the transcript;
- output remains bounded before entering model context;
- runtime transport and object handles remain private implementation details;
- the local API supports both revisioned live views and immutable snapshots;
- no viewer starts or prolongs a daemon.
