# Shiny control-channel fixture

This local Shiny for Python app puts agent chat beside a transcript, Variables pane, plot gallery, and table viewer for the same Console worker.
It exercises the integrator control channel through an IDE-style interaction model: the human and agent share a live session, while execution history and object inspection remain visible beside the conversation.
The conversation uses Shiny's native `ui.Chat`, including streamed Markdown and its chat input.
The agent owns Console's MCP connection.
The app uses the private control socket to submit code and observe typed output.
Evaluation identities keep pending and completed output attached to their submission.
Immediate requests return their own private output on completion.
The transcript distinguishes native stdout/stderr, console output, diagnostics, images, and execution errors without parsing formatted MCP text.

The layout combines conversational data exploration with an IDE's shared objects, explicit execution, and inspectable results.
It follows [Querychat's distinction between updating data views and answering analytical questions](https://posit-dev.github.io/querychat/py/tools.html): browsing a table is a human inspection action, while requested analysis runs through the agent's Console tool.
Viewer filters and row results do not automatically become model context.
Console owns execution and runtime state; Shiny owns the chat and inspection UI.

The fixture requires macOS or Linux, a built Console binary, and existing authentication for the `openai-codex` Python package.
It always selects `gpt-6-luna` with low reasoning effort; there is no automatic model substitution.
Each browser session creates one agent thread and one Console process, and closes both when the session ends.
Thread-local configuration disables inherited MCP servers and approves the fixture's `send` tool.
The SDK's effective configuration supplies the server names; the user's configuration files are not modified.
Console runs without its sandbox in the selected workspace.

From the repository root, launch it without opening a browser automatically:

```sh
uv run --script tests/fixtures/control_shiny/app.py --binary target/release/mcp-console
```

Open `http://127.0.0.1:8000`.
Use `--workspace PATH` to retain files in a particular directory, or omit it to use a temporary workspace.
Use `--port PORT` to choose another local port, for example `--port 8001` if port 8000 is already in use.

Select R, Python, or SQL in the language radio group and click **Submit**, or ask the agent to run code.
Both paths retain objects in the same worker.
Console selects and prepares its usual managed environments; its embedded Python interpreter is independent of the interpreter running Shiny.
The app declares its viewer dependencies through Console's ordinary requirements mechanism before making immediate requests.
SQL uses Console's selected SQL connection and ordinary execution semantics.

Submitted code has separate language and origin labels.
Console still accepts one language per MCP `send`; a rejected request with multiple language fields shows each submitted language separately.
The execution selector offers **Ordinary** and **Immediate**.
Choose Ordinary to submit a console cell that waits for the worker to become available.
Choose Immediate for private evaluation in the same session, using reentrant R execution or a Python background thread.
SQL supports Ordinary execution only.
An already executing R cell pauses when it services events, lets the immediate evaluation run, then resumes.
Use Immediate only for code that the integrator can safely run alongside the active evaluation; both modes share session state.
Python needs the GIL, and R cannot preempt native code that does not process events.

The compact **Interrupt** and **Restart** buttons beside the editor act directly through the control channel without an agent turn.
Interrupt targets the current ordinary evaluation; it does not cancel a standalone immediate request.
Restart replaces the worker, clears its in-memory objects and viewer snapshots, and prepares the new managed session.
Connection status reports preparation progress; a preparation failure does not disable Restart.
The transcript retains previous submissions and marks any running entries as stopped once worker replacement is observed.

The transcript follows incoming output while the viewer is at the bottom.
Scrolling up preserves the viewer's position, including when pending output completes or an image loads.
Human and agent interrupt requests are shown separately from their results; accepting a request does not establish that execution has stopped.

Submitted R, Python, and SQL code uses Shiny's native Markdown component for syntax highlighting, with origin and language labels retained in the chat-style transcript.
The native Shiny code editor starts in Python mode and follows the selected language.
The typed-output transcript keeps a small incremental renderer to reconcile concurrent evaluations without replacing messages or resetting the viewer's scroll position.

The **Export** menu at the transcript's top right copies Console's existing projections to the clipboard.
**Copy QMD** copies source and environment metadata for the current worker; a new worker has no QMD until recorded code is submitted.
**Copy Markdown** copies the recording's transcript across worker sessions, including recorded output and image links.
Exports read the recording files rather than the browser's bounded output history, and private inspection calls remain omitted.
These are text copies at read time: image and retained-output links remain relative to the recording directory, and running output can continue afterward.
The browser reports clipboard permission or read failures instead of claiming the text was copied.

The Variables pane refreshes about once a second through private immediate requests, including while an ordinary cell is running.
It uses `utils::ls.str()` for R's global environment and bounded previews of Python's `__main__` symbols, showing up to 100 names per language.
Python previews display exact builtin values and small containers; other objects appear by type without calling custom `__repr__` methods.
R uses its usual `str()` methods, which can evaluate promises or active bindings.
R inspection requires R to service events and waits while an ordinary Python or SQL cell owns the worker; the pane retains its last view until inspection can resume.
Inspection output stays out of the transcript and pending-output preview.

## Manage environments

The **Environments** tab uses Shiny's native filterable, sortable DataTable for the complete requirements list.
It shows accepted declarations and Console runtime dependencies, not an installed-package inventory or resolved versions.
The preparation indicator is Console's aggregate environment flag.
The app also requests Python `pandas` and `duckdb` for its table viewer during session setup.

Select R, Python, or DuckDB extensions, enter one complete requirement per line, then choose **Add and prepare**.
For example, keep a Python constraint such as `requests[socks]>=2,<3` on one line.
Preparation uses Console's existing managed environments and policies without an agent turn or an immediate evaluation.
Private inspection and table requests pause while the app prepares requirements.
Busy execution returns a visible status without retrying automatically; the draft is preserved for retry or correction.
Human preparation requests and outcomes appear as transcript notices, with diagnostics in the Environments tab.

The accepted list refreshes after operations and worker replacement without overwriting the draft.
Failures can retain successfully activated changes from a mixed-language request, so the refreshed declaration is the source of truth.
This first version adds requirements only; replacement, reset, and interpreter changes that require restart remain outside this panel.
The ordinary Restart button retains accepted requirements and does not apply an unaccepted draft.

## Inspect plots

The Plots pane reuses the images Console already captured for delivery to the agent.
Open an image to inspect it at a larger size.
Resizing the pane scales the captured image without running plotting code again.
These are static images: they do not provide plot-specific hover, zoom, or selection, and resizing does not change their underlying resolution.
Redrawing at a new device size would require runtime support for retained plotting commands or display lists; this fixture does not add it.

## Explore tables

Open a supported table from the Variables pane.
The viewer uses Shiny's native table renderer and input controls, with 100 rows per page.
Choose a sort column, set column filters, and use Previous, Next, or a page number to explore the result.
Sorting and filters run against the whole snapshot in a private DuckDB connection in the worker's Python interpreter.
The browser receives only the requested page.
Shiny's DataGrid and DataTable apply client-side sorting and filtering to the supplied frame, so the fixture uses `render.table` with explicit controls to preserve backend ordering and filtering across pages.
It does not require AG Grid, vendored UI libraries, or a CDN.

Opening or refreshing a table materializes a snapshot once.
R data frames are converted on R's owning thread through the existing R/Python bridge.
Python pandas DataFrames register directly with the private connection.
Both paths use the same query builder and a stable row ordinal to break sorting ties.
Filter values are bound as parameters, and column references are checked against the snapshot schema.
Later row requests reuse this snapshot; they do not convert the whole R data frame again.
Mutating or replacing the original object does not change an open snapshot: use **Refresh** to capture it again.
Avoid mutating the selected object while its snapshot is being captured; immediate execution does not make concurrent access atomic.

The private connection, registrations, and helpers do not add bindings to R's global environment, Python's `__main__`, or Console's selected SQL connection.
Table requests share the immediate-request coordinator with variable inspection.
Filter changes are debounced, and superseded requests cannot replace the current view.
A busy worker leaves the view pending instead of receiving a flood of retries.
Closing the view or replacing the worker releases or invalidates the snapshot.

The app transfers only the requested rows and metadata through private temporary files.
This avoids Console's text response size limit and keeps exploration out of the model's context and visible transcript.
The control requests still follow Console's normal raw-journal recording policy.
Opening a large table requires memory for its snapshot, and filtering or sorting may scan that snapshot even though returned row blocks are bounded.

The first implementation accepts R `data.frame` objects and Python pandas DataFrames with 1–100 uniquely named string columns.
Supported scalar columns include strings, booleans, numbers, dates, timestamps, and categorical/factor values that DuckDB can represent.
Nested/list columns, binary columns, arbitrary Python objects, and other unsupported DuckDB types produce an explicit error.
The viewer requests 100 rows at a time and retains only the current page; the backend permits at most 200 rows per request.
Displayed strings are shortened after 1,024 characters with a truncation notice; filters and sorting use the complete stored value.
Large integers and decimals are returned as strings to preserve their displayed precision.
Decimal columns and integer columns containing values outside JavaScript's exact integer range use text filters for exact matching; sorting still uses their numeric values in DuckDB.

## Browser regression and live smoke

The browser regression launches the actual app and Console process.
It blocks an evaluation on a file checkpoint, verifies that pending output arrives before completion, releases the checkpoint, and checks retained state and rendered images.
Additional cases select Immediate for R and Python while an ordinary cell remains blocked.
Human controls cases interrupt a running cell without clearing its objects and restart a running session with an open table.
Variables cases check live changes and deletion during a blocked cell, private output routing, and Python previews without custom repr execution.
Table checks use the actual control channel and managed runtimes to exercise snapshot freshness, row limits, filters, sorting, and namespace isolation.
The live smoke additionally asks Luna to mutate an object created through the direct input and verifies the result through both paths.
The live agent checks make model inference requests; the other checks do not.

```sh
uv venv /tmp/console-shiny-test
uv pip install --python /tmp/console-shiny-test/bin/python shiny 'pandas[output-formatting]' openai-codex pytest playwright py-yaml12
/tmp/console-shiny-test/bin/python -m playwright install chromium
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" MCP_CONSOLE_LIVE_AGENT=1 \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny -k 'agent or native_chat'
```

These optional checks are separate from the ordinary repository gate because they install a browser and start the agent runtime.
Set `MCP_CONSOLE_FIXTURE_SCREENSHOT` to a file path to capture the populated UI after a successful live smoke.
Authentication failures and unavailable model access are reported as failures; the fixture does not log in or select another model.
