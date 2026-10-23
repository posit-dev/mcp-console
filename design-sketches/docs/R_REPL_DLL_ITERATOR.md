# Native R DLL-REPL iterator

Status: implemented by the current native worker.
The final section records the implementation sequence used for the change.

## Decision

For the current native-worker implementation, use R's public embedding pair `R_ReplDLLinit()` and `R_ReplDLLdo1()` to run each submitted R cell.
The eventual full-runtime worker backend remains an open design decision.

Rust continues to own the worker process, private sideband, cell source, interactive stdin, and output messages.
A small C function owns the live DLL-REPL frame for one cell.
This is the only C code needed: R errors may long-jump back to the context installed by `R_ReplDLLinit()`, so the call to `R_ReplDLLinit()` and every call to `R_ReplDLLdo1()` must remain under one C caller.

This replaces the prior combination of:

- pre-parsing the complete cell with Harp;
- evaluating each parsed expression manually with `Rf_eval()`;
- installing a calling error handler to synthesize `.Traceback`;
- passing each value back through a generated `base::get()` expression so the DLL REPL performs auto-print, warning, `.Last.value`, and task-callback work;
- resetting the DLL REPL after nested console input.

The selected design lets R perform its own parse, eval, print, warning, traceback, and top-level callback work.

## Sources inspected

These findings are based on the local checkouts:

- R source `0ee275848b67`;
- `posit-dev/mcp-repl` `e60f582d9437`;
- Ark/Harp `37d3db0074e0`, which is also the revision used by this package.

Relevant R sources are:

- `src/include/Rembedded.h`: the public experimental embedding API;
- `src/main/main.c`: `Rf_ReplIteration()`, `R_ReplDLLinit()`, and `R_ReplDLLdo1()`;
- `src/main/errors.c` and `src/main/context.c`: top-level error handling and non-local transfer;
- `doc/manual/R-exts.texi`: the documented pseudo-console loop.

## Why not reuse Ark's console?

An Ark-backed prototype was implemented and evaluated, rather than rejected from source inspection alone.
It embedded Ark in the sandboxed worker and translated Ark's Jupyter messages over private ZeroMQ sockets into the MCP `send` protocol.
Persistent cells, every visible top-level value, errors, source-bearing tracebacks, `readline()`, `browser()`, `recover()`, menu input, and the tested partial and multiple LF-delimited stdin cases all worked.
Ark also captured direct subprocess output, emitted plot MIME data, and opened a Data Explorer comm without a Positron frontend.

At the inspected revision, however, Ark does not expose a narrow, transport-neutral console service.
Its public startup path constructs a complete Jupyter kernel.
Its lower-level console construction and evaluation machinery is not separately reusable: its callbacks, request channels, parser, and evaluator are coupled to Amalthea, IOPub, comms, graphics, help, LSP, DAP, and Ark's global console state.
Using Ark therefore required MCP Console to implement a Jupyter frontend covering connection setup, kernel startup, shell, IOPub, stdin and control channels, message correlation, and completion detection.
Its ZeroMQ Unix sockets also required a worker-sandbox exception that the inherited sideband does not need.

The prototype needed a small Ark API addition to combine console-mode visible-value printing with structured stdin for an otherwise unconnected `browser()` prompt.
Ark's stdin API treats each reply as a complete line and adds a newline, so the MCP adapter still had to buffer the tested partial and multiple LF-delimited chunks before passing complete lines to Ark.
Ark also pre-parses the complete cell, calls `Rf_eval()` directly, and sends an internal `base::.ark_last_value` expression through R's REPL to finish top-level processing.
Consequently, a top-level task callback sees that proxy rather than the submitted expression.
That is the same pre-parse, direct-eval, and value-proxy machinery that the DLL iterator removes.

The current worker therefore reuses the lower-level Harp and `libr` crates from the Ark repository for R discovery, loading, initialization helpers, and API bindings.
MCP Console owns the narrower cell/stdin protocol and uses libR's DLL-REPL API for evaluation.
This decision is limited to the current native worker; it is not a permanent rejection of Ark.

Reconsider Ark when plots, help, interrupts, debugger support, Variables, or Data Explorer enter the implemented surface and can be consumed through a stable adapter.
[`RUNTIME_BACKEND.md`](RUNTIME_BACKEND.md) records the required Ark-side changes, the MCP Console adapter work, and the optional lower-level extraction.

## Approaches considered

### Pre-parse and call `Rf_eval()`

This was the implementation before the DLL-REPL iterator.
It gives the host exact expression boundaries and makes cell EOF easy to classify, but the host must manually reconstruct console behavior around `Rf_eval()`:

- capture visibility and auto-print values;
- update `.Last.value`;
- print accumulated warnings at the correct boundary;
- invoke top-level task callbacks;
- preserve native traceback and error behavior;
- recover from R long-jumps;
- distinguish the DLL REPL's private proxy reads from genuine `ReadConsole` requests.

The prior value-proxy technique delegated some of that work back to `R_ReplDLLdo1()`, but changes observable semantics: top-level task callbacks receive the generated `base::get()` call rather than the submitted expression.
It also requires separate traceback and REPL-reset machinery.

### Run `run_Rmainloop()` with a simple source queue

`mcp-repl` installs console callbacks and hands the runtime thread to `run_Rmainloop()`.
This is a suitable balance for its readline-oriented input protocol: all submitted text enters one console queue, and its input batching normalizes partial input by adding a newline.

MCP Console has two distinct operations:

- a complete code cell; and
- an exact worker-scoped stdin stream that may be queued with a cell, during a timed-out evaluation, or while the worker is idle.

A custom `ReadConsole` can feed both operations to the full main loop, but the public callback surface does not reveal whether a primary read is for a new expression or a continuation of an incomplete expression.
Both reads call `R_Busy(0)`.
The distinguishing state is the private `R_ReplState.prompt_type`; the callback receives only its rendered prompt.

Consequently, a full `run_Rmainloop()` implementation would need one of:

- comparison with `options("continue")`;
- a sentinel expression or another protocol embedded in the source;
- a copy of R's private REPL state and parser loop;
- weaker complete-cell EOF semantics.

Those choices add more coupling or ambiguity than the DLL stepping API.

### Call `Rf_ReplIteration()` directly

`Rf_ReplIteration()` is the newer implementation behind R's main console loop, but it is `attribute_hidden`.
Its `R_ReplState` argument is declared privately inside `src/main/main.c`.
`Rf_ReplConsole()` is also private.

Neither is an embedder API.
Copying them would make MCP Console responsible for R's private parser state and for tracking changes across R releases.

### Step with `R_ReplDLLdo1()`

`R_ReplDLLinit()` and `R_ReplDLLdo1()` are declared in `Rembedded.h`, documented for pseudo-console embedders, and exported by libR.
`R_ReplDLLdo1()` returns enough state for a complete-cell boundary:

- `1`: the parser is back at a primary prompt;
- `2`: the parser needs continuation input;
- `-1`: `ReadConsole` returned EOF.

The host can therefore feed the cell until its source queue is empty.
EOF after status `1` completes the cell; EOF after status `2` is an incomplete-source language error.
No prompt comparison or source pre-parse is required.

R's source notes that this older DLL implementation can drift from `Rf_ReplIteration()`.
That is a reason to keep the adapter small and to retain behavioral tests, not a reason to copy the private implementation.

## Native semantics supplied by `R_ReplDLLdo1()`

For each successfully parsed top-level expression, R:

1. evaluates the actual parsed expression in `R_GlobalEnv`;
2. records the value as `.Last.value`;
3. auto-prints it when visible;
4. prints collected warnings;
5. calls top-level task callbacks with the actual expression, value, success, and visibility.

A parse, evaluation, auto-print, or task-callback error follows R's normal top-level error path.
With `R_Interactive` true, that path prints the error, records native traceback state, unwinds R contexts, and jumps to the context installed by `R_ReplDLLinit()`.

MCP Console therefore does not need `harp::try_catch()`, `harp::top_level_exec()`, a calling error handler, a value proxy, or custom traceback capture around submitted code.

## C-owned cell boundary

The C shim receives the already-resolved libR function pointers.
It does not include R headers or link directly to libR.
The implementation is intentionally kept in [`src/r_repl.c`](../../src/r_repl.c) rather than repeated here.
It keeps `R_ReplDLLinit()` and every `R_ReplDLLdo1()` call beneath the C boundary.
A `noinline` helper sets a volatile `sig_atomic_t` marker only after `R_ReplDLLdo1()` returns normally; after an R long-jump, the marker remains clear and the shim reports status `0`.

Return values to Rust are:

- `0`: R performed a top-level long-jump; R has already printed the language error;
- `1`: source EOF at a complete primary boundary;
- `2`: source EOF while the parser requires continuation input.

The volatile marker is deliberately the only C state consulted after a long-jump.
The shim must not depend on Rust destructors, Rust unwinding, or modified automatic C locals surviving that transfer.
Rust callbacks invoked from R must return normally and must not panic.

`R_ReplDLLinit()` runs once per submitted cell.
It resets the DLL parser, console buffer, prompt state, and top-level jump target.
It does not reset `.GlobalEnv`, so session state persists.

## Routing source and stdin through `ReadConsole`

The callback must distinguish a top-level DLL-REPL source read from a console read made while the expression is evaluating.

`R_ReplDLLdo1()` provides a usable signal:

