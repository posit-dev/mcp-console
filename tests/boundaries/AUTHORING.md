# Authoring a boundary case

Locate a related case with `scripts/test --locate SELECTOR`, read its contract, and add a public regression before changing behavior.
Follow the [validation ladder](../../docs/DEVELOPMENT.md#validation-ladder).

## Readable session transcripts

A client/server transcript should read as a plausible client session or a focused behavioral regression, not a second unit-test suite.
Check schema, guidance, and configuration through public discovery or configuration interfaces.
Extend existing cases rather than launching duplicate sessions for the same permutations.
Before deleting a case, identify its surviving coverage and remove its producer and owned snapshots together.

Make each YAML understandable without reading its Python fixture.
Emit a short `scenario` and `fixture` description before initialization when setup is not evident from the exchanges.
Identify fake workers, hidden runtimes, injected failures, gates, and host-side actions in one to three sentences; keep the prose in the producer so regeneration preserves it.
Record one Console invocation per transcript, using companions for genuinely distinct sessions.

Focused interrupt handlers and causal lifecycle fixtures are valid behavioral tests even when their cells contain assertions.
Preserve real-library sandbox regressions and distinct permission, isolation, cold-cache, and activation paths; do not replace them with trivial programs.

## Embedded programs

Use short strings for short fixtures and dedented multiline literals for longer programs.
Put `# fmt: python` or `# fmt: r` immediately above `code(` and its opening delimiter, including in nested calls.
Indent the payload and closing delimiter four spaces beyond that line:

```python
client.send(
    # fmt: python
    python=code(r"""
        for value in (1, 2):
            print(value)
        """),
)
```

Use raw literals when backslashes belong to the submitted program.
Preserve its indentation and trailing newlines.
Do not label a custom worker's command language as R/Python merely because it travels in that field.
Run `scripts/format` and inspect every formatter's result and the diff; `--strict` fails after attempting all formatters when any is missing or fails.

## Execution modes

Use one execution mode for ordinary language or representation behavior: `@executions(RUNTIME)` selects the platform's runtime mode.
Use `@executions(DIRECT, SANDBOXED)` only when the contract differs by mode, and launch through `execution.serve()`.
Put sandbox-only arguments in explicit sandbox fixtures, for example `SANDBOXED.serve("--writable-root", str(root))`, with the appropriate capability.
See [modes and requirements](README.md#requirements-and-execution-modes).

## Lifecycle receipts and checkpoints

Establish the observable state an assertion needs before releasing a fixture.
Use `FifoCheckpoint` (`support.checkpoints`), `read_lines` (`support.capture`), and `Events` (`support.events`) for blocking gates, complete stream receipts, and process transitions.
Keep cleanup in context managers or `finally` blocks.
Use `wait_for_path()` for known marker paths and worker-file discovery only for worker-owned directories whose names are initially unknown.
`FifoCheckpoint.release()` buffers an early token; `release_fixture_checkpoint()` instead waits for a real reader with a bounded rendezvous.
Pass the client when its exit should end that wait.
Marker waits subscribe to native filesystem/process events on macOS and Linux; Windows uses cancellable bounded path polling.
Pass an owned `Events` instance to `wait_for_path()` when the fixture needs explicit cancellation; `Events.cancel()` wakes the wait without a marker or client.

```python
client.send(python=program, timeout_ms=0)
assert last_tool_text(client) == "\n[running; poll with an empty send]"
started.wait("worker reached the release gate")
release.release()
```

A fixture marker alone does not prove public evaluation admission.
Writing a request to stdin does not prove its handler has started.
When cancelling a send during gated discovery, observe its exclusive polling claim before submitting a competing cell or cancellation.
Before closing input to check response delivery, gate an accepted result; EOF can cancel a call that has not been accepted yet.
Process startup does not prove a cell log has closed.
For shutdown output, establish cell completion and its recorded `cell_output` event before asserting which file owns later bytes.
Sleeps, broader matching, and longer timeouts do not establish ordering.

`wait_for_evaluation_output()` submits once, accumulates every text delta, and bounds transport receives with its completion budget.
Give cold preparation an explicit longer budget.
The default `send` observation deadline can return while setup is still running; establish completion before dependent requests.
Use the client's preparation budget for resolver collection and initial requirements inspection.
Use `McpClient.response_deadline()` for client-aware fixture waits so the remaining case deadline retains its shutdown and reap reserve.
Convert the absolute deadline to a nonnegative timeout immediately before blocking.
Retain raw exchanges when delivery or polling is the contract being tested.

See the [Python lifecycle cases](client_server/python/test_lifecycle.py) and [retention cases](client_server/output/test_spools.py).
Preserve complete output assertions and use the [snapshot rules](README.md#snapshots) for normalization.
