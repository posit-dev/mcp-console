# Boundary tests

Tests capture behavior at process interfaces.
Use the outermost boundary that observes a regression; private tests cover their seam without repeating public result text.
Security and liveness cases may need causal/process assertions in addition to snapshots.
Keep combined cases when their interaction is the failure mode being tested.

| Boundary        | Contract                                                            |
| --------------- | ------------------------------------------------------------------- |
| `client_server` | MCP negotiation, tool schema, results, errors, and delivery.        |
| `server_relay`  | Private JSONL framing, ordering, and generation ownership.          |
| `relay_worker`  | Worker sideband, native streams, EOF, crash, and shutdown.          |
| `cli`           | Arguments, exit status, signals, terminals, and sandbox guarantees. |

## Find a contract's test area

Use `scripts/test --full --list` for exact cases and `scripts/test --locate SELECTOR` for source and snapshots.
[Architecture](../../docs/ARCHITECTURE.md#where-to-look-in-source) maps production ownership; [Windows validation](../../docs/WINDOWS_DEVELOPMENT.md#validation) covers native acceptance.

| Behavior           | Test areas                                                                                               |
| ------------------ | -------------------------------------------------------------------------------------------------------- |
| Admission/delivery | [Server](client_server/server/), [output](client_server/output/)                                         |
| Worker lifecycle   | [MCP lifecycle](client_server/lifecycle/), [relay/worker](relay_worker/lifecycle/)                       |
| Framing            | [CLI relay](cli/relay/), [server/relay failures](server_relay/failures/)                                 |
| Preparation        | [MCP requirements](client_server/requirements/), [server/relay requirements](server_relay/requirements/) |
| Recording          | [MCP recording](client_server/recording/)                                                                |
| Sandbox            | [CLI sandbox](cli/sandbox/), [MCP sandbox](client_server/sandbox/)                                       |

## Running cases

```sh
scripts/test --full --list
scripts/test --stress --list
scripts/test --locate client_server/server/test_tools
scripts/test client_server/server/test_tools::initializes_and_lists_tools
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

Selectors name `BOUNDARY/SUITE` or `BOUNDARY/SUITE::CASE` and retain scope under every profile.
Default runs use the smoke profile.
`--full` includes every functional capability-applicable case; `--stress` runs allocation and repeated concurrency workloads with their original ceilings.
Short recovery cases do not substitute for allocation stress.
[Development](../../docs/DEVELOPMENT.md#validation-ladder) covers gates, installed binaries, concurrency defaults, and build ownership.

Cases run in separate processes.
`--jobs N` controls concurrency and `--timeout SECONDS` changes the default 600-second case deadline, including snapshot work.
Cancellation requests cleanup, then allows 15 seconds before forced case termination.
Fixtures own their subprocess cleanup.
Unix supervision does not guarantee descendant retirement; Windows additionally uses kill-on-close Jobs and confirms they are empty before deleting workspaces.

Failures are collected while remaining cases continue, until the runner's failure threshold stops new launches.
Active cases retain their deadlines.
The final report lists failed selectors and unstarted cases; any failure fails the command.
Progress and rerun messages are UI output, not transcript records.

## Requirements and execution modes

Declare capabilities with `@requires(...)` from `support.requirements`; platform detection belongs in shared support.
Execution fixtures select launch behavior:

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
`RUNTIME` selects sandboxed macOS/Linux and direct shared Windows execution; native Windows acceptance owns its sandbox coverage.
Use one mode for ordinary language/representation behavior, both for contracts that depend on mode, and sandbox fixtures for sandbox-only arguments.
There is no runner `--execution` flag.

Preserve representative direct runtime, plot, input, restart, and recording cases, and real-library sandbox cases with distinct permissions or activation paths.
Policy cases belong in their boundary's `sandbox/` directory; ordinary runtime cases stay with their subject.
Declare `SQL` for SQL behavior and `R` when the fixture or expected provider requires it.
Declare `POSIX` for Unix shell/FIFO fixtures; those exclusions do not imply that runtime behavior is unsupported.

Modes run sequentially within one case deadline and normally share a snapshot.
During updates, later modes must match the first rather than overwrite it.
`@execution_snapshots` is for behavior or captured policies that differ: direct owns `CASE.yaml`, sandbox owns `CASE.sandbox.yaml`.
Fixture write grants alone do not justify variants.
Unavailable modes are skips, not validation.
Test-host observation facilities are not necessarily production requirements.

Every case gets a temporary workspace and private `MCP_CONSOLE_HOME`, preserving `HOME` and the caller's R/Python/uv environment.
Home-discovery cases explicitly use `use_home_configuration=True` and remove inherited `MCP_CONSOLE_HOME` when testing the default location.
Remove fixture directories only after their processes exit.

## Snapshots

A `test_` function returns a `Transcript` stored as YAML 1.2 at `tests/snapshots/BOUNDARY/SUITE/CASE.yaml`.
`TranscriptWithCompanions` adds named siblings: YAML compares as values; Markdown/Quarto compare as exact UTF-8 text.
Wrap extra MCP transcripts in `McpTranscript` from `support.records` for request-ID normalization and canonical-handshake comparison.
Ordinary YAML companions retain protocol IDs.
Underscore-prefixed suite paths are not discovered.

Never edit snapshots by hand.
Regenerate intentional changes, inspect the diff, and rerun without `--update`.
Only full unscoped runs audit orphans; only successful full updates remove them.
Focused updates preserve unselected/skipped cases.
Successful updates retire obsolete variants only for updated cases and preserve unrun declared modes/platforms and shared references still in use.

Prefer shared platform snapshots.
Make fixtures deterministic, then normalize incidental paths or suffixes after assertions.
Use `@platform_snapshots("win32",
reason="...")` only when the case owns the differing platform contract.
Windows variants place `.win32` before the execution suffix.
Handshake defaults, unrelated package differences, and presentation alone do not justify duplicating portable cases.

Preserve complete errors, tracebacks, status codes, signals, and meaningful output ordering.
Do not weaken assertions to match snapshots.
Use mappings for structured data but retain serialized JSON when escaping is the contract.
Version/initialization/ recording cases assert actual versions before normalization.
CLI help uses an 80-column width with streams detached from the caller's terminal.

Narrow compaction requires full evidence first:

| Case                                     | Required evidence                                                                                                                                                         |
| ---------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Requirements supporting a lifecycle case | Compare complete text with `structuredContent`, assert the owned declaration, and retain those fields. Keep full inventories in requirements cases; never project errors. |
| Native-runtime fidelity                  | Compare complete output/conditions with the live reference before recording the verified comparison. Remove only irrelevant frontend differences.                         |
| MCP overflow previews                    | Retain literal previews and assert UTF-8 budgets, exact head/tail, and omission accounting. Assert raw bytes separately.                                                  |
| Repeated synthetic payloads              | Use lossless `support.evidence.compact_text()` only after complete assertions; keep diagnostics, boundaries, omissions, images, and final states.                         |

`transcript_normalization` is harness metadata, not a wire field.
Request IDs and invariant JSON-RPC versions abbreviate only after validation.
Cancellation targets retain matching request/notification labels; unmatched IDs remain literal.

### Canonical handshake

Keep one Console invocation per transcript, normally with initialization and tool discovery near the top.
Restarts in that invocation stay in the same transcript.
Launch/protocol failure cases retain whatever complete or partial exchange occurred.

`client_server/server/test_tools::initializes_and_lists_tools` owns full handshakes and configured/direct/bare/runtime variants.
Update it before affected cases.
The runner compares complete exchanges before replacing exact matches with `!same-as`; the tag records that comparison rather than loading another file.
Different or incomplete exchanges stay in full.
Fixture write-root normalization belongs in the [shared client](../support/client.py), after behavior assertions.

## Fixtures

Use `McpClient` as a context manager with `initialize_and_list_tools()`, `send()`, and `finish()`.
Overlapping calls use `start_send()` and `receive()` / `receive_many()`, matching response IDs.
Use `finish_with_standard_error()` when diagnostics are part of the contract.

Shared helpers live in `tests/support/`; each boundary's `_harness.py` owns launch and capture mechanics.
Keep large fixture programs in searchable files under `tests/fixtures/`.
[Authoring](AUTHORING.md) covers readable sessions and causal gates.

Use `support.processes.host_process_id` before host observation or signaling of namespace PIDs.
**Never send namespace process-group ID 1 to host group signaling: `kill(-1, ...)` is a broadcast.** Cleanup must identify only fixture-owned resources.
