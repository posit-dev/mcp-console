# Requirements and environments

Managed sessions prepare missing dependencies so code can use them.
Preparation makes a package available; it does not attach an R package, import a Python module, or load a DuckDB extension.

The server retains accepted requirements across worker restarts, but not across new server connections.
A declaration is **not** an installed-package inventory or a lockfile.

## Defaults and startup configuration

When not overridden, managed sessions start with these optional declarations:

| Runtime | Packages or extensions                                                                                    |
| ------- | --------------------------------------------------------------------------------------------------------- |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `arrow`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                                      |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                           |

The resolver skips downloads for extensions built into the selected DuckDB.
Loading remains a separate runtime operation.

Set `r.packages` or `python.managed.packages` to replace an optional package list.
`[]` means no optional packages; omission preserves defaults.
It does not remove required runtime infrastructure, ambient libraries, or caches.
R `resolution: disabled` also skips Console's R infrastructure preparation.

Existing Python environments use preinstalled packages and have no managed Python declaration.
Without R, they also use preinstalled DuckDB extensions.
A user-selected DB-API connection can provide SQL without DuckDB.

See [startup declarations](CONFIGURATION.md#startup-package-declarations) and [runtime selection](CONFIGURATION.md#python-environment-selection).

## Resolution policy admission

Configuration chooses a policy independently for R and managed Python:

| Policy                         | Prepare startup declaration | Explicit MCP changes | Automatic runtime additions |
| ------------------------------ | --------------------------- | -------------------- | --------------------------- |
| `automatic`                    | Yes                         | Yes                  | Yes                         |
| `explicit`                     | Yes                         | Yes                  | No                          |
| `startup_only`                 | Yes                         | No                   | No                          |
| R `disabled` / existing Python | No                          | No                   | No                          |

Unchanged declarations remain no-ops.
A mixed request is checked as a whole before preparation.
`set` cannot omit a locked language to clear it, and `reset` or restart cannot unlock a policy.
A policy change requires a new server connection.

DuckDB extension preparation follows its managed provider's policy.
Selecting another SQL connection does not grant preparation authority.
These policies govern Console's resolver, not arbitrary installer code in cells or network permissions.

## Inspecting and replacing requirements

The examples are MCP argument objects:

```json
{ "requirements": { "action": "get" } }
```

```json
{ "requirements": { "python": ["requests[socks]>=2,<3"] } }
```

```json
{
  "control": "restart",
  "requirements": { "action": "set", "python": ["requests>=2"] }
}
```

```json
{ "control": "restart", "requirements": { "action": "reset" } }
```

| Action          | Meaning                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------- |
| `get`           | Inspect the last committed declaration without consuming output or launching a worker.            |
| `add` (default) | Accumulate requirements. Exact repeats do nothing.                                                |
| `set`           | Replace the complete declaration. Omitted lists and constraints are empty; no defaults are added. |
| `reset`         | Restore the startup declaration captured from configuration, removing later additions.            |

`get` must be used alone: no code, control, stdin (even empty), or declaration fields.
`reset` accepts no declaration fields.
A bare requirements object `{}` is invalid; `{"action":"set"}` intentionally clears optional declarations.

Changed `set` or `reset` needs explicit restart after a worker has accepted user code or nonempty input.
An unused prewarmed worker can be replaced automatically after successful preparation.
Unchanged requirements do not wait for bootstrap.
When R is available, changed requirements wait for runtime bootstrap to finish; supply any requested startup input before changing requirements.
Configured session startup code and failed/interrupted native R startup end that exception.
An explicitly requested restart still occurs for unchanged requirements.

Inspection returns `requirements`, `prepared`, and `runtime_requirements` in structured content.
`requirements` is the editable user declaration; `runtime_requirements` identifies additional runtime infrastructure.
During resolution, inspection reports the last commit, not an in-progress candidate.
During initial startup it may instead return `[worker starting]`.

Copy and edit the complete `requirements` object, then add `action: "set"` to replace it.
Removing a declaration neither uninstalls a package nor prohibits its use; automatic resolution can add it again.

## Accepted requirement input

| Field            | Accepted values                                                                                                                                       |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`              | Single-line `ir` package references, including supported remote references. Local sources, NUL, and line breaks are rejected.                         |
| `python`         | Named PEP 508 registry requirements, with versions, extras, and markers. No paths, editable requirements, URLs, archives, or `name @ URL` references. |
| `duckdb`         | Extension names of at most 64 characters: lowercase ASCII letter first, then lowercase letters, digits, or underscores.                               |
| `python_version` | A list of versions or supported `==`, `!=`, `<`, `<=`, `>`, `>=` constraints, not executable paths.                                                   |
| `exclude_newer`  | A publication cutoff accepted by uv, such as `"2026-01-01"`, or null.                                                                                 |

Lists contain strings, not null.
`add` accepts at most 64 entries per language per call; `set` accepts a complete accumulated manifest without that per-list limit.

`add` accumulates Python version constraints and can fill an unset cutoff, but cannot replace an existing cutoff.
`set` clears omitted constraints and cutoffs.
`reset` restores the configured baseline.
Effective version-constraint changes on a live interpreter require restart.

## Requirements for a cell

Requirements may accompany one cell or stand alone.
Failed preparation withholds the accompanying cell.
Standalone success returns `[prepared]`; standalone preparation rejects nonempty stdin.
Its wait includes initial runtime discovery and is not limited by `timeout_ms`.

```json
{
  "requirements": { "python": ["polars>=1"] },
  "python": "import polars as pl; pl.DataFrame({'value': [1, 2, 3]})"
}
```

For ordinary calls, preparation precedes bundled input and code.
Supported interrupt-plus-cell calls are different: the interrupt and input are delivered before deferred requirement validation.
Those effects are not rolled back when the follow-up fails.
Python-only sessions reject interrupt-plus-requirements combinations.
See [operation order](SEND_OPERATIONS.md#operations).

## Automatic R package resolution

Under `automatic`, a reached missing package load can request preparation through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`.
Console does not scan source or rerun the cell.

Automatic requests accept plain package names.
Use explicit requirements for remote references.
Available packages, explicit `lib.loc`, and ordinary help/listing operations retain their native behavior.
`install.packages()` is not intercepted.

Failure preserves the original loading operation's behavior, including `FALSE` from a missing-package `require()` or `requireNamespace()`.
A successfully accepted library remains available even if the subsequent load or cell fails.

## Automatic Python import resolution

A last-chance import finder can prepare a missing distribution under `automatic`.
Existing standard-library, installed, local, and loaded modules retain normal lookup.
Availability checks such as `find_spec()` do not install packages.

Known mappings include `yaml` → `pyyaml`, `PIL` → `pillow`, and `sklearn` → `scikit-learn`.
Otherwise inference uses a conservative same-name fallback.
It declines ambiguous shared namespaces, missing submodules of an available package, and absent standard-library modules.
Missing optional imports during an installed package's eager initialization retain ordinary behavior.

Use explicit requirements for the correct distribution, versions, extras, or markers.
Preparation is available only on the configuring worker thread and process; prepare dependencies before starting background threads or fork children.
Existing Python environments and bare workers do not use this finder.

An accepted environment survives a later import error.
The cell is never replayed.

## Live preparation

Changed additions require an idle worker, except automatic requests within the worker's own execution.
Preparation is noninteractive; finish managed input or debugger work first.

### Live R preparation

Console changes its managed library entry while preserving other library paths and live objects.
Loaded namespaces are not unloaded.
Some activation failures leave recoverable state but require restart before further changes.

### Live Python preparation

A compatible addition preserves objects and the running interpreter.
It must match the loaded Python library and version and must not change a loaded distribution's version.
A new package can still change unloaded or transitive dependencies.
Use restart for changes that need fresh imports or a different environment.

Live R and Python activations have separate acceptance points: a successful Python activation can remain after a later R failure.
Arbitrary startup-hook effects are not rolled back.

### DuckDB extension preparation

Preparation uses the selected engine's installation and signature checks.
An extension-only addition does not replace a SQL connection or catalog.
A Python replacement that retains extension requirements must also supply a usable DuckDB provider; include `duckdb` explicitly when needed.

## Restarting with requirements

Console prepares and accepts the complete candidate before retiring the old worker.
Preparation failure preserves the old worker and declaration and withholds same-call input/code.
Once retirement begins, a replacement failure cannot restore live state.
A plain restart reuses accepted requirements without resolving again.

Downloads, builds, and cache writes can remain after any failed preparation.
Accepted declarations and filesystem effects have different lifetimes.

## Host resolution and trust

On macOS and Linux, preparation runs in a separate resolver sandbox, with host reads, permitted cache writes, and approved downloads.
Windows preparation and `--no-sandbox` use host permissions.
Package builds, interpreter hooks, resolvers, configuration, and package sources remain **trusted code and inputs**.

Capturing an executable path or configuration does not freeze its contents.
A worker permitted to rewrite a resolver wrapper, package source, installation, or cache can influence later preparation.
Keep those inputs outside worker-writable paths when relying on isolation.
Console cache separation is not validation of package code or protection for readable secrets.

Discovery and R preparation suppress startup profiles; worker `.Rprofile` settings do not configure the resolver.
[Resolver configuration](RESOLVER.md) owns caches, download permissions, and environment capture.

## Custom workers

Custom workers have no built-in defaults or managed Python preparation.
Explicit R/DuckDB preparation and optional runtime R callbacks require the current [worker protocol](WORKER_PROTOCOL.md#custom-worker-conformance).
They do not inherit the built-in SQL implementation.

## Saving an R script

A saved script must declare its own packages and inputs; it does not inherit Console's live objects or retained declaration.
[ir frontmatter](https://r-lib.github.io/ir/run.html) can describe packages and the R version for a separate `ir run script.R` invocation.
A recorded QMD is a starting point for editing, not automatic reproducible replay; see [Recordings](RECORDING.md#render-a-reviewed-copy).
