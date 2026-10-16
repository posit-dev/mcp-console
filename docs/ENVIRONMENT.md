# Environment variables

Prefer [configuration](CONFIGURATION.md) for settings with a public YAML equivalent.
This reference covers environment values that can affect Console startup and execution; it is not a catalog of every variable read by R, Python, compilers, or imported packages.

## Where to set variables

**Server settings** must be present in the environment launching `mcp-console serve`.
For example, setting `MCP_CONSOLE_HOME` inside a cell or YAML `environment` does not move the running server's configuration or recordings.

**Worker settings** inherit launch values, then apply the shared `environment` mapping.
**Resolver settings** additionally apply `resolver.environment`:

```yaml
environment:
  PYTHONPATH: /work/shared/python
  MPLBACKEND: agg
resolver:
  environment:
    UV_HTTP_TIMEOUT: "120"
```

`inherit_environment: false` removes inherited workload values; `resolver.inherit_environment` defaults to the shared setting and can override it.
Explicit mappings still apply.
This does not clear the trusted supervisor's own environment.

All YAML environment values are strings.
Quote numbers and booleans.
Empty strings are values, not unset instructions; null is invalid.
Shared values, including credentials, also reach preparation.

Console subsequently applies its runtime, cache, temporary-storage, proxy, and transport assignments.
These win over conflicting inherited/YAML values.
On Windows, that precedence also applies to differently cased names.
Managed temporary-storage assignments apply only with `sandbox.tempdir: managed`.
On Windows, `read_write` supplies the granted host default as `TMPDIR`, `TEMP`, and `TMP` when all three child values are unset or empty.
Other configured/inherited temp values are preserved; `--no-sandbox` adds no temporary-environment assignments.
Worker startup files and cells can change their own environment afterward, but not the separate resolver's captured configuration.
A new server connection reloads launch settings; worker restart does not.
It does rerun R's native startup files.

## Server paths and executable discovery

| Variable            | Console-specific effect                                                                                                                                                                                                                 |
| ------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `MCP_CONSOLE_HOME`  | Absolute Console home for global `config.yaml` and fallback session storage. Defaults to the user's `.agents/console`; does not move package caches or project-local storage. Empty/relative values fail when needed; no `~` expansion. |
| `HOME`              | Home discovery and leading-`~` expansion for public interpreter paths. Those uses need an absolute home. Child overrides affect runtime/tool behavior, not server configuration discovery.                                              |
| `USERPROFILE`       | Windows fallback for home/cache discovery when the relevant HOME value is unavailable; native home discovery can also use the Windows profile API.                                                                                      |
| `PATH`              | R, resolver, and subprocess discovery. The selected Python executable directory is added during worker initialization/activation. Windows R discovery supports `.exe`, `.bat`, and `.cmd`.                                              |
| `R_HOME`            | Selects an R installation directory unless public `r` selection overrides it. Presence, including empty/invalid values, can cause a selection error instead of Python-only fallback.                                                    |
| `RETICULATE_PYTHON` | Legacy launch-time existing-Python selection, also recognized without R. Empty, absent, and literal `managed` are not explicit selections. Public `python` configuration wins.                                                          |
| `RETICULATE_UV`     | uv selection for R/reticulate-backed Python preparation. R-free preparation ignores it and uses PATH uv.                                                                                                                                |
| `SystemRoot`        | Windows supervisor preflight uses it to locate `System32/cmd.exe`.                                                                                                                                                                      |

