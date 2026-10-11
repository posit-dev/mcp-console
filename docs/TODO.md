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

## Graphics coverage

**GR-1 — Deferred device-reuse coverage and filename collision (unselected; not a release blocker).** A future public-API case should create a managed plot, close its device, and open an explicit user PNG in the same slot within one cell.
Exercise both a distinct temporary filename template and the original managed template read from `.Devices`.
The same-template case currently makes cleanup close the explicit device; preserving it would require ownership identity that distinguishes separate device openings with the same number and template.
For the distinct-template case, verify the number is reused, cell cleanup leaves the user device open and uncaptured, and drawing and explicit closure work in a later cell.
These cases cover the [managed-device cleanup boundary](ARCHITECTURE.md#why-native-r-graphics-callbacks).

## Sandbox and platforms

**SP-1 — Review evidence for macOS policy exceptions.** Check each application extension against the actual sandboxed workflow and selected native pin.
Distinguish historical evidence from speculation and inherited runner allowances.
Preserve meaningful regressions, including real-library process creation; resolver success is not worker-sandbox evidence.
See [Sandbox](SANDBOX.md#policy-extensions-and-compatibility).

**SP-2 — Track managed UDP and Unix-socket capability gaps.** Console rejects `tcp_udp` on macOS/Linux and path-specific Unix-socket lists on Linux.
Historical docs recorded an upstream UDP reply-routing issue.
Verify the selected companion's current implementation and executable behavior before changing admission or documentation.
A schema accepting a native option is not proof of usable routing.
See [native capabilities](SANDBOX_CONFIGURATION.md#native-capabilities).

**SP-3 — Reduce Windows fixture-only exclusions.** Port shell/shebang, FIFO, and Unix-layout fixtures for otherwise supported admission, resolver, client, and lifecycle behavior.
Keep genuinely OS-specific contracts on their owning platform.
Native Windows acceptance does not cover every skipped shared case.
Audit through full-suite capability diagnostics; see [Windows parity](WINDOWS.md#testing-parity-and-remaining-gaps).

**SP-4 — Recheck development-Python compatibility.** The development guide records isolated `sitecustomize` fixture incompatibility with Python 3.14.
Reproduce against current tooling before removing the tested Python 3.13 recommendation.
This concerns the test runner, not a blanket claim that Console cannot embed later Python versions.

## Documentation and agent usability

**DX-1 — Measure tool-description changes with agent evaluations.** This documentation refactor does not change advertised tool prose.
A later pass should compare discovery, language choice, correct polling/input, dependency preparation, recovery, and token cost against the same scenarios.
Smaller prose alone is not evidence of better behavior.
See [Tool descriptions](TOOL_DESCRIPTIONS.md).

**DX-2 — Keep defaults and reference links tied to their owners.** This pass found stale Python-default prose and companion-pin references.
A lightweight regression/check for important defaults and pin-specific links is a possible follow-up.
Do not build a general documentation-generation framework without a demonstrated need.

DX-2 is an editorial follow-up proposed by this pass; it is not a previously committed implementation task.
Updating completed entries and links should be part of the change that resolves them.

## Unselected proposals, not release blockers

The [design archive](../design-sketches/README.md) describes viewers, a sidecar API, typed object/table inspection, named sessions, separate runtime threads, and SQL-only execution without either language runtime.
Several sketches also assume old compute targets or an R-hosted Python design.

These are possible directions, not supported features or implied commitments.
Do not copy their old implementation checklists into the active backlog.
Select a goal, reconcile it with current architecture, and create a scoped issue before implementation.

Documented native limits—runner-loss cleanup, inherited procfs visibility, mount masking, unrestricted/external-mode authority, and Windows host preparation—remain limits unless separately selected for work.
They are not hidden promises of stronger guarantees.
