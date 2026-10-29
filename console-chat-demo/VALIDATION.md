# Validation

Validated on macOS on October 29, 2026, using this checkout's Console binary (`0.0.6+dev.g6c7e53ca1bb1`) and its matching native sandbox companion.
The model checks used the real `gpt-6.1-terra` Responses API with low reasoning effort.
No substitute model or mocked Console was used for those checks.

## App checks

The five-test app suite passed in one run.
A subsequent private-path regression and a cold R/Python startup check also passed after the final policy change.
A capacity-reduction regression then confirmed that retained files in a higher-numbered slot remain inaccessible when the server restarts with fewer slots.
Together, the seven tests cover:

- Serving the UI and public assets without a model call; private files are not static routes.
- Persistent R and Python variables and real PNG plots from both runtimes.
- Concurrent execution, using a file checkpoint to hold one client while another completes.
- Denied cross-client workspace and temporary-file reads, including clients started later.
- Denied reads of retained client files after reducing the server's session capacity, without deleting those files.
- Capacity limits, clean slot reuse, and cancellation during MCP initialization.
- Absent API credentials in both runtimes, denied private-file reads and host writes, and blocked networking against a listening local socket.
- Rejection of missing explicitly configured private paths before creating the runtime root.

After enabling the full tool surface, focused process checks passed for the advertised parameters, SQL over the CSV, a named SQL session, requirements inspection and requests, and denial of SQL reads from another client's slot.
The concurrent-client isolation and HTTP checks also passed with the per-client preparation caches.

From the repository root:

```sh
TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
CONSOLE_CHAT_BINARY="$PWD/target/release/mcp-console" \
PYTHONDONTWRITEBYTECODE=1 \
  console-chat-demo/.venv/bin/python -B -m pytest console-chat-demo/tests \
  --basetemp=console-chat-demo/.runtime/final-pytest -q
```

## Browser checks

Chromium drove the actual app through its chat input, buttons, download link, and table UI.
Two independent browser contexts shared one Shiny server process.
Live model calls verified Python calculations, R plotting, retained state on a follow-up turn, separate client variables, Markdown export, and New session.
A second check held one client's Python cell at a filesystem checkpoint while the other client completed a model/tool turn, then pressed Stop and verified that the blocked worker PID and workspace disappeared.

The desktop and 390-pixel mobile layouts were inspected visually.
Browser assertions check loaded plot pixels, mobile card ordering, an unobstructed usable composer, and absence of JavaScript page errors.

The YAML display and follow-up buttons were also checked interactively in the in-app browser with live Terra calls.
Assertions first reproduced the JSON display and missing suggestion button, then passed after the change: native cards showed multiline YAML, and clicking a generated suggestion submitted the next question and completed another tool turn with retained Python state.
The HTTP app test passed.
The live browser script includes these display and suggestion assertions for subsequent runs.
An additional live browser session submitted Python and R cells; its exported Markdown preserved each cell in a language-tagged code block and included the corresponding output.
Another live Terra session used SQL to aggregate the CSV, confirmed a cross-client SQL read was denied, requested and used an additional Python package, and rendered plots from both R and Python.
A separate browser check with a simulated tool response verified that the export retains tool options as well as the submitted code.

With the app running on port 8008:

```sh
TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
  console-chat-demo/.venv/bin/python -B console-chat-demo/tests/browser_check.py \
  --browser '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --live

TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
  console-chat-demo/.venv/bin/python -B console-chat-demo/tests/browser_check.py \
  --browser '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --lifecycle
```

Supply the installed Chromium executable on another host.
The live options make billed model requests using the running server's credentials.
Screenshots, a downloaded analysis, and result receipts are saved under `.runtime/browser/` and excluded from Git.

## Console checks

- Offline release build through `scripts/with-checkout` passed.
- Two focused recording cases passed after replacing a stale `has_source()` call with the existing `has_contents()` predicate in `src/transcript/markdown.rs`.
- `scripts/preflight --json` passed.
- `scripts/check-core` passed: runtime-source checks, architecture, rustfmt, Clippy, and all 53 Rust tests.
- `scripts/test --jobs 2` passed all 17 default smoke cases.
- `scripts/format` reported success for Ruff, Yamark, rustfmt, and Air.

The ordinary check's core and smoke phases ran separately using an already staged, hash-verified native companion.
Its unchanged companion rebuild was omitted to keep all writes inside this checkout.
A short checkout-local temporary path was needed for the native Unix-socket tests.
Validation records are in `.dev-workflow/runs/20261029-165537-d4rtzoh2/` and `.dev-workflow/runs/20261029-165601-o8fhpyae/` at the repository root.

Linux deployment, service installation, authentication proxy configuration, resource exhaustion, and Windows were not exercised.
The systemd file is a deployment template, not evidence of a running Linux deployment.