Use [interpreter selection](CONFIGURATION.md#python-environment-selection) for projects rather than depending on ambient activation.
In particular, `VIRTUAL_ENV` selects Python only through the configured `active_venv` choice; `UV_PYTHON` is not a substitute for `python:`.

## Python and reticulate

| Variable                             | Effect during built-in Python setup                                                                                                                |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| `PYTHONPATH`                         | Additional import paths, not an interpreter selector.                                                                                              |
| `RETICULATE_PYTHONPATH`              | Replaces `PYTHONPATH` when present, even empty, including without R.                                                                               |
| `PYTHONHOME`, `PYTHONPLATLIBDIR`     | Removed before native initialization; Console uses the inspected embedding configuration.                                                          |
| `VIRTUAL_ENV`                        | Removed from managed uv preparation. In the worker it reflects the selected virtualenv, or is removed for a non-virtualenv Python.                 |
| `RETICULATE_CHECK_REQUIRED_PACKAGES` | Reticulate virtualenv compatibility check; enabled by default. Case-insensitive `true`, `1`, or `yes` enable it; other supplied values disable it. |
| `RETICULATE_ENABLE_PYTHON_FINALIZER` | Reticulate exit finalization opt-in; disabled by default. The same true values enable it.                                                          |

Changing a selector after initialization does not switch the live interpreter.
Use restart with supported managed requirement changes, or start a new server for a different configured selection.

R-side compatibility discovery may also interpret `RETICULATE_PYTHON_ENV`, `RETICULATE_PYTHON_FALLBACK`, `RETICULATE_USE_MANAGED_VENV`, and `WORKON_HOME`.
They are not universal selectors in Console's R-free path and do not override a running interpreter.
See reticulate's [discovery rules](https://rstudio.github.io/reticulate/articles/versions.html#order-of-discovery).

## Python dependency preparation: `UV_*`

The resolver captures the open-ended `UV_*` family.
Most values retain their uv meaning, subject to the command and Console's explicit options.
Important exceptions are:

| Variable                                                 | Console handling                                                                                                                                                                   |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `UV_OFFLINE`                                             | Removed from managed preparation. Built-in workers separately set it to `1`, including with `--no-sandbox`. It does not describe resolver networking or enforce process isolation. |
| `UV_NO_CACHE`                                            | Removed when creating a retained environment, because a no-cache environment would be deleted on command exit. It can still reach version discovery.                               |
| `UV_PYTHON_PREFERENCE`                                   | Set to `only-managed` for managed discovery/preparation.                                                                                                                           |
| `UV_MANAGED_PYTHON`, `UV_NO_MANAGED_PYTHON`, `UV_PYTHON` | Removed from managed preparation; they cannot override Console's selected managed interpreter.                                                                                     |
| uv cache/install paths                                   | Replaced in Console cache mode; participate in native cache selection in host mode.                                                                                                |

Package indexes, credentials, mirrors, TLS, build controls, and timeouts remain uv settings.
Mirror configuration does not grant sandbox access to the mirror.
See [uv's reference](https://docs.astral.sh/uv/reference/environment/) and [resolver networking](RESOLVER.md#configuration).

## Cache and data locations

[Resolver configuration](RESOLVER.md#cache-locations) owns root selection and host-cache permissions.
Let `D` be the selected Console dependency directory.
`cache: console` applies these assignments to preparation and eligible workers:

| Variables                                | Values                                                                                |
| ---------------------------------------- | ------------------------------------------------------------------------------------- |
| `XDG_CACHE_HOME`, `XDG_DATA_HOME`        | `D`, `D/data`                                                                         |
| `UV_CACHE_DIR`                           | `D/uv/cache`                                                                          |
| `UV_PYTHON_INSTALL_DIR`                  | `D/uv/python`                                                                         |
| `UV_PYTHON_BIN_DIR`, `UV_TOOL_BIN_DIR`   | `D/uv/bin`                                                                            |
| `UV_TOOL_DIR`                            | `D/uv/tools`                                                                          |
| `IR_CACHE_DIR`, `IR_LIBRARY_ROOT`        | `D/ir`, `D/ir/libraries`                                                              |
| `R_USER_CACHE_DIR`, `R_USER_DATA_DIR`    | `D`, `D/data`                                                                         |
| `RENV_PATHS_ROOT`, `RENV_PATHS_CACHE`    | `D/renv`, `D/renv/cache`                                                              |
| `RENV_PATHS_SOURCE`, `RENV_PATHS_BINARY` | `D/renv/source`, `D/renv/binary`                                                      |
| `PKG_CACHE_DIR`, `R_PKG_CACHE_DIR`       | `D/R/pkgcache`, `D`                                                                   |
| `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` | `D/duckdb/extensions`                                                                 |
| `MPLCONFIGDIR`                           | `D/matplotlib`                                                                        |
| `PYTHONPYCACHEPREFIX`                    | `D/python/bytecode`                                                                   |
| `PYTHONUSERBASE`                         | `D/python/user`, except existing Python preserves its configured/inherited user base. |

With `sandbox.tempdir: managed`, the worker subsequently places `XDG_CACHE_HOME` and `MPLCONFIGDIR` under private `TMPDIR`.
Values observed inside a cell are therefore not necessarily the persistent resolver cache locations.

With `cache: host`, tool variables/defaults select caches and Console generates appropriate resolver grants.
An explicit resolver filesystem replaces those generated entries.
`MCP_CONSOLE_HOME` does not move caches.
Windows cache-root selection uses absolute `XDG_CACHE_HOME`, then absolute `LOCALAPPDATA`, then its home/profile fallback.

Despite its prefix, `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` is a supported user cache setting in host mode.
A relative nonempty value resolves against the launch directory; otherwise managed extensions use the resolver home's `.duckdb/extensions`.
It is captured before execution and does not itself grant filesystem access.

## R startup and libraries

Normal R startup runs inside the worker by default.
R interprets `R_ENVIRON`, `R_ENVIRON_USER`, `R_PROFILE`, `R_PROFILE_USER`, `R_LIBS`, `R_LIBS_USER`, `R_LIBS_SITE`, and `R_DEFAULT_PACKAGES` under its [native startup rules](https://stat.ethz.ch/R-manual/R-devel/library/base/html/Startup.html).
`r.vanilla: true` suppresses normal startup files/workspace restoration; it is not general environment clearing.

Managed preparation prepends its library to captured `R_LIBS`; native startup or user code can alter `.libPaths()` afterward.
Console supplies `R_HOME`, `R_SHARE_DIR`, `R_INCLUDE_DIR`, and `R_DOC_DIR` from the selected installation rather than retaining mismatched resource paths.
On Windows, embedded R home uses nonempty `R_USER`, then `HOME`, then the native profile fallback.

Discovery and preparation suppress profile/environment files.
A worker `.Rprofile` or `.Renviron` is therefore not a resolver configuration mechanism.
Put mirrors and preparation credentials in the resolver environment instead.
Managed R preparation also supplies `IR_NO_LOCAL_SOURCES=1` and a package-subprocess timeout; the latter is not a cell timeout.

## Temporary storage, plotting, and native loading

| Variable                              | Built-in behavior                                                                                                                                                    |
| ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `TMPDIR`                              | Redirected to private storage with `sandbox.tempdir: managed`; otherwise retains normal runtime meaning. Managed mode also redirects DuckDB spill files and secrets. |
| `TEMP`, `TMP`                         | Also redirected by managed Windows sandbox lifecycle; preserved in other modes and direct workers.                                                                   |
| `COLUMNS`                             | Initially set to `200`.                                                                                                                                              |
| `MPLBACKEND`                          | Defaults to `agg` only when absent.                                                                                                                                  |
| `MPLCONFIGDIR`                        | Redirected to private plotting storage in managed mode after configuration/cache selection; otherwise retains normal library behavior.                               |
| `MATPLOTLIBRC`                        | Selects a Matplotlib config file or a directory containing `matplotlibrc`; Console retains a discovered config before private-cache redirection.                     |
| `XDG_CONFIG_HOME`                     | Participates in Linux Matplotlib config lookup, not Console YAML discovery.                                                                                          |
| `LD_LIBRARY_PATH`                     | Adjusted for selected R/Python libraries on Linux.                                                                                                                   |
| `LD_PRELOAD`, `DYLD_INSERT_LIBRARIES` | Removed when launching the native sandbox runner; this is not a blanket guarantee for direct execution.                                                              |
| `PROCESSOR_ARCHITECTURE`              | On Windows x64, absent/empty values default to `AMD64` before runtime loading; supplied nonempty values remain.                                                      |

Private plotting/cache initialization still occurs with `cache: host` when `sandbox.tempdir: managed` is selected.
Other tempdir modes and `--no-sandbox` retain normal library cache selection after the configuration/cache assignments above.
Selecting host caches does not turn the built-in worker into an ordinary unmodified interpreter command.

## Networking and delegated variables

Networking tools and the native runner interpret `HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`, `NO_PROXY`, supported lowercase forms, certificate settings, and authentication variables.
Managed proxy configuration can replace workload proxy values.
Configure permissions through the sandbox schema, not these variables.

`CODEX_NETWORK_PROXY_ACTIVE` is a runner marker, not a user permission switch.
DuckDB extension preparation uses it with `HTTP_PROXY` to configure the native download path.
Faking the marker without a proxy can break preparation.

Other Python settings, such as `PYTHONHASHSEED`, `PYTHONWARNINGS`, `PYTHONDONTWRITEBYTECODE`, and `PYTHONNOUSERSITE`, follow the embedded runtime's behavior.
Command-line REPL features should not be assumed to apply unchanged.
Consult the relevant runtime/package documentation for delegated settings.

## Internal variables: leave unset

Internal handoff variables are not configuration; use the documented application settings and environment variables.
