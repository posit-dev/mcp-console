# Open work and design questions

This page collects unfinished work and deferred questions from the documentation.
It is not a release scope commitment, a second issue tracker, or a claim that every limitation needs implementation.
Check the source, applicable tests, and tracker before starting work; add an issue link when an item is selected.

The initial inventory comes from the documentation reviewed at `b9caf7c55e9f9fbb0ce33582e4c61d615813b4eb`.
Status below distinguishes verification, known limitations, and unselected proposals.
No priorities, owners, or deadlines have been invented.

## Release verification

**RV-1 — Verify the resolver boundary at the actual release candidate.** The [historical audit](RESOLVER_RELEASE_AUDIT.md) ended pending final-head CI and used an older companion pin.
Run the relevant trust, cancellation, result-handoff, and cleanup cases on the candidate's supported native platforms.
Attach exact Console/companion revisions and CI evidence.
Do not treat a historical NO-GO as permanent current status or an old pass as current approval.

**RV-2 — Complete native Linux floor rehearsal.** Earlier controlled-Jammy results do not replace a release-candidate rehearsal on both native architectures, including R-free/R-present installed wheels, the bundled helper, loader/ABI validation, and bounded shutdown.
Follow [Release](../RELEASE.md#linux-floor-validation).
This is a release checkpoint, not a request to redesign packaging.

## Runtime and resource limits

**RT-1 — Resolve or explicitly bound late R startup's process-environment risk.** The [peer-runtime sketch](../design-sketches/peer-runtime-completion.md#execution-thread-constraints) records environment mutation during late R initialization while native Python services may already run.
Establish the remaining behavior on current source before choosing a fix.
A safe design needs an appropriate R embedding interface or a justified restriction, with startup/object/connection continuity and reentrant-callback regressions.
Moving runtimes to separate threads does not by itself fix process-wide environment mutation.

**RT-2 — Decide whether internal frames and queued stdin need limits.** [Worker protocol](WORKER_PROTOCOL.md#launch-and-transport) has no general sideband frame-size or stdin-queue cap.
Public output budgets do not bound these allocations.
Any selected change must define rejection, streaming/backpressure, cancellation, and compatibility behavior at the owning boundary.

**RT-3 — Decide recording retention and retrieval policy.** [Recordings](RECORDING.md) have no aggregate retention quota, automatic cleanup, built-in read tool, or redaction.
Per-output caps are not a total disk budget.
Choose a concrete user need before adding cleanup or artifact APIs; protect active sessions and user-managed files.
Current documentation must continue to state the limits.

**RT-4 — Remove the managed R DuckDB streaming adapter when the driver supports incremental data frames.** The [temporary workaround](ARCHITECTURE.md#managed-duckdb-streaming-workaround) keeps Arrow fetching and scratch conversion internal.
Once the supported DuckDB R versions include [upstream streaming support](https://github.com/duckdb/duckdb-r/pull/2587), use the ordinary DBI retrieval path and delete the adapter and Arrow-specific retained-result branches.
Preserve the existing public data-frame, row-limit, preview, and cleanup contracts; exact preview formatting has a separate use for the scratch catalog.

## Sandbox and platforms

**SP-1 — Review evidence for macOS policy exceptions.** Check each application extension against the actual sandboxed workflow and selected native pin.
Distinguish historical evidence from speculation and inherited runner allowances.
Preserve meaningful regressions, including real-library process creation; resolver success is not worker-sandbox evidence.
See [Sandbox](ARCHITECTURE.md#policy-extensions-and-compatibility).

**SP-2 — Track managed UDP and Unix-socket capability gaps.** Console rejects `tcp_udp` on macOS/Linux and path-specific Unix-socket lists on Linux.
Historical docs recorded an upstream UDP reply-routing issue.
Verify the selected companion's current implementation and executable behavior before changing admission or documentation.
A schema accepting a native option is not proof of usable routing.
See [native capabilities](SANDBOX_CONFIGURATION.md#native-capabilities).

**SP-3 — Reduce Windows fixture-only exclusions.** Port shell/shebang, FIFO, and Unix-layout fixtures for otherwise supported admission, resolver, client, and lifecycle behavior.
The CLI resolver inventory/manifest fixture now uses a native executable, and CLI configuration-conflict and startup-validation cases no longer require POSIX.
Warmup cases for missing or unusable R and interactive input also run on Windows.
Other fixture exclusions remain.
Keep genuinely OS-specific contracts on their owning platform.
Native Windows acceptance does not cover every skipped shared case.
Audit through full-suite capability diagnostics; see [Windows parity](WINDOWS_DEVELOPMENT.md#testing-parity-and-remaining-gaps).

**SP-4 — Move development and CI to Python 3.15 when DuckDB wheels are available.** DuckDB 1.5.6 has no CPython 3.15 wheels.
The complete test suite uses Python 3.14 so SQL fixtures can install their native provider without building it from source.
This concerns the test runner, not a blanket claim that Console cannot embed later Python versions.

## R client dependencies

**RC-1 — Resolve ellmer's overlapping S7/rlang imports.** With ellmer 0.5.0 and the S7 revision pinned in `r/DESCRIPTION`, `loadNamespace("ellmer")` warns that `rlang:::=` replaces `S7:::=`.
Ellmer imports both namespaces in full; its upstream namespace needs to select one binding explicitly before Console can adopt a warning-free dependency combination.
The R integration tests pass with this warning, which remains visible rather than suppressed in the adapter or tests.
See the [R package installation notes](../r/README.md).

## Documentation and agent usability

**DX-1 — Measure tool-description changes with agent evaluations.** This documentation refactor does not change advertised tool prose.
A later pass should compare discovery, language choice, correct polling/input, dependency preparation, recovery, and token cost against the same scenarios.
Smaller prose alone is not evidence of better behavior.
See [Tool descriptions](TOOL_DESCRIPTIONS.md).

Delete completed entries and empty sections as part of the change that resolves them, and update related links.

## Unselected proposals, not release blockers

An opt-in Git-aware write policy and a separate project-configuration trust mechanism remain future design questions.
Resolve project authorization independently of Git before making workspace writes more automatic.

The [design archive](../design-sketches/README.md) describes viewers, a sidecar API, typed object/table inspection, named sessions, separate runtime threads, and SQL-only execution without either language runtime.
Several sketches also assume old compute targets or an R-hosted Python design.

These are possible directions, not supported features or implied commitments.
Do not copy their old implementation checklists into the active backlog.
Select a goal, reconcile it with current architecture, and create a scoped issue before implementation.

Documented native limits—runner-loss cleanup, inherited procfs visibility, mount masking, and unrestricted/external-mode authority—remain limits unless separately selected for work.
They are not hidden promises of stronger guarantees.
