# Validation

Validated locally on macOS on October 31, 2026, with Console `0.0.6+dev.g74e81f4d49ef` and its matching sandbox companion.
Live browser conversations used `gpt-6.1-terra` through chatlas's Responses API in a local test launcher; the deployed app still uses the configured Anthropic model.

## App and process checks

The full 19-test suite passed before the final cancellation regression was added, including a cold managed environment that installed and imported `humanize`.
After that regression and the export changes, all 19 tests excluding the already-exercised dependency download passed against the rebuilt Console binary.
The checks cover:

- Serving the app and public assets without a model session.
- R/Python state, plots, SQL, named sessions and package requests.
- Concurrent clients, slot reuse, cancellation during startup, private files, credentials, blocked networking and denied host writes.
- Parquet views for the table tool, bounded previews, rejection of multiple SQL statements and denied reads outside the five bundled files.
- Interrupting a cancelled table query before closing its cursor, using an explicit checkpoint at the SQL driver boundary.
- Rejection of follow-ups in the primary runtime, with the most recent runtime breaking a tie.
- Separate exports for independent Console sessions, knitr selection for SQL-only exports, and execution of R, Python and SQL chunks from the exported report.

From the repository root:

```sh
TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
CONSOLE_CHAT_BINARY="$PWD/target/release/mcp-console" \
PYTHONDONTWRITEBYTECODE=1 \
  console-chat-demo/.venv/bin/python -B -m pytest console-chat-demo/tests \
  --basetemp=console-chat-demo/.runtime/pytest -q
```

Do not run these tests while rebuilding or staging Console: a changed companion can fail the binary's artifact identity check.
The cold package test can take several minutes; the final local run omitted it with `-k 'not dynamic_resolution'` after it had passed.

## Browser and export checks

The in-app browser drove real conversations on one Shiny process: a SQL starter question, an R bootstrap follow-up, Python reuse of its R result with an inline Matplotlib plot, table selection, and the code-format switch.
A second browser client had only its own table views.
Generated follow-ups were activated through their buttons.
A downloaded zip contained the QMD source and all five Parquet files; `quarto render analysis.qmd --to html` produced the report.
The process test also runs the exported Python chunk, rather than treating it as text.

[tests/browser_check.py](tests/browser_check.py) contains repeatable Chromium checks, including concurrent clients and Stop.
Its live modes make billed model requests using the running server's credentials.

```sh
TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
  console-chat-demo/.venv/bin/python -B console-chat-demo/tests/browser_check.py \
  --browser '/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' --live
```

## Console checks and limits

`scripts/preflight` and the core checks passed, including Clippy and all 53 Rust tests.
`scripts/format` reported success for Ruff, Yamark, rustfmt and Air.
Use a short checkout-local `TMPDIR` for native socket tests on macOS; a longer path exceeded `SUN_LEN` in the first attempt.
All 17 smoke cases passed with `scripts/test --jobs 2`.
The first smoke run at default concurrency had four failures involving busy workers or missing results; the lower-concurrency rerun passed without code changes.

Linux/Connect deployment, service installation, authentication proxies, resource exhaustion and Windows were not revalidated in this audit.
The deployment files are templates, not evidence of deployment on a new host.
