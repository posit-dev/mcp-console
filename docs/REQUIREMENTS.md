# Packages and environments

By default, Console prepares environments and package libraries for your agent separately from those you use for other work.
No configuration file is required.

- **Python:** Console prepares its own Python environment, downloading a suitable interpreter when needed.
  Packages stay separate from your system Python and existing virtual environments.
- **R:** Console uses your installed R with separate package libraries.
  These are added to the Console session without changing the library paths of your other R sessions.
- **DuckDB:** Console prepares the SQL engine through its R or Python package, along with the configured extensions.

Prepared environments, packages, and extensions are kept in [Console's own caches](RESOLVER.md#cache-locations) for reuse across sessions.

The agent's code runs in an [OS-level sandbox](SANDBOX.md) by default, with restricted filesystem writes and direct network access.
Package preparation has [separate permissions](#host-resolution-and-trust), including its own sandbox on macOS, Linux, and Windows.

## Add packages as needed {#add-packages-during-an-analysis}

The agent can add compatible packages as an analysis develops without restarting the session or losing existing objects.
For Python, Console prepares and activates an updated virtual environment in the running interpreter.
For R, it updates the session's library paths.
Adding DuckDB extensions preserves the SQL connection and tables.

With explicit preparation, the agent declares the R packages, Python packages, or DuckDB extensions it needs in a tool call's `requirements` argument.
For example, the agent can request `plotnine` alongside the code that imports it:

```javascript
console.send({
  requirements: { python: ["plotnine"] },
  python: "import plotnine as p9",
});
```

Console prepares the requested packages or extensions before running the accompanying code.
The agent can also make a request on its own.

With implicit preparation, a call such as `library(palmerpenguins)` in R or `import plotnine` in Python can trigger preparation when a package is missing.
Console resolves the package and continues the load or import without rerunning the cell.

R defaults to `implicit`, which also allows explicit package requests.
Managed Python defaults to `explicit`; you can [choose `implicit`](#resolution-policy-admission) to enable automatic preparation on import.

Switching Python versions or changing package versions already in use can require a restart, which clears live objects.
See [Requirements API](REQUIREMENTS_API.md) for request details.

## Packages available at startup {#defaults-and-startup-configuration}

Console prepares these packages and extensions at startup:

| Runtime | Packages or extensions                                                                           |
| ------- | ------------------------------------------------------------------------------------------------ |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                             |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                  |

These are starter packages, not prerequisites for every analysis.
Built-in convenience defaults are prepared on a best-effort basis: Console reports each failed default, skips it, and continues with the other defaults.
Required runtime infrastructure remains strict, including packages used by the R/Python bridge and SQL adapters.
Default DuckDB extensions follow the same best-effort rule; an extension requested explicitly remains required.

Preparation makes packages available; the agent still uses ordinary imports, R package-loading calls, or database extension-loading commands in its code.
Extensions already built into the selected DuckDB do not need downloading.

## Choose startup packages

You can replace the default startup package lists in `config.yaml`:

```yaml
r:
  packages: [dplyr, ggplot2]
python:
  packages: [numpy, pandas]
```

An omitted list keeps the defaults.
An empty list (`[]`) removes the additional startup requests; it does not remove Console's required runtime packages, uninstall packages, or delete cached installations.
Packages listed explicitly in configuration or requested by the agent are required, even if they also appear in the built-in defaults.

[Interpreters and startup](RUNTIME_CONFIGURATION.md#startup-package-declarations) covers interpreter versions and startup settings.
[Package sources and caches](RESOLVER.md) covers download permissions and storage locations.

## Choose when packages may be prepared {#resolution-policy-admission}

R and managed Python each have a resolution policy.
The three policies allow progressively more package preparation: `fixed` → `explicit` → `implicit`.

### `fixed`: prepare packages at startup

Console prepares the startup packages and keeps that package declaration fixed for the connection.
The agent can use available packages, but cannot ask Console to add packages or change their versions during the session.

### `explicit`: also accept package requests

In addition to preparing startup packages as `fixed` does, Console accepts explicit package requests from the agent during the session.
The request can accompany the code that uses the package:

```javascript
console.send({
  requirements: { python: ["plotnine"] },
  python: "import plotnine as p9",
});
```

Ordinary R package-loading calls and Python imports use available packages; they do not trigger preparation on their own.
For managed Python, evaluated `reticulate::py_require()` calls also count as explicit dependency requests, including declarations from R package `.onLoad()` hooks.
They retain declared versions and extras and use the same preparation lifecycle as MCP requests.
See [Python declarations from R](REQUIREMENTS_API.md#python-declarations-from-r) for initialization and live-addition rules.

### `implicit`: also prepare packages on load or import

This adds automatic preparation to everything `explicit` allows.
When the agent's code tries to load a missing package, that operation can trigger Console to resolve and prepare it:

- In R, an ordinary call such as `library(palmerpenguins)` or `loadNamespace("palmerpenguins")` can prepare the missing package and continue loading it.
- In Python, an ordinary import such as `import plotnine` can prepare the missing package and continue importing it.

Preparation happens when execution reaches the load or import.
The agent does not need to make a separate package request, and Console does not rerun the whole cell.
Explicit package requests remain available when the agent needs to specify a version or other requirements.

### Set a policy

R defaults to `implicit`; managed Python defaults to `explicit`.
You can choose a policy for each in `config.yaml`:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  packages: [numpy, pandas]
  resolution: fixed
```

R's `disabled` policy turns off all managed R package preparation, including Console's R infrastructure.
For preinstalled-only Python, select an [existing environment](#use-an-existing-python-environment) rather than a managed one.

Changing a policy requires a new server connection.
These policies control Console's package preparation, not arbitrary installation commands in code or network permissions.
DuckDB extension preparation follows the policy of its managed provider.

## Use an existing Python environment

By default, Console prepares and manages its own Python environment.
You can instead use an environment you manage yourself, such as a project's virtual environment or a separate Python installation:

```yaml
python: .venv
```

Console uses the selected environment as supplied, without resolving or installing Python packages.
Console itself needs no third-party Python packages; a [compatible Python installation](RUNTIME_CONFIGURATION.md#python-environment-selection) with its standard library is sufficient.

Install the packages you want your agent to use in that environment, such as pandas for data frames, Matplotlib for plotting, and DuckDB for the default [Python-owned SQL connection](BUILTIN_RUNTIME.md#select-a-native-connection).
The session's filesystem and network permissions still apply.

## Package sources and permissions {#host-resolution-and-trust}

Console resolves and prepares packages in a separate native sandbox by default.
The resolver sandbox limits writes to dependency caches and temporary directories and restricts network access to allowed package and tooling hosts.
These permissions are separate from those of the agent's code.

Default package sources include PyPI for Python and CRAN through Posit Package Manager for R.
DuckDB extensions come from its core extension repository.

You can use other sources, such as a private Posit Package Manager instance or a company PyPI mirror, through the package managers' native settings.
See [Configure package sources](RESOLVER.md#configuration) for repository settings and download permissions.

`--no-sandbox` explicitly uses host permissions.
[Sandbox and trust](SANDBOX.md) describes the platform-specific limits.

## Recordings {#saving-an-r-script}

Package requirements are included in the session's [recordings](RECORDING.md), alongside code, output, and plots.

<span id="inspecting-and-replacing-requirements"></span> <span id="accepted-requirement-input"></span> <span id="requirements-for-a-cell"></span> <span id="automatic-r-package-resolution"></span> <span id="automatic-python-import-resolution"></span> <span id="live-preparation"></span> <span id="live-r-preparation"></span> <span id="live-python-preparation"></span> <span id="duckdb-extension-preparation"></span> <span id="restarting-with-requirements"></span>

For requirement actions, accepted input, and live-preparation semantics, see [Requirements API](REQUIREMENTS_API.md).

<span id="custom-workers"></span>

Continue with [Sandbox and trust](SANDBOX.md) to understand what the session and package preparation can access.
