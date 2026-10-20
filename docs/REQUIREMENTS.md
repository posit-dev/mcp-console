# Packages and environments

In the [first session](getting-started.qmd), your agent could use Python libraries without you creating an environment or installing each package.
By default, Console prepares environments and package libraries for the agent separately from those you use for other work.

For Python, Console prepares its own environment and downloads a suitable interpreter when needed.
For R, it uses your installed R with separate package libraries, without changing the library paths of your other R sessions.
It also prepares DuckDB through its R or Python package to provide the default SQL connection.

Console keeps prepared packages, environments, and extensions in its own caches for reuse across sessions.
Starting another session can reuse those installations without resuming the previous session's live objects.
No configuration file is required.

Exact settings: [Package settings](PACKAGE_CONFIGURATION.md).

## Add packages as needed {#add-packages-during-an-analysis}

As an analysis develops, the agent may need another library.
For example, you could ask it to use seaborn to redraw the dice simulation's plot.
The agent can request compatible packages without restarting the session or losing existing objects.

The agent can declare the R packages, Python packages, or DuckDB extensions it needs in a tool call's `requirements` argument.
For example, the agent can request `seaborn` alongside the code that imports it:

```javascript
console.send({
  requirements: { python: ["seaborn"] },
  python: "import seaborn as sns",
});
```

Console prepares the requested packages or extensions before running the accompanying code.
The agent can also make a request on its own.

Adding a package can preserve the work in memory, but switching Python versions or changing package versions already in use can require a restart.

Reference: [Requirements](API.md#requirements) for request fields and which changes require a restart.

## Packages available at startup {#defaults-and-startup-configuration}

Console prepares a starter set of packages, including NumPy, pandas, and Matplotlib for Python and tidyverse packages for R.
Preparation makes them available; the agent still imports or loads the packages it uses.
If an optional starter package cannot be prepared, Console reports the failure and continues with the other defaults.

You can declare packages at startup when a project needs a particular set.
Packages you explicitly declare are required, so a failure to prepare one prevents Console from proceeding with an incomplete environment.

Exact settings: [Startup package declarations](PACKAGE_CONFIGURATION.md#startup-package-declarations) and [default packages and extensions](PACKAGE_CONFIGURATION.md#default-startup-packages).

## When packages are prepared {#resolution-policy-admission}

By default, R can prepare a missing package when the agent calls `library(palmerpenguins)`.
Console resolves the package and continues the load without rerunning the cell.
The agent can also request R packages through `requirements`.
Managed Python instead waits for an explicit package request, such as the seaborn example above; an ordinary import does not trigger preparation.
Evaluated `reticulate::py_require()` calls also count as explicit requests for managed Python, including declarations made when an R package loads.
They retain declared versions and extras and use the same preparation lifecycle as MCP requests.

You can let missing Python imports trigger preparation too, require explicit requests in R, or keep either language's startup declaration fixed for the session.
A fixed declaration lets the agent use the packages prepared at startup while rejecting later additions through Console.
Each language can have its own policy.
These policies control Console's package preparation; they do not grant network access or restrict arbitrary installation commands in submitted code.

Exact settings: [Resolution policies](PACKAGE_CONFIGURATION.md#dependency-resolution-policies).
See [Python declarations from R](API.md#python-declarations-from-r) for initialization and live-addition rules.

## Use an existing Python environment

By default, Console prepares and manages its own Python environment.
You can instead use an environment you manage yourself, such as a project's virtual environment or a separate Python installation:

```yaml
python: .venv
```

Console uses the selected environment as supplied, without resolving or installing Python packages.
Install the packages you want your agent to use in that environment, such as pandas for data frames, Matplotlib for plotting, and DuckDB for the default SQL connection.
The session's filesystem and network permissions still apply.

Interpreter selection: [Python environments](RUNTIME_CONFIGURATION.md#python-environment-selection).

## Package sources and permissions {#host-resolution-and-trust}

Console resolves and prepares packages in a separate native sandbox by default.
The resolver sandbox limits writes to dependency caches and temporary directories and restricts network access to allowed package and tooling hosts.
These permissions are separate from those of the agent's code.

Default package sources include PyPI for Python and CRAN through Posit Package Manager for R.
DuckDB extensions come from its core extension repository.

You can use other sources, such as a private Posit Package Manager instance or a company PyPI mirror, through the package managers' native settings.
Installing a package does not give the agent's code those download or cache permissions.

Exact settings: [Package sources and preparation permissions](PACKAGE_CONFIGURATION.md#configuration).

## Recordings {#saving-an-r-script}

Package requirements are included in the session's [recordings](RECORDING.md), alongside code, output, and plots.

<span id="inspecting-and-replacing-requirements"></span> <span id="accepted-requirement-input"></span> <span id="requirements-for-a-cell"></span> <span id="automatic-r-package-resolution"></span> <span id="automatic-python-import-resolution"></span> <span id="live-preparation"></span> <span id="live-r-preparation"></span> <span id="live-python-preparation"></span> <span id="duckdb-extension-preparation"></span> <span id="restarting-with-requirements"></span>

<span id="custom-workers"></span>

Continue with [Sandbox and trust](SANDBOX.md) to understand what the session and package preparation can access.
