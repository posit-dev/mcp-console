# Peer-runtime completion after shared Python bootstrap

This sketch records the completion boundaries for Console-owned R and Python runtimes with an optional reticulate bridge.
Implemented behavior belongs in [ARCHITECTURE.md](../docs/ARCHITECTURE.md#worker), [BUILTIN_RUNTIME.md](../docs/BUILTIN_RUNTIME.md#python), and [REQUIREMENTS.md](../docs/REQUIREMENTS.md).

## Baseline and follow-up

PR #421 introduced the shared Python requirement owner, reached-import callback, inspected candidate activation, and common NumPy, pandas, and Matplotlib defaults.
It removed the separate native requirement store and retained R declaration representation in a compatibility adapter.
It did not complete independent initialization: worker startup still initialized R, ordinary Python entered reticulate attachment, and idle tool preparation retained two paths.

The follow-up composes the ordinary runtime paths:

- Worker launch prepares Linux loader paths and private process-lifetime storage before either interpreter starts.
  R's session temporary directory does not own Python caches or SQL storage.
- Process interrupt ownership starts without R and attaches R's interrupt state without acknowledging pending requests.
  R and reticulate hooks reinstall Console's input, output, and interrupt services.
- An explicit or host-resolved Python identity enters the common CPython bootstrap directly.
  R cells, Python-side `r` access, and R-owned SQL initialize R on demand.
  Unresolved R selection callbacks and declarations remain a real dependency on R.
- Late R startup installs selection hooks before loading startup packages that could enter reticulate.
  Attachment uses the running identity and obtains only additional conversion metadata from that executable.
  Conflicting later selections require restart.
- R-first startup still adopts Python initialized by an external startup package without replaying environment activation or claiming ownership of its initialization.
- Idle tool additions, reached imports, and R declarations share the Console Python requirement and activation owner.
  R argument validation, conditions, history, provenance, string encodings, attributes, and protected lifetimes remain in the R adapter.
  The host-supplied native-only preparation variant and tool calls through `reticulate::py_require()` are removed.
- SQL retains connections in their owning runtime.
  Capability selects the default provider; initializing another runtime or attaching the bridge does not replace a selected connection or open another database.
- Prepared targets accept genuine Python absence when R is available, expose the corresponding tool schema, and reject broken explicit Python selections.
  Inspection stays on the execution host and prepared environments remain non-managed.

Interpreter initialization, common setup completion, bridge completion, resolved candidates, live environment mutation, and generation-checked server acceptance remain distinct.
Setup interruption can retry completed bootstrap stages without replacing the interpreter.
Attachment can retry before reticulate publishes its configuration; a failure in later hooks requires restart for further bridge use because arbitrary hook effects cannot be rolled back.
Partial R initialization likewise requires worker replacement.
Accepted managed activation survives a subsequent import or cell failure.

## Acceptance coverage

The shared peer-runtime exercise proves R is uninitialized before Python creates a persistent object and a selected SQLite DB-API connection containing data.
An R cell or Python-side R access then initializes R and attaches interoperability.
The exercise checks object and connection identity, database contents, executable and prefixes, cache locations, display overrides, and a once-only startup hook, then runs nested R-to-Python-to-R callbacks.
The same exercise covers direct and sandboxed execution and a real SSH target with poisoned controller discovery commands.
PID continuity alone is not the assertion.

The existing suites retain reverse initialization order, R-only and Python-only execution, external-startup adoption, managed import and tool additions, pre-mutation rejection, unsafe activation, accepted publication after cell failure, generation replacement, stdin cancellation, interrupts, callback servicing, plotting, and attachment retry.
Prepared-target cases additionally exercise R-only discovery and invalid explicit Python configuration.
Real Docker and Docker Sandbox execution remains capability-gated; a missing local provider is a validation gap rather than an alternative runtime design.

## Execution-thread constraints

Both runtimes retain the existing serialized worker thread.
R APIs and protected R objects are bound to the initializing thread.
CPython calls need the appropriate thread state and GIL, while saved initial thread-state restoration and interpreter lifetime remain Console-owned.
Signals and environment variables are process-wide, and synchronous cross-language callbacks can re-enter both runtimes.
Runtime handles are copied out of Rust state before interpreter calls; locks and mutable state borrows do not span those calls.

Late R startup with concurrent native environment access remains unresolved.
The current implementation temporarily changes `R_DEFAULT_PACKAGES`, and R's bootstrap also mutates the process environment before an embedder can install hooks.
A safe replacement needs an R embedding startup interface that accepts package selection and bootstrap environment values without those mutations, followed by removal of Console's late environment writes.
A native interposer that rejects environment writes after Python startup, combined with the existing startup-package, object-continuity, and nested-callback exercises, is the required regression boundary.
Moving either runtime to another thread would not solve this process-wide constraint.

Separate runtime threads and SQL-only operation remain future work and are not completion criteria here.
Reticulate schedules R callbacks from Python background services onto the main thread and waits for their results.
A future thread split needs a reentrant callback protocol, admission and cancellation rules, GIL-release boundaries, and shutdown ordering that cannot deadlock while one runtime waits on the other.
No scheduler abstraction or extra runtime thread is introduced by this follow-up.
