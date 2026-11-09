# Package settings

Configure startup packages, preparation policies, package sources, caches, and preparation permissions in a [configuration file](CONFIGURATION.md).
These settings are captured when the server starts; changing them requires a new connection.
Use [`requirements`](API.md#requirements) to change a session's package declaration within those policies.
For an introduction, see [Packages and environments](REQUIREMENTS.md); for interpreter selection, see [Languages and startup](RUNTIME_CONFIGURATION.md).

## Choose startup packages {#startup-package-declarations}

```yaml
r:
  packages: [dplyr, dbplyr, ggplot2]
python:
  packages: [numpy, pandas, matplotlib]
```

An omitted list keeps the [default startup declaration](#default-startup-packages).
A supplied list replaces it; `[]` requests no optional packages.
Preparation makes packages available without attaching R packages or importing Python modules.
An explicitly listed package is required, even when it also occurs in the defaults.

Empty lists do not remove runtime infrastructure, uninstall packages, or delete caches.
Later `requirements` calls with `action: reset` restore the captured startup declaration without rereading configuration.
See [accepted requirement input](API.md#accepted-requirement-input) for R references, Python registry requirements, and their restrictions.

### Default packages and extensions {#default-startup-packages}

| Runtime | Packages or extensions                                                                           |
| ------- | ------------------------------------------------------------------------------------------------ |
| R       | `tidyverse`, `dplyr`, `dbplyr`, `reticulate`, `DBI`, `duckdb`, `nanoarrow`, `yyjsonr`, `ggplot2` |
| Python  | `numpy`, `pandas`, `matplotlib`, `plotnine`; also `duckdb` without R                             |
| DuckDB  | `icu`, `json`, `sqlite`, when the managed provider is available                                  |

Convenience defaults are best-effort: Console reports and skips each failed default, then continues.
Runtime infrastructure, including the R/Python bridge and SQL adapters, remains required.
Explicitly requested DuckDB extensions are also required; extensions built into the selected DuckDB need no download.

## Choose when packages may be prepared {#dependency-resolution-policies}

<span id="resolution-policies"></span>

Set `r.resolution` and `python.resolution` independently.
The Python setting also applies under `python.managed` and managed fallback candidates.
The defaults are:

<!-- preparation-defaults -->

```yaml
r:
  resolution: implicit
python:
  managed:
    resolution: explicit
```

The [policy table](CONFIGURATION.md#dependency-resolution-policies) defines `implicit`, `explicit`, `fixed`, and R-only `disabled`.
For example, allow explicit R package requests while keeping Python's declaration fixed:

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  packages: [numpy, pandas]
  resolution: fixed
```

For preinstalled-only Python, select an [existing environment](RUNTIME_CONFIGURATION.md#use-an-existing-environment).
DuckDB extension preparation follows its managed provider's policy.
These policies control Console's preparation; they do not restrict arbitrary installer code or grant network access.
The [`send` reference](API.md#requirements) owns live-request admission, automatic resolution, and restart rules.

## Preparation permissions

The resolver selects environments and prepares packages separately from submitted code, with its own `resolver.sandbox` permissions.
By default it permits host-file reads, private temporary and dependency-cache writes, and downloads through the [package-source proxy](#expanded-public-default).
An unavailable or failed native sandbox is an error during ordinary sessions; Console does not retry with host permissions.
`--no-sandbox` explicitly selects host permissions.
See [Sandbox permissions](SANDBOX_CONFIGURATION.md) for platform requirements and network limits.

[`warmup`](CLI.md#check-your-installation), including background warmup from `configure-client`, prepares dependencies with host permissions from the outset.
It uses the session's resolver environment and caches, so it can populate them before sandboxed use.
Runtime checks still use the configured worker sandbox.

## Cache locations

`cache` selects where prepared dependencies live:

| Choice                               | Use                                                                                      |
| ------------------------------------ | ---------------------------------------------------------------------------------------- |
| `managed` (default with sandboxing)  | Console-owned dependency caches, separate from ordinary host caches.                     |
| `{directory: PATH}`                  | Console-owned caches under `PATH/dependencies`. Relative paths use the launch directory. |
| `host` (default with `--no-sandbox`) | Normal tool caches selected by the resolver environment and platform defaults.           |

The older value `console` is an alias for `managed` and exports as `managed`.
A custom directory must be nonempty; a bare path string is not a cache selector.
Leading `~` follows the [configuration path rules](CONFIGURATION.md#paths).

For managed caches, an explicit directory wins over `MCP_CONSOLE_CACHE_DIR`; otherwise Console uses that variable or the platform cache directory.
The [environment reference](ENVIRONMENT.md#cache-and-data-locations) owns the cache paths, redirected variables, and precedence.
`MCP_CONSOLE_HOME` moves configuration and fallback recordings, not dependencies.
Existing Python environments continue to use their installed packages.

With `inherit_codex`, automatic cache selection waits for the client's first usable policy: sandboxed sessions use managed caches and full-access sessions use host caches.
`config resolve --full` reports `cache: null` while this choice is pending.

Managed temporary storage can redirect worker plotting and general caches into private storage even with `cache: host`.
The resolver's persistent caches remain separate from that worker storage.
The pinned native companion has its own build cache, which is not worker-writable.

## Host cache selection

To reuse tool caches:

```yaml
cache: host
```

Console uses the effective resolver environment to select uv, ir, renv, R, plotting, and DuckDB cache paths, then grants preparation access to the selected subtrees.
An explicit `resolver.sandbox.filesystem` replaces those generated grants.
Host caches may also be used by other tools, so preparation can change shared host state.
The selection rules live in [resolver cache selection](../src/resolver/cache.rs); recognized variables and overrides are listed in [Environment variables](ENVIRONMENT.md#cache-and-data-locations).

`MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY` selects a host-mode extension directory; a relative nonempty value uses the launch directory.
Otherwise extensions use the resolver home's `.duckdb/extensions`.
The choice is captured before execution, and changing a cell's environment does not move it or grant additional access.

`--no-sandbox` requires host caches and rejects explicit worker/resolver sandbox mappings.
Clear inherited selections when opting out:

```sh
mcp-console serve --no-sandbox -c sandbox=null -c resolver.sandbox=null -c cache=host
```

## Configure package sources {#configuration}

Use uv's native configuration and environment for Python sources, and ir/pak's environment for R sources.
R discovery and preparation do not load `.Rprofile` or `.Renviron`; set resolver variables directly:

```yaml
resolver:
  environment:
    UV_INDEX_URL: https://packages.example.com/simple
    PKG_CRAN_MIRROR: https://packages.example.com/cran
  sandbox:
    network:
      proxy:
        mode: full
        domains:
          allow: [packages.example.com, artifacts.example.com]
```

Allow both index hosts and any redirected artifact hosts.
An explicitly configured `domains` mapping replaces the generated public destinations; omitted lists are empty, and `{}` allows none.
Ordinary configuration layers still merge recursively, so use [ordered clearing](CONFIGURATION.md#merge-rules) to remove entries supplied by an earlier file.

Shared `environment` values reach workers and preparation; `resolver.environment` overrides them for preparation only.
Resolver inheritance defaults to the shared `inherit_environment` choice.
Console's required internal assignments take precedence, and later cell environment changes do not reconfigure preparation.

Worker and resolver networking use the same [network settings](SANDBOX_CONFIGURATION.md#network-choices).
A restricted/enabled mode replaces the generated proxy, while a destination list changes its allowlist.
`network: null` preserves the earlier layer.
Limited proxies reject HTTPS CONNECT and are usually unsuitable for package downloads.
Windows managed proxies require local binding; Unix-socket and UDP limitations are described in [platform support](SANDBOX_CONFIGURATION.md#native-capabilities).

## Filesystem grants

Omit `resolver.sandbox.filesystem` to keep generated host reads and cache writes.
An explicit mapping replaces that whole generated policy, so grant every location your chosen tools need:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /var/cache/console-uv
  sandbox:
    filesystem:
      read_only: [/usr, /lib, /lib64, /etc, /opt]
      read_write: [/var/cache/console-uv]
```

This example selects only uv's cache; managed Python installations, R libraries, DuckDB extensions, and other tools may need further grants.
Create writable directories before launch where required by the native backend.
Paths follow the [shared filesystem rules](SANDBOX_CONFIGURATION.md#filesystem), including protection against relative writes that resolve to a filesystem root or home directory.

To retain automatic grants while changing only a cache location, omit the filesystem mapping:

```yaml
cache: host
resolver:
  environment:
    UV_CACHE_DIR: /var/cache/console-uv
```

Console does not inspect arbitrary uv configuration files for additional writable paths.
Select cache locations through the documented variables or grant them explicitly.
Managed/custom cache mode overrides native tool cache paths.
Generated grants cover complete selected cache subtrees, including any Git metadata inside them.

## Trust boundary

Package builds and interpreter hooks execute code.
Use trusted requirements, sources, tools, and configuration.
The default resolver can read host files, including secrets, and package caches are mutable.
Separate preparation permissions do not make those inputs immutable or inspect their code.

Keep resolver executables, wrappers, installation directories, sources, and configuration outside worker-writable paths.
Captured names and interpreter identity checks do not prevent every concurrent replacement or changes to dependencies loaded later.
`UV_OFFLINE=1` in a worker is a uv setting, not process-level network enforcement or a description of resolver permissions.
See [live preparation](API.md#live-preparation) for partial effects after failures.

## Selecting resolver tools

Without R, managed Python requires `uv` on the launch PATH; there is no managed fallback to system Python.
An existing Python environment bypasses managed preparation.

With R, preparation prefers PATH `ir` (at least 0.4.0), then `uv tool run --from r-lib-ir ir`, with reticulate bootstrap when available.
An unavailable bootstrap can leave a bare R runtime; a selected bootstrap that fails is an error.
`RETICULATE_UV` affects R-backed Python preparation only.
Windows compatibility adaptations are documented beside [managed Python preparation](../src/resolver/managed_python.rs) and [resolver sandbox setup](../src/resolver/sandbox.rs).

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
