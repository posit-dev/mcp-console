# Sessions and languages

Console gives your agent an interactive workspace for R, Python, and SQL.
You can ask it to explore data, refine an analysis, or try another approach while keeping the work already in memory.

## Work iteratively {#build-on-earlier-work}

Your agent can inspect or modify what's already in memory without repeating the setup.
For example, after the agent fits a statistical model, you can ask it to inspect the residuals or plot predictions without loading the data and fitting the model again.
An error does not undo changes made before the failure.

The agent can respond to interactive prompts and step through code in a debugger.
Long computations can keep running between calls while the agent collects results as they become available.
A call's wait timeout does not stop the computation.
The agent can request an interrupt or restart an unresponsive session.

<span id="current-limitations"></span>

Each connection has one session and runs one code cell at a time.
Restarting clears live objects and in-memory database state; reconnecting starts a new session.
Recordings preserve a record of the work, not a saved session that can be resumed.

## Combine languages in one session {#r-and-python-interoperability}

Console embeds R, Python, and DuckDB in the same process.
Your agent can switch languages between calls, choosing the libraries and syntax that fit each step.
It can reuse data across languages without exporting and reloading intermediate files.

For example, with R and the required packages available, the agent could load complete observations from the Palmer Penguins dataset in R:

```r
penguins <- na.omit(palmerpenguins::penguins)
```

Then summarize body mass by species with SQL:

```sql
SELECT species, avg(body_mass_g) AS mean_body_mass_g
FROM penguins
GROUP BY species
ORDER BY species;
```

And use Python to plot the relationship between flipper length and body mass:

```python
r.penguins.plot.scatter(x="flipper_length_mm", y="body_mass_g")
```

Here, SQL queries the R data frame through the default R-owned DuckDB connection, and Python accesses it through reticulate as `r.penguins`.
Conversions follow reticulate's rules; some conversions need additional Python packages, such as pandas for data frames.
The [runtime reference](RUNTIME_REFERENCE.md#r-and-python-interoperability) describes the language bridge and its helpers.

## Tailor the runtime {#runtime-selection}

<span id="r"></span> <span id="python"></span>

You can tailor Console to a project's languages and dependencies.
For example, [expose only Python and SQL](TOOL_CONFIGURATION.md#model-visible-languages), use an [existing Python environment](RUNTIME_CONFIGURATION.md#python-environment-selection), or select a specific [R installation](RUNTIME_CONFIGURATION.md#r-executable-selection).

An existing Python environment uses the packages already installed there.
For managed environments, you can [choose startup packages and control whether more may be prepared during the session](REQUIREMENTS.md).

R and Python can each run without the other.
SQL uses a database connection supplied by one of them.
To let your agent use R, [install it separately](getting-started.qmd#install).

## Work with databases {#sql-and-duckdb}

<span id="select-a-native-connection"></span>

Console can use any database with an R DBI or Python DB-API driver.
Your agent can create a connection object in R or Python and select it for subsequent SQL calls, which then run against that database.

By default, Console prepares an in-memory DuckDB database.
The agent can use it to query data frames and keep tables and views available between calls.

The selected connection determines the SQL dialect and transaction behavior.
Access to database files and servers depends on the session's [filesystem and network permissions](SANDBOX.md).

The [SQL connection reference](RUNTIME_REFERENCE.md#select-a-native-connection) shows how to select a connection from either language.
You can also [configure a database connection at startup](RUNTIME_CONFIGURATION.md#session-startup-source).

## Inspect help, plots, and results

Console formats help and output for agents, with affordances for inspecting results without accidentally flooding the model's context.

### Help and documentation {#installed-documentation}

Your agent can consult installed R help and package vignettes while it works.
Help pages, search results, and installed HTML vignettes are presented as text, including code examples and evaluated results from the vignettes.
Vignette figures are represented by captions and available alternative text.
The [documentation reference](RUNTIME_REFERENCE.md#installed-documentation) explains other formats and access to the original files.

### Plots and images

Console captures R plots and Matplotlib pyplot figures and returns them as images for the agent to inspect.
You can ask the agent to revise labels, change the scale, or try a different view of the same data.
It can generate a new plot using objects already in the session.

The [plotting reference](RUNTIME_REFERENCE.md#plots-and-images) covers image dimensions and which plotting methods are captured automatically.

### Large results {#output-and-notices}

Console limits how much text it sends back at once and collapses progress bar redraws.
For oversized output, it shows the beginning and recent end and identifies the retained log.
The agent can use its client's file tools to inspect that log without rerunning the computation.

<span id="result-previews"></span>

SQL previews show a limited number of rows and columns, and the log records that preview.
Your agent can [retrieve the last SQL result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result) in R or Python without rerunning the query, then analyze the data or save it to a file.
It can retrieve all rows at once or work through the result in chunks.

## Review and reuse the work

[Recordings](RECORDING.md) retain the code, output, and plots so you can see what the agent did and inspect its results.
Ask the agent for the recording's location.
The recorded Quarto document provides a starting point for a report; review its code, inputs, and package requirements before rendering it.

For precise language behavior, see the [runtime reference](RUNTIME_REFERENCE.md).
[Client integration](CLIENTS.md) covers applications that call Console directly.

Continue with [Packages and environments](REQUIREMENTS.md) to learn how Console prepares the libraries your agent uses.
