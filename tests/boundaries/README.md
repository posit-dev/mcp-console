# Boundary tests

Tests capture behavior at process interfaces.
Use the outermost boundary that can observe a regression; private-boundary tests should cover their seam rather than repeat public result text.

| Boundary        | Contract                                                            |
| --------------- | ------------------------------------------------------------------- |
| `client_server` | MCP negotiation, tool schema, results, errors, and delivery.        |
| `server_relay`  | Private JSONL framing, ordering, and generation ownership.          |
| `relay_worker`  | Worker sideband, standard streams, EOF, crash, and shutdown.        |
| `cli`           | Arguments, exit status, signals, terminals, and sandbox guarantees. |

Security and liveness cases may need causal or process assertions in addition to a snapshot.
Do not assert incidental internal sequencing.
Keep a combined case when the interaction itself is a plausible failure mode.

## Find a contract's test area

Start with a behavior below and search its test areas.
Use `scripts/test --full --list` to discover exact suites and cases, then `scripts/test --locate SELECTOR` to reach source and snapshots; see [Running cases](#running-cases).
See [Fixtures](#fixtures) for fixture and harness guidance, and the [architecture source map](../../docs/ARCHITECTURE.md#where-to-look-in-source) for production ownership.
These areas provide representative transcript coverage; [case capabilities](#requirements-and-execution-modes) determine applicability.
For native Windows acceptance, use the [Windows validation guide](../../docs/WINDOWS.md#validation).

| Behavior                  | Test areas                                                                                               |
| ------------------------- | -------------------------------------------------------------------------------------------------------- |
| Admission/delivery        | [MCP server](client_server/server/), [MCP output](client_server/output/)                                 |
| Worker lifecycle          | [MCP lifecycle](client_server/lifecycle/), [relay/worker lifecycle](relay_worker/lifecycle/)             |
| Sideband/framing          | [CLI relay](cli/relay/), [server/relay failures](server_relay/failures/)                                 |
| Output retention/previews | [MCP output](client_server/output/)                                                                      |
| Preparation               | [MCP requirements](client_server/requirements/), [server/relay requirements](server_relay/requirements/) |
| Recording                 | [MCP recording](client_server/recording/)                                                                |
| Sandbox guarantees        | [CLI sandbox](cli/sandbox/), [MCP sandbox](client_server/sandbox/)                                       |

## Running cases

```sh
scripts/test --full --list
scripts/test --stress --list
scripts/test --locate client_server/server/test_tools
scripts/test client_server/server/test_tools::initializes_and_lists_tools
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

A selector names `BOUNDARY/SUITE` or `BOUNDARY/SUITE::CASE`.
Default runs use the explicit smoke profile; `--full` includes every functional capability-applicable case.
`--stress` runs the three allocation scale cases with their original workloads and ceilings.
Short recovery cases in `--full` check delivery, recording, and image aggregation without claiming bounded allocation growth.
Selectors retain their scope under every profile.
See the [validation ladder](../../docs/DEVELOPMENT.md#validation-ladder) for the full workflow, installed-binary override, and build ownership.

Cases run in separate processes.
`--jobs N` controls concurrency; `--timeout
SECONDS` changes the default 600-second case deadline, including snapshot work.
On cancellation or timeout, the supervisor requests cleanup and allows 15 seconds before forcibly killing the case.
Fixtures must retire their own subprocesses.
Unix case supervision cannot guarantee descendant cleanup; Windows cases additionally use kill-on-close Jobs and confirm empty Jobs before deleting workspaces.
Runner progress and rerun messages are UI output, not captured protocol records.

Locally and in CI, case failures are collected while the remaining cases continue.
After more than 15% of the selected, capability-applicable cases fail, the runner stops starting new cases and lets active cases finish under their existing deadlines.
The final failure report lists the failed selectors, their diagnostics, and how many cases were not started; any failure makes the command fail.
Execution modes count as one case, and the initialization case is included in the total.
User cancellation still interrupts active cases and waits for their cleanup.

## Requirements and execution modes

Declare capabilities beside cases with `@requires(...)` from `support.requirements`; platform detection belongs in test support.
Use explicit execution fixtures:

```python
@executions(DIRECT, SANDBOXED)
def test_persistent_state(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(r="answer <- 42")
        client.send(r="answer")
        return client.finish()
```

`DIRECT.serve()` supplies `--no-sandbox`; `SANDBOXED.serve()` uses the default sandbox.
Use one mode for ordinary language and representation assertions.
`RUNTIME` selects the sandbox on macOS/Linux and direct execution for shared Windows cases; native Windows acceptance covers its sandbox policy.
Keep both modes for permissions, inherited environment, cache placement, executable selection, process cleanup, and other mode-dependent contracts.
Keep representative direct conformance for each runtime, plots, input, restart, and recording.
Real-library sandbox workloads, including sklearn/joblib, retain their distinct APIs and initial/live activation scenarios.
There is no runner `--execution` flag.
Keep sandbox-only arguments in sandbox fixtures and policy contracts in the owning boundary's `sandbox/` directory.
Ordinary runtime cases stay with their subject even when sandboxed.

Available modes run sequentially within one case deadline and compare against one snapshot.
During updates, the first mode writes and later modes must match, not overwrite differences.
Use `@execution_snapshots` from `support.snapshots` when the case owns differing execution behavior or captured launch policy: direct keeps `CASE.yaml`, sandbox uses `CASE.sandbox.yaml`, and both modes update and verify their own records.
Fixture write grants alone do not require execution-specific snapshots.
Unavailable modes report a skip, not validation.
Test-host requirements such as Linux process-observation facilities do not imply the same runtime requirements.
Windows full checks run shared direct cases in addition to native acceptance.
Declare `SQL` for SQL runtime cases and `POSIX` for Unix-only shell/FIFO fixtures explicitly; fixture exclusions are remaining parity debt, not evidence that the supported runtime behavior is unavailable.
Also declare `R` when a SQL case uses R fixtures or cells, or asserts R-provider output; Python-backed SQL and custom-worker cases remain available without R.
The shared sandbox mode uses Seatbelt/bubblewrap fixtures; Windows native sandbox acceptance owns Windows policy coverage.

Each case uses a temporary workspace and private `MCP_CONSOLE_HOME`, preserving `HOME` and the caller's R/Python/uv environment.
Home-discovery cases supply their environment explicitly with `use_home_configuration=True`; remove inherited `MCP_CONSOLE_HOME` when testing the default home location.
Remove fixture-owned directories only after processes exit.

## Snapshots

A `test_` function returns a `Transcript`.
Its YAML 1.2 snapshot lives at `tests/snapshots/BOUNDARY/SUITE/CASE.yaml`.
`TranscriptWithCompanions` adds named siblings: YAML companions compare as values; Markdown and Quarto compare as exact UTF-8 text.
Wrap additional MCP transcripts in `McpTranscript` from `support.records` to normalize request IDs and apply the primary's exact canonical-handshake comparison.
Ordinary YAML companions retain protocol IDs.
Suite paths with an underscore-prefixed component are not discovered.

Never edit snapshots by hand.
Regenerate intentional changes with `scripts/test --update SELECTOR`, review them, and rerun without `--update`.
CLI help snapshots use an 80-column width with all standard streams detached from the caller's terminal.
Only a full unscoped run audits orphan snapshots; a successful full update can remove them.
Focused updates preserve unselected snapshots, and skipped cases retain their snapshots even during full updates.
Prefer one shared snapshot across platforms.
First make fixture output deterministic (for example, write explicit LF bytes), use declared capability requirements for separate SQL or native-policy cases, and normalize incidental paths or executable suffixes after checking the actual output.
Do not duplicate an otherwise portable case because its handshake, unrelated dependency defaults, or text presentation differs.
Keep assertions on the behavior the case owns; do not weaken them to make snapshots match.

Use `@platform_snapshots("win32", reason="...")` from `support.snapshots` only when the case tests the exact differing platform contract, such as Windows tool presentation, native interpreter identity, or an OS filesystem diagnostic.
The required reason identifies that contract.
Preserve complete errors, tracebacks, status codes, and signal distinctions; normalization must not erase a failure.
Canonical handshakes still compare in full against the applicable platform reference before compaction, so ordinary execution-mode transcripts can share a snapshot.

Windows companions use `.win32` before the execution suffix.
Successful updates, including focused updates, prune obsolete snapshots and companions only for updated cases.
Removing a platform declaration retires its variants even when updating on another host; this does not require that platform's runner.
Updates preserve unselected/skipped cases, unrun declared execution modes (including canonical handshakes), other declared platforms, and shared references owned by cases that still declare a platform variant.

Preserve complete errors, tracebacks, output, and meaningful ordering.
Normalize only incidental values such as temporary paths.
Use mappings for data, but retain serialized JSON when quoting or escaping is the contract; code and literal output remain strings.
The case chooses the representation, not the serializer.

Narrow exceptions require stronger evidence, not weaker assertions:

- When requirements inspection supports a Python lifecycle case, check the complete text against `structuredContent`, assert the owned Python declaration, and record those fields as evidence.
  Keep complete inventories in requirements/defaults cases, and never project an error this way.
- Native-runtime fidelity cases may compare complete output and conditions with a live reference, then record the verified comparison.
  Remove only explicitly irrelevant frontend differences, such as Rscript's `Execution halted` footer.
- Bounded MCP overflow previews remain literal strings in the output-limit and preview cases.
  Assert the actual response's UTF-8 byte budget and exact emitted head/tail and omission accounting before normalizing fixture-owned paths.
  Keep full raw-stream byte assertions separate, including Unicode and newline bytes; a response preview is not a raw recording.
- Other synthetic stress output may use `support.evidence.compact_text()` after full assertions.
  Its literal text and repeat/count entries are lossless; retain diagnostics, boundaries, omissions, paths, images, and final states.

`transcript_normalization` is harness metadata, never a wire field.
MCP request IDs and the invariant JSON-RPC version are abbreviated only after validation.
Cancellation targets retain matching labels on the request and notification; IDs without a recorded request remain literal.

### Canonical handshake

Generally keep one `mcp-console` invocation per YAML transcript.
Include initialization once near the top, normally via a matching canonical `!same-as` reference; use separate transcript files for additional invocations.
Client/server transcripts should generally retain the complete `initialize`, `notifications/initialized`, and `tools/list` exchange before ordinary calls.
A worker restart within the same Console invocation stays in that transcript.
Launch-rejection or protocol-failure cases may have no handshake or an incomplete exchange; record what occurred.

`client_server/server/test_tools::initializes_and_lists_tools` owns full handshake snapshots and their configured/direct/bare/runtime variants.
Update it before other affected cases.
The runner compares the complete exchange before replacing an exact match with `!same-as`; the tag records that comparison and does not load a file.
Different or incomplete handshakes remain in full.
The canonical case owns mode-specific companions; cases that test different captured policies declare separate execution snapshots.
After response assertions, `McpClient.finish()` normalizes CLI fixture write roots in tool descriptions to `<writable-root>`; it preserves the grant and leaves other output untouched.
The canonical writable-root companions retain that policy shape for exact handshake comparison.
After that comparison, shared execution transcripts use the corresponding base variant's label for fixture write grants; execution-specific transcripts retain the writable-root variant label.

## Fixtures

Use `McpClient` as a context manager, then `initialize_and_list_tools()`, `send()`, and `finish()`.
For overlapping calls, use `start_send()` and `receive()` / `receive_many()`; responses are matched by request ID.
Use `finish_with_standard_error()` when diagnostics are part of the contract.

Shared capability, execution, client, snapshot, checkpoint, and process helpers live in `tests/support/`.
Each boundary's `_harness.py` owns its concrete launch and capture mechanics.
Keep large fixture programs in searchable files under `tests/fixtures/`; see [authoring](AUTHORING.md) for examples and causal gates.

For namespace PIDs, use `support.processes.host_process_id` before host observation or signaling.
**Never send a namespace process-group ID of 1 to host group signaling: `kill(-1, ...)` is a broadcast.** Fixture cleanup must identify only its own resources.
