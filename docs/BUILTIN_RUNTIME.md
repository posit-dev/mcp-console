# Sessions and languages

Console gives your agent an interactive workspace for R, Python, and SQL.
You can ask it to explore data, refine an analysis, or try another approach while keeping the work already in memory.
Your client connects to Console and gives the agent a tool for running code in that session.
The same session model applies whether you use an MCP client, the R package, or the Python package.

For exact helper names and execution behavior, see [Language behavior](RUNTIME_REFERENCE.md).

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

The [memory guard](RUNTIME_REFERENCE.md#memory-guard) checks the worker and its descendants every five seconds.
If their resident memory exceeds 90% of total host RAM, Console stops the session and reports why.
Use a smaller dataset or process data in chunks before trying again; the stopped session's objects are lost.

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

Reference: [R and Python interoperability](RUNTIME_REFERENCE.md#r-and-python-interoperability).

## Use the languages you need {#runtime-selection}

<span id="r"></span> <span id="python"></span>

R and Python can each run without the other.
SQL uses a database connection supplied by one of them.
Console can prepare Python for you; to let your agent use R, [install it separately](CLIENTS.md#r-python-and-sql).

Console prepares packages in its own environments and libraries by default.
The [Packages and environments](REQUIREMENTS.md) guide explains how those packages become available during an analysis.
You can also use an existing Python environment, select an R installation, or limit the languages exposed to the agent.

Exact settings: [Languages and startup](RUNTIME_CONFIGURATION.md).

## Work with databases {#sql-and-duckdb}

<span id="select-a-native-connection"></span>

Console can use any database with an R DBI or Python DB-API driver.
By default, Console prepares an in-memory DuckDB database.
The agent can use it to query data frames and keep tables and views available between calls.

With the R DuckDB package 2.0.0, an interrupt can leave a SQL query running while Console fetches its result through Arrow.
Restart the session if the query does not stop; restarting clears its in-memory state.
This known limitation is tracked in [open runtime work](TODO.md#runtime-and-resource-limits).

To work with another database, your agent can create a connection object in R or Python and select it for subsequent SQL calls.
The selected connection determines the SQL dialect and transaction behavior.
Access to database files and servers depends on the session's [filesystem and network permissions](SANDBOX.md).

Reference: [SQL connection helpers](RUNTIME_REFERENCE.md#select-a-native-connection) and [database startup settings](RUNTIME_CONFIGURATION.md#session-startup-source).

## Inspect help, plots, and results

The agent can consult package documentation and inspect results as it works.
Console returns text and images while limiting how much text reaches the model at once.

### Help and documentation {#installed-documentation}

Your agent can consult installed R help and package vignettes while it works.
Help pages, search results, and installed HTML vignettes are presented as text, including code examples and evaluated results from the vignettes.

Reference: [Supported documentation formats and original files](RUNTIME_REFERENCE.md#installed-documentation).

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
Your agent can retrieve the last SQL result in R or Python without rerunning the query, then analyze the data or save it to a file.
It can retrieve all rows at once or work through the result in chunks.

Reference: [Retrieve the last SQL result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result).

## Review and reuse the work

[Recordings](RECORDING.md) retain the code, output, and plots so you can see what the agent did and inspect its results.
Ask the agent for the recording's location.
The recorded Quarto document provides a starting point for a report; review its code, inputs, and package requirements before rendering it.

Continue with [Packages and environments](REQUIREMENTS.md) to learn how the agent gets the libraries it uses.
