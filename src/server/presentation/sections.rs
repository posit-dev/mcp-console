//! Named prose sections; selection belongs to the captured presentation profile.

pub(super) const BUILTIN_SCOPE: &str = "for calculations, data exploration and analysis, database queries, debugging, simulations, and plots.";

pub(super) const REUSE: &str =
    "Reuse objects across calls to inspect results, test hypotheses, and compare approaches.";

pub(super) const SEND_WORKFLOW: &str = " cell per call.";

pub(super) const DISPLAY: &str =
    " Results display automatically; supported plots return as images.";

pub(super) const SEND_ORDERING: &str = "Run one cell at a time. Wait for its `send` to return before another call with code. You can interrupt a pending call with `send(control=\"interrupt\")`. An error can leave earlier changes in place.";

pub(super) const POLLING: &str = "If work is still running, poll with an empty `send`; do not resubmit the cell. When waiting for completion, use `timeout_ms=300000` for a long poll; it returns early when work finishes or needs input. Avoid repeated short polls such as `timeout_ms=1000`.";

pub(super) const DEFAULTS: &str =
    r#" Defaults: `timeout_ms=60000` (60 seconds), `requirements.action="add"`."#;

pub(super) const OUTPUT: &str = "Text results, including notices, have an 8 KiB limit, retaining the beginning and latest tail; images have separate limits. Response notices report state and omitted-output locations.";

pub(super) const CUSTOM_CAPABILITIES: &str = "Language fields describe the configured interface; evaluation, display, SQL, and sharing depend on the worker. Console does not supply built-in runtime packages, import hooks, or a default SQL connection. Managed requirements need compatible worker callbacks; Python requirements are unavailable.";

pub(super) const CUSTOM_SELECTED_CAPABILITIES: &str = "Language fields describe the configured interface; evaluation, display, connections, and sharing depend on the worker. Console does not supply built-in runtime packages, import hooks, or a default database connection. Managed requirements need compatible worker callbacks; registry package preparation is unavailable.";

pub(super) const CUSTOM_SWITCHING: &str =
    " Switch languages when useful, using the worker's capabilities.";

pub(super) const SQL_SELECTION: &str = "For databases and structured files, consider DuckDB SQL first for inspection, joins, and aggregation. ";

pub(super) const R_SELECTION: &str =
    "Use R for vectorized data and string operations, statistics, and plots. ";

pub(super) const R_SCRIPT: &str = r#"For a reusable R script, include imports, data inputs, and `#| packages:`/`#| r-version:` metadata for `ir run script.R`, which starts without live Console objects. "#;

pub(super) const PYTHON_SELECTION: &str =
    "Use Python when its libraries or format-specific parsing simplify the task. ";

pub(super) const SWITCHING: &str = "Switch languages when useful.";

pub(super) const SQL_FILES: &str =
    "DuckDB can query CSV, Parquet, JSON, and JSONL directly, including nested JSON. ";

pub(super) const CUSTOM_R: &str = "Evaluate one complete R cell in the custom worker.";

pub(super) const CUSTOM_PYTHON: &str = "Evaluate one complete Python cell in the custom worker.";

pub(super) const CUSTOM_SQL: &str = "Evaluate one complete SQL cell; the custom worker supplies the connection, dialect, and display.";

pub(super) const R_RUNTIME: &str = r#"Evaluate one complete R cell in persistent global state. Visible top-level expressions autoprint; leave the primary result last. Load CRAN packages normally through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`; managed sessions prepare missing packages on first use. Avoid availability probes or `install.packages()`. `?pkg::topic` and `help.search()` print installed help; `vignette()` prints installed UTF-8/ASCII source. Capture long help with `text <- capture.output(print(?pkg::topic))`, then print line ranges with `writeLines()`."#;

pub(super) const R_BRIDGE: &str = " Access Python globals through `py$name`.";

pub(super) const R_SQL: &str = r#" `.console$sql_connection()` returns the active native DBI connection for DBI/dplyr, or errors if another runtime owns SQL. Select a user-owned DBI connection for SQL cells with `.console$sql_connection(connection)`; restore managed DuckDB with `.console$sql_connection(NULL)`. Never disconnect the managed connection; restore it before disconnecting a selection."#;

pub(super) const R_PLOTS: &str = r#" Default-device plots return as PNGs and finalize at cell end, including after errors; draw each plot in one cell. Explicit devices are not captured. Set persistent dimensions in inches and DPI with `options(console.plot.width_in = ..., console.plot.height_in = ..., console.plot.dpi = ...)`."#;

