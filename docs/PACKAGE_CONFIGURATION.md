# Package settings

Configure startup packages, when more packages may be prepared, package sources, cache locations, and preparation permissions.
[Packages and environments](REQUIREMENTS.md) explains how these choices affect an analysis.
For interpreter selection and native startup, see [Languages and startup](RUNTIME_CONFIGURATION.md).

Put these settings in a [configuration file](CONFIGURATION.md).
They are captured when the server starts; open a new connection after changing them.
The `requirements` argument can change the live declaration within the configured policies; its request format is documented in the [send reference](API.md#requirements).

## Choose startup packages {#startup-package-declarations}

| Setting           | Accepted values                             | Meaning and default                                                                                                                                                         |
| ----------------- | ------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r.packages`      | List of single-line `ir` package references | Startup R packages. Omission keeps the defaults; `[]` requests no optional packages.                                                                                        |
| `python.packages` | List of named PEP 508 registry requirements | Startup packages for managed Python. Also accepted under `python.managed` or a managed fallback candidate. Omission keeps the defaults; `[]` requests no optional packages. |

```yaml
r:
  packages: [dplyr, dbplyr, ggplot2]
python:
  packages: [numpy, pandas, matplotlib]
```

An omitted package list keeps that runtime's [default startup declaration](#default-startup-packages).
A supplied list replaces it; an empty list (`[]`) requests no optional packages.
Empty lists do not remove Console's required runtime infrastructure, uninstall packages, or delete cached installations.
Preparation makes packages available without attaching R packages or importing Python modules.

Packages listed explicitly in configuration are required, even if they also occur in the built-in defaults.

R package strings use single-line `ir` references, including supported remote references; local sources, NUL, and line breaks are rejected.
Python strings use named PEP 508 registry requirements, including versions, extras, and markers; paths, editable requirements, URLs, archives, and `name @ URL` references are rejected.
See [accepted requirement input](API.md#accepted-requirement-input) for package-request fields.

Later `requirements` calls with `action: reset` restore the effective startup declaration captured at server launch, rather than rereading a subsequently edited configuration file.
[Requirements API](API.md#requirements) covers additions, replacement, and changes that require a runtime restart.

### Default packages and extensions {#default-startup-packages}

Console prepares these packages and extensions at startup:

| Runtime | Packages or extensions                                                                           |
| ------- | ------------------------------------------------------------------------------------------------ |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                             |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                  |

Built-in convenience defaults are prepared on a best-effort basis: Console reports each failed default, skips it, and continues with the other defaults.
Required runtime infrastructure remains strict, including packages used by the R/Python bridge and SQL adapters.
Default DuckDB extensions follow the same best-effort rule; an extension requested explicitly remains required.
Extensions already built into the selected DuckDB do not need downloading.

## Choose when packages may be prepared {#dependency-resolution-policies}

<span id="resolution-policies"></span>

Set `r.resolution` or `python.resolution` to control Console's package preparation.
The Python setting also applies under `python.managed` and in managed fallback candidates.
The default preparation policies are shown below.
Managed Python adds no version constraint by default.

<!-- preparation-defaults -->

```yaml
r:
  resolution: implicit
python:
  managed:
    resolution: explicit
```

For how these choices affect an analysis, read [When packages are prepared](REQUIREMENTS.md#resolution-policy-admission).

| Policy              | Startup preparation                                                     | During the session                                                                                                                                                         |
| ------------------- | ----------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `fixed`             | Prepare the startup declaration.                                        | Keep that declaration fixed for the connection; reject package additions and version changes through Console.                                                              |
| `explicit`          | Prepare the startup declaration.                                        | Also accept package requests through `requirements` and managed Python declarations through `reticulate::py_require()`. Ordinary loads/imports do not trigger preparation. |
| `implicit`          | Prepare the startup declaration.                                        | Also accept explicit requests and prepare missing packages when execution reaches a supported load/import. The cell is not rerun.                                          |
| `disabled` (R only) | No managed R package preparation, including Console's R infrastructure. | No managed preparation. If `r.packages` is supplied, it must be empty.                                                                                                     |

For example, allow explicit R package requests while keeping the Python startup declaration fixed:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  packages: [numpy, pandas]
  resolution: fixed
```

To enable preparation on missing Python imports, set `python.resolution: implicit`.
For preinstalled-only Python, select an [existing environment](RUNTIME_CONFIGURATION.md#use-an-existing-environment).
DuckDB extension preparation follows the resolution policy of its managed provider.

Changing a resolution policy requires a new server connection.
These policies govern Console's preparation; they do not restrict arbitrary installation commands in submitted code or grant network access.
[Requirements API](API.md#requirements) specifies live-request behavior.
Preparation permissions and package sources are configured separately, as described below.

## Preparation permissions

The resolver selects environments and prepares packages separately from the agent's code.
Its filesystem and network permissions are independent of the session's permissions.

Default preparation runs in a native sandbox with host-file reads, private temporary and dependency-cache writes, and a download proxy.
Windows uses the pinned MXC companion, including for package/build subprocesses, runtime discovery and inspection, and DuckDB extension preparation.
It requires no administrator privileges, custom AppContainer profiles, or custom firewall rules.
The managed proxy resolves destination names through Windows system DNS.
During ordinary sessions, an unavailable or failed native sandbox is an error; Console never retries preparation with host permissions.
`--no-sandbox` explicitly selects host permissions.

[`warmup`](CLI.md#check-your-installation), including the background warmup started by `configure-client`, prepares dependencies with host permissions from the outset.
It uses the same resolver environment and cache locations as the configured session so downloads and builds can populate the caches before sandboxed use.
Resolver filesystem and network restrictions do not apply during this trusted installation step.
Runtime checks retain the configured worker sandbox.
Older pinned releases that do not support this warmup behavior retain their ordinary preparation policy.

Package builds and interpreter hooks execute code.
Use trusted requirements, package sources, tools, and configuration.
See [the trust boundary](#trust-boundary).

## Cache locations

| Value of `cache`    | Behavior                                                                                                                                                 |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `managed`           | Use Console-specific dependency caches and generated resolver write grants. Requires sandboxing.                                                         |
| `host`              | Use ordinary host cache locations and installed package environments where supported. Selecting host caches does not itself disable resolver sandboxing. |
| `{directory: PATH}` | Use managed dependency caches under `<directory>/dependencies` for preparation and workers. Requires sandboxing.                                         |
| Omitted or `null`   | Use `managed` for sandboxed execution, or `host` with `--no-sandbox`.                                                                                    |

`console` is an alias for `managed`; configuration exports use `managed`.

With [Codex inheritance](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex), the automatic choice waits for client permissions: sandboxed profiles use Console caches; full access uses host caches.
`config resolve --full` retains `cache: null` for this pending choice.
Existing Python uses its preinstalled packages in either mode.

Managed caches use these default dependency directories:

| Platform | Default dependency directory                    |
| -------- | ----------------------------------------------- |
| Linux    | `$HOME/.cache/mcp-console/dependencies`         |
| macOS    | `$HOME/Library/Caches/mcp-console/dependencies` |
| Windows  | `%LOCALAPPDATA%/mcp-console/cache/dependencies` |

An absolute `XDG_CACHE_HOME` selects `<XDG_CACHE_HOME>/mcp-console/dependencies` on every platform.
Windows has user-profile/home fallbacks when `LOCALAPPDATA` is unavailable.
To relocate Console-managed caches, set `cache.directory` in the configuration:

```yaml
cache:
  directory: /absolute/path/to/console-cache
```

Dependency caches go under `<directory>/dependencies` for both package preparation and workers.
`cache.directory` must be a nonempty path.
Relative directories use the launch directory; a leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
Put the path under `directory`; a bare path such as `cache: /path/to/cache` is not accepted.
This path takes precedence over environment variables and platform defaults.
Configuration and recordings keep their existing locations.
With `cache: managed`, the launch-time `MCP_CONSOLE_CACHE_DIR` environment variable selects the cache root ahead of `XDG_CACHE_HOME` and platform defaults; an unset or empty value retains the default selection.
Environment-based selection uses the resolver's effective environment.
The selected location is captured for the server connection and retained across worker restarts.
`MCP_CONSOLE_HOME` moves configuration and recordings, not these caches.

Console redirects supported package/cache variables beneath that directory.
With `sandbox.tempdir: managed`, worker plotting and general-purpose caches use private temporary storage, with prepared caches reused where supported.
Other tempdir modes and `--no-sandbox` retain normal library selection after cache configuration.
Existing Python preserves its configured user-site package location.
[Environment variables](ENVIRONMENT.md#cache-and-data-locations) lists the assignments.

To share ordinary host installations and caches:

```yaml
cache: host
```

This retains resolver sandboxing but changes the default cache locations and write grants.
`MCP_CONSOLE_CACHE_DIR` is ignored with `cache: host`.
Shared cache writes can affect artifacts later used outside Console.
`--no-sandbox` defaults to host caches and rejects managed caches, including custom directories, and explicit sandbox policies.
To keep other file settings while disabling enforcement, clear inherited permission nodes:

```sh
mcp-console serve --no-sandbox -c sandbox=null -c resolver.sandbox=null
```

The companion's source/build cache is separate from dependency caches.
It is not worker-writable under the default policy.

Resolver cache grants cover their complete subtrees, including Git metadata.
Console creates the cache roots needed for native enforcement; it does not add metadata directories or read-only exceptions inside them.

## Host cache selection

With `cache: host`, explicit tool variables select locations and corresponding default resolver write grants.
The principal variables are `UV_CACHE_DIR`, `UV_PYTHON_INSTALL_DIR`, `UV_TOOL_DIR`, `IR_CACHE_DIR`, `IR_LIBRARY_ROOT`, `RENV_PATHS_*`, `PKG_CACHE_DIR`, `R_PKG_CACHE_DIR`, and `MPLCONFIGDIR`.

Without overrides, uv uses its cache/data locations under XDG directories or the user's home.
R-related caches use `R_USER_CACHE_DIR`, then `XDG_CACHE_HOME`, then R's platform cache base.
Reticulate, ir, renv, and package caches occupy their respective subdirectories.
Default host-cache grants require an absolute resolver `HOME`, with `USERPROFILE` as the Windows fallback.
Windows uv defaults use `LOCALAPPDATA/uv/cache` and `APPDATA/uv/{python,tools}`; R uses `LOCALAPPDATA/R/cache` when neither R nor XDG cache overrides are set.

Use `MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` for a custom host extension cache.
Otherwise managed installation and SQL use `.duckdb/extensions` under the resolver's effective home.
On Windows, an absent or relative `HOME` falls back to an absolute `USERPROFILE` for that directory.
The location is captured; changing it in a cell does not reconfigure preparation.
Explicit filesystem policies must grant its writes.

Exact platform fallbacks and generated grants are implemented in [`src/resolver/cache.rs`](../src/resolver/cache.rs).

## Configure package sources {#configuration}

| Setting                        | Accepted values             | Default and behavior                                                                                                                                                     |
| ------------------------------ | --------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `resolver`                     | Mapping                     | Optional preparation settings; unknown fields are errors.                                                                                                                |
| `resolver.sandbox`             | Sandbox mapping or `null`   | Independent preparation permissions. Omitted or `null`: host reads, cache writes, private temporary storage, and the [default download proxy](#expanded-public-default). |
| `resolver.environment`         | Mapping of names to strings | Default: `{}`. Override individual shared environment values for preparation.                                                                                            |
| `resolver.inherit_environment` | `true` or `false`           | Omission follows the top-level `inherit_environment` setting.                                                                                                            |

The complete permission fields are in [Sandbox permissions](SANDBOX_CONFIGURATION.md).
[Environment variables](ENVIRONMENT.md#where-to-set-variables) covers inheritance, value validation, and variables recognized by Console and its package tools.

Python indexes and mirrors use uv's native settings, including user- or system-level `uv.toml` files and environment variables such as `UV_INDEX_URL`.
For R, pak's `PKG_CRAN_MIRROR` selects a CRAN-compatible repository, including a private Posit Package Manager instance.
Pass package-manager environment variables through `resolver.environment`.
R startup files are not read during preparation.

For example, to use private Python and R package mirrors:

```yaml
resolver:
  environment:
    UV_INDEX_URL: https://packages.example.org/simple
    PKG_CRAN_MIRROR: https://packages.example.org/cran/latest
  sandbox:
    network:
      proxy:
        domains:
          allow: [packages.example.org, artifacts.example.org]
```

Include every host serving artifacts and redirects, plus other package sources needed by the session.
An explicit `domains` mapping replaces the **generated** default allowlist; `{}` allows no destinations.
Ordinary file/CLI mapping merges still apply before defaults are generated.

`resolver.sandbox` uses the [public sandbox schema](SANDBOX_CONFIGURATION.md).
Omitted networking retains the download proxy.
Explicit `network: restricted` or `network: enabled` removes it.
Limited proxy mode does not support HTTPS CONNECT and therefore cannot perform ordinary HTTPS downloads.

Top-level `environment` values also reach preparation.
`resolver.environment` overrides individual shared values.
`resolver.inherit_environment` defaults to the top-level setting.
Console's runtime, cache, proxy, and transport assignments take precedence.
Worker environment mutations do not reconfigure later preparation.

Windows accepts resolver filesystem, temporary-storage, and network settings using the same schema as worker permissions.
An MXC managed proxy requires bidirectional host loopback access: `allow_local_binding: auto` selects true, while explicit false is rejected.
Use proxy-free `network: restricted` to prohibit loopback; Windows also rejects SOCKS5 UDP and Unix-socket grants.
`--no-sandbox` rejects explicit sandbox policies.

## Filesystem grants

Omitting `resolver.sandbox.filesystem` retains generated host reads and cache writes.
**An explicit filesystem mapping replaces those default entries.** Supply necessary reads and writes yourself:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
  sandbox:
    filesystem:
      read_only: [/]
      read_write: [/home/alice/package-caches/uv]
```

This grants only the selected uv cache; Python installations, R libraries, and DuckDB can need additional writable paths.
Create custom writable directories before Linux launch.
Paths are relative to the launch directory and support a leading `~` under the shared [path rules](CONFIGURATION.md#paths).
Environment-variable references and globs are not expanded.
The [same restrictions on broad write grants](SANDBOX_CONFIGURATION.md#filesystem) apply to resolver paths.
They also apply to relative cache locations such as `UV_CACHE_DIR=.`, before Console resolves or creates cache directories.

To select that cache while retaining generated host-cache grants, omit the filesystem mapping:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /home/alice/package-caches/uv
```

Console does not read uv configuration files to infer extra writable paths.
Expose a custom cache path through the corresponding environment variable or grant it explicitly.
In Console cache mode, Console's cache assignments take precedence.

## Trust boundary

Preparation can read host files and modify its granted caches.
Console cache mode separates artifacts from ordinary host caches; it does not make them immutable, inspect package code, or protect readable secrets.

Keep selected resolver executables, wrappers, installation directories, source files, and configuration outside worker-writable paths.
Captured names and interpreter identity checks do not prevent every concurrent file replacement or changes to dependencies loaded later.

`UV_OFFLINE=1` in a worker is a uv setting, not process-level network enforcement or a description of resolver permissions.
R discovery/preparation is profile-free.
See [Requirements API](API.md#accepted-requirement-input) for supported request syntax and [live preparation](API.md#live-preparation) for partial effects.

## Selecting resolver tools

Without R, managed Python requires `uv` on the launch PATH; there is no managed fallback to a system Python.
An [existing Python environment](RUNTIME_CONFIGURATION.md#python-environment-selection) bypasses managed Python preparation.

With R, preparation prefers PATH `ir` (at least 0.4.0), then `uv tool run --from r-lib-ir ir`, with reticulate bootstrap when available.
A genuinely unavailable bootstrap can leave a bare R runtime; a selected bootstrap that fails is an error.
`RETICULATE_UV` affects the R-backed Python preparation path, not R-free preparation.

MXC denies creation of uv's optional Python minor-version junction.
When uv reports that specific denial after downloading its concrete interpreter, Console resumes the same sandboxed Python materialization once after confirmed cleanup.
Package/build failures are not retried; failure of the resumed invocation remains an error.
Sandboxed Windows R preparation sets `RENV_CONFIG_CACHE_SYMLINKS=FALSE` and `RENV_CONFIG_COPY_METHOD=r` so renv uses R's directory copier within the granted caches instead of junctions or Robocopy's restartable-copy operations that MXC denies.

## Expanded public default

The default download policy is equivalent to the following networking configuration.
Omit the filesystem mapping to retain generated cache and temporary-storage permissions.

```yaml
resolver:
  sandbox:
    network:
      proxy:
        mode: full
        socks5: tcp
        allow_upstream_proxy: false
        domains:
          allow:
            - pypi.org
            - files.pythonhosted.org
            - astral.sh
            - releases.astral.sh
            - github.com
            - api.github.com
            - codeload.github.com
            - raw.githubusercontent.com
            - objects.githubusercontent.com
            - release-assets.githubusercontent.com
            - r-lib.github.io
            - packagemanager.posit.co
            - rspm-sync.rstudio.com
            - bioconductor.posit.co
            - bioconductor.org
            - cran.r-project.org
            - cloud.r-project.org
            - cran.rstudio.com
            - extensions.duckdb.org
      sockets:
        unix_sockets: []
      allow_local_binding: true
```

Local binding permits installer coordination.
On macOS it also permits host loopback access and DNS; Linux uses a private network namespace.
On Windows it permits bidirectional host loopback access required by the MXC proxy, including other local services.
The proxy allowlist does not restrict that direct loopback access; non-loopback downloads still require an allowed proxy destination.
