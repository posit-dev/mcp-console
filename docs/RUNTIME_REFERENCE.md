# Runtime behavior reference

Detailed language behavior for client authors and integrations.
Start with [Working with R, Python, and SQL](BUILTIN_RUNTIME.md) for the user guide.

One connection owns one persistent session.
Globals, imports, options, database state, and unread input survive between cells.
Cells are not transactions: an error can leave earlier changes in place.
Restart or worker loss discards all live language state.

Submit one complete R, Python, or SQL cell, inspect its result, and then submit the next.
[Calls, input, and control](SEND_OPERATIONS.md) covers polling, interactive input, interruption, and restart; [`send` API](API.md) lists the arguments.

## Runtime selection

[Configuration](CONFIGURATION.md#python-environment-selection) owns interpreter requirements and selection.
[Packages and environments](REQUIREMENTS.md) owns package defaults and preparation choices.
Language visibility changes tool fields, not the availability of a SQL provider.
Tool discovery is independent of [server readiness](SEND_OPERATIONS.md#server-readiness).

## R

R cells run in persistent global state with native console behavior.
Every visible top-level expression can autoprint.
The whole cell is parsed before evaluation, so a parse error runs none of it.
Evaluation or print errors preserve earlier effects and normally leave the worker usable.

R owns its normal startup files and workspace restoration; see [Native R startup](CONFIGURATION.md#native-r-startup).
Console supplies transport, interrupt, graphics, and documentation output integration.
R event handlers, including `later` callbacks, can run while idle and produce output collected by a later call.

Use ordinary package-loading operations.
In managed automatic mode, reached missing-package loads can prepare dependencies without rerunning the cell.
A sandboxed R worker also has a temporary writable library; manual installations there last only for that worker and remain subject to permissions.

### Installed documentation

Use ordinary R help calls such as `?stats::lm`, `help("lm", package = "stats")`, `??lm`, or `help.search("linear models")`.
Help pages, search results, package indexes, and vignette listings print through Console's text output.
R retains its lookup and ambiguous-topic selection behavior; answer a selection prompt through `stdin`.

Capture a long page and print the lines you need:

```r
text <- capture.output(print(?stats::lm))
writeLines(text[80:120])
```

`vignette()` prints readable text from an installed HTML vignette, including its evaluated results.
The vignette object's rendered-artifact metadata selects the file; Console does not execute its source or render it again.
Headings, paragraphs, lists, code indentation, simple tables, link destinations, captions, and available image alt text appear as text.
Scripts, styles, and embedded image payloads are omitted; linked assets are not fetched.
Figures appear only through their captions and alt text, and JavaScript-dependent content is unavailable.
The text does not expose image paths or a request mechanism for figure pixels.
To inspect an original figure, locate the installed HTML with `file.path(x$Dir, "doc", x$PDF)` from the retained vignette object and inspect its image references.
Images may be separate local files, embedded data, or remote URLs.
A client can use its own image tools or display a locally decoded figure through [ordinary plot capture](#plots-and-images).
Assignment stays quiet, so you can retain the ordinary vignette object and print it later.
Use `capture.output(print(vignette("topic", package = "pkg")))` to capture its text and print selected line ranges with `writeLines()`.

PDF-only, source-only, and other non-HTML vignettes retain the installed source view, labeled `Vignette source:`.
That view omits evaluated results and figures; source must be installed as UTF-8 or ASCII.
HTML contents must be UTF-8; conversion failures and missing source are errors.

Console installs text help and pager defaults after native R startup, once per worker.
Later changes to R options or print methods remain in effect until restart.
Custom pagers receive converted HTML through `file.show()` with its standard encoding behavior and `delete.file = TRUE`.
A pager that returns before reading the file must remove it after use.

## Python

Python cells execute in persistent `__main__.__dict__`; the final expression is displayed.
The whole cell is compiled before execution.
Ordinary exceptions print a traceback and leave the worker usable.
An uncaught main-thread `SystemExit` terminates the worker.

Console supplies `r` and `_console.sql_connection()` through Python's builtins; attaching reticulate may also bind `r` in `__main__`.
Alongside Python's standard workspace metadata and builtins, these are the intentional conveniences available without imports.
Console's internal imports, setup state, and exception helpers remain private.
Modules such as `os`, `sys`, and `tempfile` enter the workspace only when user code or startup hooks bind them.
Later setup and interrupt recovery preserve those user bindings; restart creates a fresh workspace.

Startup hooks run before cells, and attaching reticulate later preserves the running interpreter and its objects.
The working-directory import entry follows `os.chdir()`.
Managed automatic mode can resolve reached missing imports; existing environments require preinstalled packages.

Console does not provide a notebook event loop.
Manage asynchronous work explicitly.
Background threads and subprocesses can produce raw output, but managed dependency resolution is restricted to the owning interpreter thread.
Prepare packages before starting such work.

## R and Python interoperability

Reticulate supplies the bridge; see the [language guide](BUILTIN_RUNTIME.md#r-and-python-interoperability) for data-transfer examples.

Python globals are available from R through `py$name`.
Attribute access attaches the bridge on demand; loading reticulate or reading its `py` proxy alone need not initialize it.
Conversion follows reticulate's rules.
Objects and proxies do not survive worker replacement.

Console attaches four R helpers in `tools:mcp-console`:

| Helper            | Reticulate function           | Purpose                                 |
| ----------------- | ----------------------------- | --------------------------------------- |
| `py_import()`     | `reticulate::import()`        | Import a Python module.                 |
| `py_eval()`       | `reticulate::py_eval()`       | Evaluate a Python expression.           |
| `py_run_string()` | `reticulate::py_run_string()` | Run Python statements.                  |
| `py_require()`    | `reticulate::py_require()`    | Inspect or declare Python requirements. |

These aliases preserve reticulate's arguments, return values, and conversion rules.
Import, evaluation, and execution use Console's shared Python interpreter and state.
Installing or inspecting the helpers does not initialize Python, and ordinary R definitions can mask them.

For help, use `help("import", package = "reticulate")` for `py_import()`.
The other helpers use their matching reticulate help topics, for example `help("py_run_string", package = "reticulate")`.

`py_require()` without arguments returns the current R-side Python declarations.
In managed sessions, declarations use Console's existing [requirements and preparation](REQUIREMENTS_API.md#live-preparation) path.
Pre-initialization declarations are accepted when Python is prepared; MCP requirements inspection returns the last committed declaration.
Before Python initializes, `action = "set"` can replace requirements; after initialization, compatible additions preserve live objects.
Use [restart](REQUIREMENTS_API.md#restarting-with-requirements) for changes that require a fresh environment.

Ordinary Python does not need to attach reticulate, and R can run without Python.

## SQL and DuckDB

Managed SQL uses one in-memory DuckDB connection and persistent catalog.
R owns the default through DBI when available; otherwise Python owns it through DB-API.
No setup cell is required.
Missing optional providers can be prepared on demand in managed sessions.
A user-selected connection can provide SQL without DuckDB.

With R-owned DuckDB, unqualified names can refer to R global data frames; a table or view with the same name takes precedence.
Views can observe later rebinding of an R name.
To query a Python frame through this provider, first assign it to an R global.

With Python-owned DuckDB, register frames explicitly:

```python
_console.sql_connection().register("measurements", frame)
```

Here `frame` is an existing Python data frame.
Python globals are not automatically scanned into the SQL catalog.
DuckDB CLI dot commands are not supported.
Use read-only attachment for databases outside writable sandbox paths.

### Select a native connection

R exposes `.console$sql_connection()` and Python exposes `_console.sql_connection()`:

| Argument                | Result                                     |
| ----------------------- | ------------------------------------------ |
| Omitted                 | Return the exact active native connection. |
| DBI / DB-API connection | Select it for later SQL cells.             |
| `NULL` / `None`         | Restore the session's managed default.     |

Select SQLite from Python:

```python
import sqlite3

connection = sqlite3.connect(":memory:")
_console.sql_connection(connection)
```

Or from R:

```r
connection <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
.console$sql_connection(connection)
```

The latest selection controls SQL.
The getter only returns a connection owned by its own runtime; it reports the other runtime's helper when necessary.
Console does not provide interchangeable wrappers.

User connections remain user-owned.
Reset does not close them, commit their transactions, or discard the managed catalog.
Do not disconnect Console's managed connection.
A closed selected connection remains selected and reports its driver's errors until reset or reselection.

The driver owns SQL dialect, transaction behavior, and type mappings.
Console submits the complete source without retrying through another execution method.
Use DBI's statement interface directly from R when the driver requires it.

[Session startup source](CONFIGURATION.md#session-startup-source) can select a connection before cells run.
Reset does not rerun that source; restart does and may repeat its side effects.

### Result previews

SQL previews show at most 20 rows and 12 columns, with long values shortened.
They do not count the complete result.
The whole-response text budget can shorten them further.
A preview is not a data export: fetch and save results explicitly when you need the complete dataset.

## Plots and images

R's managed device returns PNG pages and finalizes open pages at cell end, including after language errors.
A later cell cannot add layers to a finalized plot.
Defaults are 800 × 600 pixels at 96 DPI.
Set persistent dimensions in inches and resolution:

```r
options(
  console.plot.width_in = 16,
  console.plot.height_in = 10.5,
  console.plot.dpi = 100
)
```

Explicit user devices are neither closed nor captured.
This includes devices that reuse a closed managed device's number and filename template.
Native R startup happens before the managed device attaches, so its plots use R's native device.

Python captures open pyplot figures at cell end, including after an exception, and then closes them.
`plt.show()` captures immediately and closes figures; calling it is optional.
`plt.pause()` captures frames while keeping figures open.
`savefig()` does not suppress capture, but closing an unshown figure does.
Figures outside pyplot are not captured.

An inherited `MPLBACKEND` is respected; otherwise Console uses Agg.
Matplotlib configuration can be read from the host.
Cache writes use private storage in `sandbox.tempdir: managed`; other modes retain normal library cache selection.
These plot rules also apply across the reticulate bridge.

## Output and notices

Language warnings and errors are ordinary console output.
Preparation, transport, and protocol failures are MCP tool errors.
[MCP results](API.md#results) defines status notices and [Calls, input, and control](SEND_OPERATIONS.md) defines readiness, replacement, and polling behavior.
[Output limits](API.md#output-limits) and [Recordings](RECORDING.md) describe previews, raw logs, and retention.

## Current limitations

There are no named sessions or parallel cells.
Recordings are not checkpoints and Console provides no file-read/search tool.
No aggregate retention quota or automatic cleanup is implemented.

Initialize R before starting background threads that access the native process environment; concurrent native environment access during R startup is unsupported.
Partial runtime initialization can require restart.

Platform enforcement and descendant cleanup have explicit [sandbox limits](SANDBOX.md#supported-hosts-and-lifetime-limits).
[Open work](TODO.md) distinguishes unresolved engineering items from deliberately unsupported features.
