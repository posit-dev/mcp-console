# Drive send directly

Use `send` to run code and manage a persistent session from your own MCP client.
The examples below are tool argument objects, not complete JSON-RPC messages.
If you are using R or Python, the [client packages](BUILD.md#choose-an-integration) provide the connection and framework adapters.

Send one complete cell at a time and collect its result before sending another.
For exact argument types, operation ordering, and limits, see the [send reference](API.md).

## Send a cell and collect its result {#cells-and-polling}

```json
{ "python": "values = [2, 4, 6]; sum(values)" }
```

The response contains output from the cell and may include images or a status notice.
After the cell finishes, later cells can use `values` while the connection remains open.

If the response says `[running; poll with an empty send]`, call `send` again without code:

```json
{ "timeout_ms": 300000 }
```

This waits for up to five minutes and returns early when work finishes or needs input.
Continue collecting output until the cell finishes.
Do not resubmit the original source: the accepted cell is still running.
If your tool configuration hides `timeout_ms`, poll with `{}` instead.

<span id="server-readiness"></span> <span id="wait-timing-and-cancellation"></span>

A first call may wait while Console prepares the runtime and packages.
If an empty poll reports `[worker starting]`, keep waiting; do not submit the same cell again.
Stopping a response wait does not stop an accepted cell.
Use the interrupt control below when you want to stop execution.

Reference: [Result notices](API.md#results), [server readiness](API.md#server-readiness), and [wait timing and cancellation](API.md#wait-timing-and-cancellation).

## Answer an input request {#standard-input-and-managed-reads}

Code can ask for input, including at a debugger prompt.
When Console reports `[waiting for stdin]`, reply through `stdin`:

```json
{ "stdin": "yes\n" }
```

Include the newline for a line-oriented prompt.
Send the answer as input, not as another code cell.
For example, R's debugger accepts `c\n` to continue or `Q\n` to quit.

Reference: [Standard input and managed reads](API.md#standard-input-and-managed-reads).

## Interrupt work {#interruption}

```json
{ "control": "interrupt" }
```

You can send this control-only call while another call is waiting.
If the pending call owns output delivery, collect the remaining output through that call and subsequent polls.
An interrupt is cooperative: code can catch or delay it.
Use restart when you need a fresh runtime.

Reference: [Interruption](API.md#interruption).

## Restart the runtime {#explicit-restart}

```json
{ "control": "restart" }
```

Restart discards live language objects, database catalogs, and unread input.
It retains accepted package requirements and recordings.
Repeat any application setup before giving the model access to the new session.
Configured startup code can run again, including its side effects.

Reference: [Restart behavior](API.md#explicit-restart).

## Change packages

A client can request packages before submitting code or alongside the cell that needs them:

```json
{
  "requirements": { "python": ["polars>=1"] },
  "python": "import polars as pl; pl.DataFrame({'value': [1, 2, 3]})"
}
```

Console prepares the packages before running the cell.
The session's package policies determine which changes are allowed.
Read [Packages and environments](REQUIREMENTS.md) for the preparation model and [requirements actions](API.md#requirements) for inspecting or replacing a declaration.

<span id="operations"></span>

Keep control, package changes, and execution in separate calls unless you need their combined behavior.
The [operation-order table](API.md#operations) describes combined calls and their partial effects.

## Close the connection

Keep the connection open for as long as you need its live state.
Closing it ends the session; reconnecting starts a new one.
[Recordings](RECORDING.md) retain the work for review, but do not restore live objects.

<span id="python-client-cancellation"></span>

For Python connection cleanup and `KeyboardInterrupt` behavior, see [MCPConsole](https://posit-dev.github.io/mcp-console/python/reference/mcpconsole/).
