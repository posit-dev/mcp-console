# Shiny control-channel fixture

This local Shiny for Python app puts agent chat beside Console's transcript, Variables pane, plots, and table viewer.
The human and agent share named sessions through the [integrator control channel](../../../docs/INTEGRATOR_CONTROL.md).
Browsing variables, output, or tables does not add their contents to the model's context.

## Run the app

The fixture requires macOS or Linux, a built Console binary, and authentication for `openai-codex` with access to `gpt-6.1-terra`.
It uses that model with low reasoning effort and runs Console **without its sandbox** in the selected workspace.
Each browser session owns one agent thread and Console process; closing it stops both.
The thread disables inherited MCP servers without editing the user's configuration files.

From the repository root:

```sh
uv run --script tests/fixtures/control_shiny/app.py --binary target/release/mcp-console
```

Open `http://127.0.0.1:8000`.
Use `--workspace PATH` to retain files in an existing directory (the default is temporary), and `--port PORT` to change the port.
The command does not open a browser automatically.

## Work with sessions

Choose a **Console session** or use **+ New** to create one.
Each has separate objects, SQL connections, requirements, and execution state; files and the agent conversation are shared.
Prompts target the session selected when sent.
Switching the dropdown does not retarget requests in flight or clear model context.
Inactive sessions keep running and retain their transcript and plots, but their table snapshot closes; reopen it from Variables when returning.

**Close** stops a named worker and clears its app views; `default` remains available.
Recreating a name gives it fresh runtime state while retaining Console's requirements declaration and recording.

Select R, Python, or SQL and **Submit**, or ask the agent to run code.
Both use the same selected worker.
**Immediate** runs a private request while ordinary execution can service it; it does not make concurrent access atomic.
It accepts R and Python; use **Ordinary** for SQL and R plots.
**Interrupt** targets the ordinary evaluation and retains objects; **Restart** replaces the worker and discards its objects and table snapshots.
The transcript records requests and outcomes separately, so accepting an interrupt does not imply that execution has stopped.

Console prepares its managed environments independently of Shiny's Python interpreter.
The **Environments** tab shows accepted requirements, including the viewer's pandas and DuckDB dependencies.
Enter one requirement per line and choose **Add and prepare**.
Busy execution preserves the draft for retry; failures refresh the accepted declaration because successfully activated changes may remain.
The panel supports additions, not replacement, reset, or interpreter changes.
Restart retains accepted requirements and does not apply an unaccepted draft.

## Read and export output

Each submission has two views:

- **Captured output** shows typed worker events, diagnostics, images, and omission notices.
- **Responses to agent** shows the actual MCP blocks observed by the SDK.
  A response can span output from multiple cells or sessions; delivery does not establish that the model read it.

Direct human and private immediate results are labeled **Not sent to agent**.
Long output can be expanded without executing code.
The transcript follows new output while scrolled to the bottom and preserves a position chosen by the reader.

**Open retained output** reads bounded pages from Console's recording; **Refresh** picks up later output.
It does not run code or change the transcript or model context.
Ordinary cells can have a raw log beyond the live view's limits; immediate requests retain a bounded result and images.
Unavailable or omitted content is reported explicitly.
See [Recording reference](../../../docs/RECORDING_REFERENCE.md) for retention limits.

**Code view** switches between original source and display-only formatting using Air for R and Ruff for Python on the Shiny host.
Missing tools, invalid input, and formatting limits leave the original visible with an explanation; SQL has no formatter.
Copy uses the displayed source; execution and exports always use the original.
The [formatter coordinator](source_format.py) owns process limits and caching.

**Export** copies the selected worker's QMD or the recording's Markdown transcript across all sessions and worker replacements.
A worker without source or notes may have no QMD.
These are text copies: image and output links remain relative to the recording directory, and running output may continue afterward.
Private inspection stays out of these projections, but its raw journal events remain recorded.

## Inspect variables, plots, and tables

Variables refresh through private immediate requests.
Python previews avoid custom `__repr__` methods; R uses `str()` methods, which can evaluate promises or active bindings.
R inspection waits while Python or SQL owns the worker.
Errors stay in the affected pane, and later refreshes can recover.
[Inspection programs](inspection.py) own the bounded previews and private result transfer.

Plots reuse Console's captured images.
Resizing scales those images without rerunning code; there is no interactive plot or redraw-at-size support.
Interaction proposals belong in [Open work](../../../docs/TODO.md#documentation-and-agent-usability).

Open an R data frame or pandas DataFrame from Variables to create a table snapshot.
Sort and filter controls apply to the whole snapshot; the browser receives one page of 100 rows.
**Refresh** captures mutations to the original object.
Avoid mutating it during capture: snapshot creation is not atomic with concurrent execution.
Closing the view or replacing the worker releases or invalidates the snapshot.

The viewer accepts 1–100 uniquely named string columns containing supported scalar values.
Unsupported types produce an explicit error.
Long displayed strings have truncation notices; filtering and sorting use complete stored values.
Large integers and decimals preserve exact displayed values as strings and use text filters when needed.

Snapshots use a private DuckDB connection, separate from Console's selected SQL connection and the user's R/Python namespaces.
Opening a large table requires snapshot memory; sorting or filtering can scan it even though returned pages are bounded.
[Table capture and queries](table_runtime.py), [request coordination](tables.py), and [the table pane](table_pane.py) own conversion, validation, limits, and pagination.

## Browser checks

These optional checks launch the actual app and Console process.
They cover shared state, controls, session switching, output, formatting, exports, inspection, and tables.
Live agent checks also make model inference requests and require authentication and model access.

```sh
uv venv /tmp/console-shiny-test
uv pip install --python /tmp/console-shiny-test/bin/python shiny 'pandas[output-formatting]' openai-codex pytest playwright py-yaml12
/tmp/console-shiny-test/bin/python -m playwright install chromium
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" MCP_CONSOLE_LIVE_AGENT=1 \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny -k 'agent or native_chat'
```

They are separate from the ordinary repository gate because they install a browser and start the agent runtime.
Set `MCP_CONSOLE_FIXTURE_SCREENSHOT` to capture the populated UI after a successful live smoke.
The fixture does not log in or substitute another model when authentication or access fails.
