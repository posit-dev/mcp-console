# Shiny control-channel fixture

This local Shiny for Python app puts agent chat beside a transcript and Variables pane for the same Console worker.
The agent owns Console's MCP connection.
The app uses the private control socket to submit code and receives pending output through one subscription.
Pending output appears in gray until a completed response becomes a transcript entry.
This preview follows the ordinary cell; Immediate requests return their own output on completion.
The transcript renders text and images.

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
Use `--port PORT` to choose another local port.

Submit Python or R directly on the right, or ask the agent to run code on the left.
Both paths retain objects in the same worker.
Submitted code appears immediately in the transcript, followed by its completed output.
The execution selector offers Execute ordinary cell and Evaluate immediately.
Choose Execute ordinary cell to submit a console cell that waits for the worker to become available.
Choose Evaluate immediately for private evaluation in the same session, using reentrant R execution or a Python background thread.
An already executing R cell pauses when it services events, lets the immediate evaluation run, then resumes.
Use Evaluate immediately only for code that the integrator can safely run alongside the active evaluation; both modes share session state.

The Variables pane refreshes about once a second through private immediate requests, including while an ordinary cell is running.
It uses `utils::ls.str()` for R's global environment and bounded previews of Python's `__main__` symbols, showing up to 100 names per language.
Python previews display exact builtin values and small containers; other objects appear by type without calling custom `__repr__` methods.
R uses its usual `str()` methods, which can evaluate promises or active bindings.
R inspection requires R to service events and waits while an ordinary Python or SQL cell owns the worker; the pane retains its last view until inspection can resume.
Inspection output stays out of the transcript and pending-output preview.

## Browser regression and live smoke

The browser regression launches the actual app and Console process.
It blocks an evaluation on a file checkpoint, verifies that pending output arrives before completion, releases the checkpoint, and checks retained state and rendered images.
Additional cases select Evaluate immediately for R and Python while an ordinary cell remains blocked.
Variables cases check live changes and deletion during a blocked cell, private output routing, and Python previews without custom repr execution.
The live smoke additionally asks Luna to mutate an object created through the direct input and verifies the result through both paths.
Only the live smoke makes a model inference request.

```sh
uv venv /tmp/console-shiny-test
uv pip install --python /tmp/console-shiny-test/bin/python shiny openai-codex pytest playwright
/tmp/console-shiny-test/bin/python -m playwright install chromium
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny/test_app.py
MCP_CONSOLE_TEST_BINARY="$PWD/target/release/mcp-console" MCP_CONSOLE_LIVE_AGENT=1 \
  /tmp/console-shiny-test/bin/python -m pytest -q tests/fixtures/control_shiny/test_app.py -k agent
```

These optional checks are separate from the ordinary repository gate because they install a browser and start the agent runtime.
Set `MCP_CONSOLE_FIXTURE_SCREENSHOT` to a file path to capture the populated UI after a successful live smoke.
Authentication failures and unavailable model access are reported as failures; the fixture does not log in or select another model.
