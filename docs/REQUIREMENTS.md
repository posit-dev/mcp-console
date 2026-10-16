# Packages and environments

Console can prepare packages for the agent or use an existing Python environment.
Preparing a package makes it available; the analysis still uses ordinary imports, R package-loading calls, or database extension-loading commands.

No configuration file is required for managed preparation.

## Defaults and startup configuration

By default, managed sessions request these optional packages and extensions:

| Runtime | Packages or extensions                                                                                    |
| ------- | --------------------------------------------------------------------------------------------------------- |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `arrow`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                                      |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                           |

These are starter packages, not prerequisites for every analysis.
Extensions already built into the selected DuckDB do not need downloading.

Supply a package list to replace a managed runtime's optional defaults:

```yaml
r:
  packages: [dplyr, ggplot2]
python:
  managed:
    packages: [numpy, pandas]
```

An omitted list keeps the defaults; `[]` requests no optional packages.
An empty list does not remove runtime infrastructure, existing packages, or cached installations.

[Configuration](CONFIGURATION.md#startup-package-declarations) covers interpreter versions and startup settings.
[Package preparation and caches](RESOLVER.md) covers download permissions and storage locations.

## Use an existing Python environment

To use a project's environment:

```yaml
python: .venv
```

Console uses the packages already installed there and does not manage Python package installation for that selection.
Standard-library Python code needs no additional packages.
Install third-party packages for the analysis you plan to run: for example, pandas for pandas operations, Matplotlib for pyplot plots, or DuckDB for the default Python-owned SQL connection.

A user-selected SQLite connection can provide SQL using Python's standard library.
See [database connections](BUILTIN_RUNTIME.md#select-a-native-connection).

The interpreter must still meet Console's [existing-environment requirements](CONFIGURATION.md#python-environment-selection), including the CPython version and shared embedding library.
Selecting an existing environment changes package management; it does not grant extra filesystem or network permissions.

## Choose when packages may be prepared {#resolution-policy-admission}

R and managed Python each have a resolution policy:

| Policy                           | Startup packages       | Explicit package requests | Missing package loads/imports |
| -------------------------------- | ---------------------- | ------------------------- | ----------------------------- |
| `automatic` (default)            | Prepared               | Allowed                   | May prepare packages          |
| `explicit`                       | Prepared               | Allowed                   | Do not prepare packages       |
| `startup_only`                   | Prepared               | Not allowed               | Do not prepare packages       |
| R `disabled`, or existing Python | No managed preparation | Not allowed               | Use installed packages        |

For example:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  managed:
    packages: [numpy, pandas]
    resolution: startup_only
```

R's `disabled` policy also skips Console's R infrastructure preparation.
For preinstalled-only Python, select an existing environment rather than a managed one.

Changing a policy requires a new server connection.
These policies control Console's package preparation, not arbitrary installation commands in code or network permissions.
DuckDB extension preparation follows the policy of its managed provider.

## Add packages during an analysis

In the default automatic mode, reached missing-package loads in R and missing imports in Python can request preparation.
Console does not rerun the whole cell to make this happen.

Python import names and package-distribution names can differ.
The agent can make an explicit package request when it needs a particular distribution, version, or extra.
Existing Python environments do not use automatic import preparation.

Compatible additions can keep live objects.
Replacing a live environment or changing versions already in use can require a restart, which clears live state.
Removing a package from the declaration does not uninstall it or prohibit its use.

Client authors can find the exact request syntax, action meanings, and failure behavior in [Requirements API](REQUIREMENTS_API.md).

## Trust and download permissions {#host-resolution-and-trust}

Package installation and build hooks execute code.
Use trusted package sources, tools, and configuration.

On macOS and Linux, preparation normally runs in a separate sandbox with host reads, cache writes, and permitted downloads.
Windows preparation and `--no-sandbox` use host permissions.
Permissions for the analysis and package preparation are independent.

Keep resolver executables, wrappers, installations, package sources, and configuration outside paths the analysis can modify when relying on that separation.
Cache separation does not inspect package code or protect readable secrets.

See [Package preparation and caches](RESOLVER.md) for mirrors, credentials, and cache configuration, and [Sandbox and trust](SANDBOX.md) for limits.

## Save an analysis {#saving-an-r-script}

Accepted package requests persist across a session restart, not across new server connections.
They are not a lockfile or an inventory of every installed package.

A saved script must declare its own packages and inputs; it does not inherit live objects or the session's package declaration.
An R script can use [ir frontmatter](https://r-lib.github.io/ir/run.html) for a separate `ir run script.R` invocation.

A recorded QMD is a starting point for a report, not automatic reproducible replay.
See [Render a reviewed copy](RECORDING.md#render-a-reviewed-copy).

<span id="inspecting-and-replacing-requirements"></span> <span id="accepted-requirement-input"></span> <span id="requirements-for-a-cell"></span> <span id="automatic-r-package-resolution"></span> <span id="automatic-python-import-resolution"></span> <span id="live-preparation"></span> <span id="live-r-preparation"></span> <span id="live-python-preparation"></span> <span id="duckdb-extension-preparation"></span> <span id="restarting-with-requirements"></span>

For requirement actions, accepted input, and live-preparation semantics, see [Requirements API](REQUIREMENTS_API.md).

<span id="custom-workers"></span>

Custom workers follow the [worker conformance contract](WORKER_PROTOCOL.md#custom-worker-conformance).