pub(super) const PYTHON_RUNTIME: &str = r#"Evaluate one complete Python cell in persistent `__main__` state. The final visible expression autoprints; leave the primary result last. Import packages normally; managed sessions prepare missing PyPI distributions on first import. Use `requirements.python` when import and distribution names differ or to prepare before execution."#;

pub(super) const PYTHON_BRIDGE: &str = " Access R globals and call functions through `r.name`.";

pub(super) const PYTHON_SQL: &str = r#" Select a user-owned DB-API connection for SQL cells with `_console.sql_connection(connection)`; restore managed DuckDB with `_console.sql_connection(None)`."#;

pub(super) const PYTHON_SQL_R: &str =
    " For R-owned DuckDB, bind Python frames to an R name through `r` before querying.";

pub(super) const PYTHON_SQL_CONNECTION: &str = r#" `_console.sql_connection()` returns the active native Python connection, or errors if SQL uses R; on Python-owned DuckDB, register frames with `_console.sql_connection().register(name, frame)` (globals are not scanned)."#;

pub(super) const PYTHON_SQL_CONNECTION_SELECTED: &str = r#" `_console.sql_connection()` returns the active native Python connection, or errors if another runtime owns SQL; on Python-owned DuckDB, register frames with `_console.sql_connection().register(name, frame)` (globals are not scanned)."#;

pub(super) const PYTHON_PLOTS: &str = r#" `matplotlib.pyplot.show()` returns open figures as PNGs immediately and closes them. At cell end, even after errors, remaining open figures return as PNGs and close; `show()` is optional. Closing an unshown figure suppresses capture."#;

pub(super) const PYTHON_R_PLOTS: &str = " R plots through `r` follow the R plot rules.";

pub(super) const CONTROL_START: &str =
    "Apply control before same-call stdin/code. `interrupt` requests ";

pub(super) const INTERRUPT: &str =
    "a cooperative interrupt and cancellation of dependency preparation, preserving live state";

pub(super) const CONTROL_END: &str = r#". A following cell runs only if prior work stops. `restart` discards live interpreter, database, debugger, and unread-input state; same-call stdin/code go only to the replacement."#;

pub(super) const CONTROL_END_SELECTED: &str = r#". A following cell runs only if prior work stops. `restart` discards live state, including debugger and unread input; same-call stdin/code go only to the replacement."#;

pub(super) const SQL_RUNTIME: &str = r#"Evaluate one complete SQL cell through the active connection. Managed DuckDB keeps a persistent catalog. Results are bounded table previews that abbreviate long text cells; query focused subsets or aggregates. The selected driver supplies its own SQL dialect and types."#;

pub(super) const SQL_R_FRAMES: &str = r#" With R-owned managed DuckDB, query R data frames by name; DuckDB tables/views take precedence."#;

pub(super) const SQL_R_STATEMENTS: &str =
    " Use DBI from an R cell for commands requiring the statement interface.";

pub(super) const TIMEOUT: &str = r#"Wait for output for up to this many milliseconds (default 60,000). When waiting for completion, use `timeout_ms=300000` for a long poll; it returns early when work finishes or needs input. Avoid repeated short polls such as `timeout_ms=1000`. `0` returns without waiting for completion. Expiry returns available output while work continues. This is a wait budget, not cancellation or a whole-call deadline: preparation and control can exceed it; standalone preparation has no timeout."#;

pub(super) const SQL_OPERATIONS: &str = r#" Managed conveniences/extensions apply only to DuckDB. Read SQLite with `ATTACH 'path' AS name (TYPE sqlite, READ_ONLY)`. With a sandbox, attach existing DuckDB databases outside writable paths using `ATTACH 'path' AS name (READ_ONLY)`. Use `SHOW TABLES`, `DESCRIBE`, `SUMMARIZE`, or `EXPLAIN`; CLI dot commands are not supported."#;

pub(super) const STDIN_START: &str =
    "Send stdin alone to answer an active read, prompt, or debugger. ";

pub(super) const STDIN_SELECTED: &str = STDIN_START;

pub(super) const STDIN_ORDERING: &str = r#"UTF-8 text is queued exactly; no newline is added, so line input normally needs a trailing `\n`. Empty text queues nothing. With a cell, input queues before code; an already waiting read may consume it first, including while interrupted work unwinds. Unread input can satisfy later reads; restart discards it."#;
