# Requirements API

MCP argument objects, admission rules, and live package changes.
For startup packages and policies, see [Packages and environments](REQUIREMENTS.md).
Accepted declarations are not installed-package inventories or lockfiles.

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
Removing a declaration neither uninstalls a package nor prohibits its use; implicit resolution can add it again.

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

Under `implicit`, a reached missing package load can request preparation through `library()`, `require()`, `requireNamespace()`, `loadNamespace()`, `::`, or `:::`.
Console does not scan source or rerun the cell.

Automatic requests accept plain package names.
Use explicit requirements for remote references.
Available packages, explicit `lib.loc`, and ordinary help/listing operations retain their native behavior.
`install.packages()` is not intercepted.

Failure preserves the original loading operation's behavior, including `FALSE` from a missing-package `require()` or `requireNamespace()`.
A successfully accepted library remains available even if the subsequent load or cell fails.

## Automatic Python import resolution

Managed Python defaults to `explicit`: declare dependencies through `requirements.python` before importing them.
Set `python.resolution: implicit` in `config.yaml` to let a last-chance import finder prepare a missing distribution.
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

## Resolution policy admission

Configuration chooses a policy independently for R and managed Python:

| Policy                         | Prepare startup declaration | Explicit MCP changes | Automatic runtime additions |
| ------------------------------ | --------------------------- | -------------------- | --------------------------- |
| `implicit`                     | Yes                         | Yes                  | Yes                         |
| `explicit`                     | Yes                         | Yes                  | No                          |
| `fixed`                        | Yes                         | No                   | No                          |
| R `disabled` / existing Python | No                          | No                   | No                          |

Unchanged declarations remain no-ops.
A mixed request is checked as a whole before preparation.
`set` cannot omit a locked language to clear it, and `reset` or restart cannot unlock a policy.
A policy change requires a new server connection.

DuckDB extension preparation follows its managed provider's policy.
Selecting another SQL connection does not grant preparation authority.
These policies govern Console's resolver, not arbitrary installer code in cells or network permissions.
