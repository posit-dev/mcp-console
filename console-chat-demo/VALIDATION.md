# Validation

App checks were rerun locally on macOS on November 5, 2026, after merging `main` at `c26e009a`.
Console was rebuilt from the merged working tree with its matching sandbox companion; all 21 app tests passed without compatibility changes.
Live browser conversations used `gpt-6.1-terra` through chatlas's Responses API in a local test launcher; the deployed app still uses the configured Anthropic model.

## App and process checks

After simplifying the chatlas callbacks and ShinyChat rendering, the app run passed all 22 tests, including installation and use of `humanize` in a managed environment.
The two HTTP app tests also passed after switching catalog cleanup to Shiny's public `on_shutdown()` hook.
The checks cover:

- Serving the app and public assets without a model session.
- R/Python state, plots, SQL, named sessions and package requests.
- Three concurrent clients, independent variables, cancellation during startup, protected server recordings and credentials, blocked networking and denied working-directory writes.
- Managed temporary writes and cleanup after the chatlas connection closes; no custom pool or reusable slots.
- Parquet views for the table tool, bounded previews, rejection of multiple SQL statements and denied reads outside the five bundled files.
- Interrupting a cancelled table query before closing its cursor, using an explicit checkpoint at the SQL driver boundary.
- Each answer offers Visualize, Investigate, and Model, with one R, one Python and one SQL choice.
  The public tool rejects repeated languages and alternates the plot language after completed answers, including when the agent replaces its choices within a turn.
  Code runs clear outdated choices; notes and polls preserve them.
  A retained SQL result is retrieved in R and reused as a data frame in Python.
- Separate exports for independent Console sessions, knitr selection for SQL-only exports, and execution of R, Python and SQL chunks from the exported report.
- The real chatlas tool loop clears stale suggestions after code runs and sends PNG image content to the model provider.
  This test scripts only the model's HTTP responses and uses a real Console runtime.

From the repository root:

```sh
TMPDIR="$PWD/console-chat-demo/.runtime/tmp" \
CONSOLE_CHAT_BINARY="$PWD/target/release/mcp-console" \
PYTHONDONTWRITEBYTECODE=1 \
  console-chat-demo/.venv/bin/python -B -m pytest console-chat-demo/tests \
  --basetemp=console-chat-demo/.runtime/pytest -q
```

Do not run these tests while rebuilding or staging Console: a changed companion can fail the binary's artifact identity check.
The cold package test can take several minutes.

## Browser and export checks

On November 5, two in-app browser clients ran on one Shiny process using the simplified session lifecycle.
One queried daily delays in SQL, retrieved them in R and drew an inline ggplot2 chart; Python then reused `r.daily_delays` and confirmed that the other client's variable was absent.
The other client counted airports and created its own Python variable, then ran a long cell that was stopped through the UI while the first client remained responsive.
Stop, New session and disconnect removed the clients' runtime directories and recordings.
These conversations used Console `0.0.6+dev.g74e81f4d49ef` before the merge; afterward, the app was restarted with the merged build and its opening page checked in the browser.

After the rendering cleanup, a live Terra conversation used the merged Console build to query flight counts in SQL, retrieve the result in R, draw an R chart and reuse the R data frame for a Python chart.
Both images loaded inline through ShinyChat's native renderer.
The three-row table view, expandable SQL output, code-format switch and one follow-up per language also worked.

The subsequent prompt and history cleanup passed all 22 app tests before and after the changes.
A live Terra conversation again queried in SQL, plotted in R, and reused `r.origin_counts` for a Python plot.
Follow-ups alternated the plot language, and the source-table preview showed all 16 airlines.
The report download endpoint returned a zip with all five Parquet files and SQL, R and Python chunks.
Both answers retained their prose from before and after tool calls exactly once, using chatlas's conversation history.

Earlier browser checks on October 31 drove a SQL starter question, an R bootstrap follow-up, Python reuse of its R result with an inline Matplotlib plot, table selection, and the code-format switch.
A second browser client had only its own table views.
Generated follow-ups were activated through their buttons.
The weather-model starter ran SQL queries, retrieved the result as `weather_delay_data` in R, and fit a Python model using `r.weather_delay_data`.
Its Python cells did not embed SQL or open a DuckDB connection.
The answer offered Visualize in R, Investigate in SQL and Model in Python, with small language badges.
Clicking Visualize reused the R data frame for an inline chart; the next choices were Visualize in Python, Investigate in SQL and Model in R.
The Python plot then used `r.visibility_summary` directly, without another query or retrieval cell, and the next Visualize option returned to R.
The app enforces the suggestion languages and alternating plot slots.
The SQL-to-R-to-Python preparation is prompt guidance, checked through the live conversation; the app does not parse or restrict the agent's submitted code.
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

After the merge, `scripts/preflight` and `scripts/check` passed, including companion staging, Clippy, all 53 Rust tests, a release build and all 17 smoke cases at default concurrency.
Console's core was unchanged by the subsequent app rendering refactor; these native checks were not repeated.
`scripts/format` reported success for Ruff, Yamark, rustfmt and Air on November 5.
The first native test run exceeded macOS's `SUN_LEN` with the longer temporary path.
Rerunning with `TMPDIR="$PWD/.tmp"` passed without code changes.

Linux/Connect deployment, service installation, authentication proxies, resource exhaustion and Windows were not revalidated in this audit.
The deployment files are templates, not evidence of deployment on a new host.
