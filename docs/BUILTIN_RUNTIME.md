# Working with R, Python, and SQL

Console keeps data, imports, models, and database state in memory while the agent works.
A follow-up can inspect or modify what is already there instead of repeating the setup.

An error does not undo earlier changes.
Restarting clears live objects and database state; reconnecting starts a new session.
[Recordings](RECORDING.md) preserve a record of the work, not a saved copy of the running session.

## Choose the languages you need {#runtime-selection}

R and Python can each run without the other.
SQL uses a database connection supplied by one of them.
By default, Console prepares a Python environment and uses an in-memory DuckDB database for SQL; install R separately to use it.

You can [select an existing Python environment](CONFIGURATION.md#python-environment-selection) or an [R installation](CONFIGURATION.md#r-executable-selection).
An existing Python environment needs a supported, embeddable CPython installation, but no third-party packages are needed for standard-library Python code.
Install packages for the features you use, such as Matplotlib for plots or DuckDB for the default Python-owned SQL connection.

The Python client package is separate from the interpreter used for analysis.
See [Client integration](CLIENTS.md) when connecting from application code.

## R

Use R expressions and packages as you would in an interactive R session.
Visible top-level results are printed, and objects remain available to later code.

```r
measurements <- data.frame(value = c(2, 4, 6))
mean(measurements$value)
```

In a later cell:

```r
measurements$centered <- measurements$value - mean(measurements$value)
measurements
```

R reads its normal startup files by default.
See [R startup settings](CONFIGURATION.md#native-r-startup) to change that behavior.

### Read installed documentation {#installed-documentation}

The agent can use ordinary help calls such as `?stats::lm`, `help("lm", package = "stats")`, and `help.search("linear models")`.
Help pages and search results appear as text.

Installed HTML vignettes can also be read as text, including their evaluated results:

```r
vignette("topic", package = "package_name")
```

Replace the topic and package name with an installed vignette.
Figures are represented by captions and available alternative text, not image pixels.
PDF-only and source-only vignettes show their installed source instead.
See [documentation behavior](RUNTIME_REFERENCE.md#installed-documentation) for details and ways to inspect the original files.

Long help output can be captured and inspected in smaller pieces:

```r
text <- capture.output(print(?stats::lm))
writeLines(text[80:120])
```

## Python

Python code runs in a persistent workspace.
Imports and assignments remain available, and the final expression in a cell is displayed.

```python
import statistics

values = [2, 4, 6]
statistics.mean(values)
```

A later cell can use `values` without defining it again.
Ordinary exceptions print a traceback; earlier changes remain in place.
An uncaught main-thread `SystemExit` ends the runtime.

Console can prepare missing packages in a managed environment.
An existing environment uses its installed packages.
See [Packages and environments](REQUIREMENTS.md).

## Move data between R and Python {#r-and-python-interoperability}

The languages share one process, and reticulate supplies the bridge.
From Python, `r.name` accesses an R object.
For example, after the R example above:

```python
r.measurements
```

From R, `py$name` accesses a Python object:

```r
py$values
```

Conversions follow reticulate's rules; some conversions need additional Python packages.
You do not need to use the bridge for an analysis in one language.
[Runtime reference](RUNTIME_REFERENCE.md#r-and-python-interoperability) documents the helper functions and conversion behavior.

## SQL and DuckDB {#sql-and-duckdb}

SQL normally uses one in-memory DuckDB connection.
Tables and views remain available for later queries.

```sql
CREATE TABLE measurements_sql AS
SELECT * FROM (VALUES (2), (4), (6)) AS t(value);
```

```sql
SELECT avg(value) AS mean_value FROM measurements_sql;
```

When R owns the managed connection, SQL can refer to R global data frames by name.
With a Python-owned connection, register a data frame first:

```python
import pandas as pd

frame = pd.DataFrame({"value": [2, 4, 6]})
_console.sql_connection().register("measurements_python", frame)
```

The registered name can then be used in SQL.
A database table or view takes precedence over an R data frame with the same name.

### Use another database {#select-a-native-connection}

Select a native DBI connection from R or a DB-API connection from Python.
For example, Python's standard-library SQLite module provides SQL without installing DuckDB:

```python
import sqlite3

connection = sqlite3.connect(":memory:")
_console.sql_connection(connection)
```

Later SQL cells use that connection.
From R, the corresponding helper is `.console$sql_connection(connection)`.

Calling the helper without an argument returns the current connection owned by that language.
Passing `None` in Python or `NULL` in R restores the managed default.
Restoring the default does not close your connection or commit its transactions; you remain responsible for it.

See [SQL connections](RUNTIME_REFERENCE.md#sql-and-duckdb) for driver behavior, connection ownership, and startup configuration.

### Result previews

SQL previews show at most 20 rows and 12 columns, and long values may be shortened.
They are not complete exports.
Fetch and save the full result explicitly when you need the whole dataset.

## Plots and images

R plots are captured as PNG images at the end of a cell.
The default size is 800 × 600 pixels at 96 DPI.
Set dimensions in inches and resolution for later plots:

```r
options(
  console.plot.width_in = 10,
  console.plot.height_in = 6,
  console.plot.dpi = 100
)
```

A finished plot cannot be extended from a later cell.
Explicit user-created graphics devices are not automatically captured.

Python captures open Matplotlib pyplot figures at cell end and then closes them.
`plt.show()` captures immediately; calling it is optional.
A figure closed before it is shown is not captured, and figures outside pyplot are not captured automatically.

[Runtime reference](RUNTIME_REFERENCE.md#plots-and-images) covers device behavior and capture during errors or across the language bridge.

## Read results and keep the work {#output-and-notices}

Long output is shown as a preview with its beginning and recent end.
The agent can use the client's filesystem tools to inspect retained logs rather than repeat the calculation.
Output that was never printed, such as rows omitted by a database preview, is not present in those logs.

See [Recordings and reports](RECORDING.md) for file locations, retention limits, and report preparation.

## Current limitations

There is one session and one active cell per connection, not parallel cells or named sessions.
Restart discards live language state.
Recordings are not checkpoints, and Console does not provide a file-reading or search tool.

[Runtime reference](RUNTIME_REFERENCE.md) records detailed language behavior.
Client authors should also read [Calls, input, and control](SEND_OPERATIONS.md); ordinary setup does not require learning the MCP call protocol.