1. before a primary or continuation source read, it calls `R_Busy(0)`;
2. after parsing a complete expression and immediately before evaluation, it calls `R_Busy(1)`.

Rust maintains an `evaluation_started` latch:

- the C shim clears it immediately before each outer `R_ReplDLLdo1()` call;
- the `Busy` callback sets it on `R_Busy(1)`;
- `R_Busy(0)` does not clear it.

The `ReadConsole` callback then routes:

- latch false: return the next logical line from the submitted cell;
- latch true: report the input request and read exact bytes from worker fd 0.

Ignoring `R_Busy(0)` after evaluation begins is required for `browser()`.
Browser input runs a nested native REPL, and that nested loop calls `R_Busy(0)` before each browser prompt.
Clearing the latch there would incorrectly route browser commands to the exhausted cell-source queue.

One boundary remains: parse errors occur before `R_Busy(1)`.
If a user `options(error=...)` handler entered by a parse error requests console input, the public DLL API exposes no state that distinguishes that read from a cell-source read.
Interactive reads from parse-error handlers are unsupported.
Evaluation, auto-print, and task-callback error handlers occur after `R_Busy(1)` and route input normally.

The server writes each stdin string to worker fd 0 without adding a newline.
The callback reads through one newline or the supplied R buffer and leaves later bytes in the stream.
Payload end is not EOF, and unread bytes remain available after the outer cell completes or errors, including to direct fd-0 readers or a later evaluation.
Cell source remains a sideband evaluation field rather than entering that stdin stream, even though both source and console input eventually pass through R's callback.

Cell source receives a final newline internally when it does not already have one, matching the line contract expected by `ReadConsole`.
The original MCP value is not modified.

`ReadConsole` has an `fgets()`-like contract for logical lines longer than its fixed buffer: return successive chunks without a newline, then include the newline in the final chunk.
The source queue follows that contract while preserving UTF-8 character boundaries.

## Error and shutdown behavior

The existing private protocol distinction remains:

- R language failures are successful tool operations with `isError: false`;
- worker startup, sandbox, process, and sideband failures are tool errors.

An infrastructure failure discards that worker generation.
A later evaluation may lazily start a fresh worker without implying that its in-memory state survived.

On a native R long-jump, the console output already contains the R error.
The worker follows it with the same `completed` boundary used for successful cells.
An incomplete cell is the one error synthesized by the host, because DLL EOF returns `-1` instead of producing R's full-console `unexpected end of input` error.

Shutdown closes worker fd 0, so a `ReadConsole` callback blocked there returns EOF and records shutdown while the server independently attempts the sideband shutdown message.
The C/Rust boundary exits at the next safe return or long-jump.
Interrupting active R computation remains out of scope.

## Observable changes

The DLL-REPL design intentionally changes two prior behaviors:

- Cell execution is streaming rather than atomic with respect to parsing.
  If a cell assigns `answer <- 41` and then ends with `answer + (`, the assignment remains applied before the incomplete-source result.
- Top-level task callbacks receive the submitted parsed expression rather than an internal `base::get()` value-proxy expression.

It preserves:

- persistent global state;
- every visible top-level value;
- native warning and `.Last.value` behavior;
- useful native tracebacks without an MCP evaluation wrapper;
- `readline()` and `browser()` suspension and resumption;
- exact partial and multiple stdin lines;
- separation of MCP stdio from worker console output.

## Implementation plan used

1. Change public acceptance tests first:
   - assert that an assignment before a trailing incomplete expression remains applied;
   - assert that a top-level task callback sees the submitted expression and not `base::get()`;
   - assert that a logical source line can span several `ReadConsole` buffers;
   - retain the existing visible-value, warning, `.Last.value`, error, traceback, auto-print failure, `readline()`, browser, fd-0 ordering, unread-input persistence, worker-restart, and shutdown coverage.
2. Confirm the changed tests fail against the prior pre-parse/proxy worker.
3. Add a macOS C shim and compile it with a small `build.rs`.
4. Keep the current runtime lookup of `R_ReplDLLinit()` and `R_ReplDLLdo1()`, but pass those pointers to the shim instead of invoking them from Rust.
5. Add distinct cell-source state and the `evaluation_started` Busy latch.
6. Change `ReadConsole` to route primary/continuation reads to cell source and evaluation-time reads to the worker fd-0 path.
7. Replace the Rust worker's manual evaluation loop with one shim call per `Evaluate` message and classify its three return values.
8. Delete the pre-parser, direct eval wrapper, calling error handler, traceback capture, value proxy, DLL proxy input, proxy-name generator, and manual REPL-reset code.
9. Update `README.md`, root `AGENTS.md`, and conflicting intended-architecture text to describe the implementation that exists.
10. Run the focused acceptance tests, then `scripts/check`.
