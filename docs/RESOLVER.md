# Package preparation and caches

The resolver selects environments and prepares packages separately from evaluated code.
Giving a worker network or filesystem access does not give the resolver the same access, and vice versa.

Default preparation runs in a native sandbox with host-file reads, private temporary and dependency-cache writes, and a download proxy.
Windows uses the pinned MXC companion, including for package/build subprocesses, runtime discovery and inspection, and DuckDB extension preparation.
It requires no administrator privileges, custom AppContainer profiles, or custom firewall rules.
The managed proxy resolves destination names through Windows system DNS.
An unavailable or failed native sandbox is an error; Console never retries preparation with host permissions.
`--no-sandbox` explicitly selects host permissions.

Package builds and interpreter hooks execute code.
Use trusted requirements, package sources, tools, and configuration.
See [the trust boundary](#trust-boundary).

## Cache locations

Sandboxed sessions default to `cache: console`:

| Platform | Default dependency directory                    |
| -------- | ----------------------------------------------- |
| Linux    | `$HOME/.cache/mcp-console/dependencies`         |
| macOS    | `$HOME/Library/Caches/mcp-console/dependencies` |
| Windows  | `%LOCALAPPDATA%/mcp-console/cache/dependencies` |

An absolute `XDG_CACHE_HOME` selects `<XDG_CACHE_HOME>/mcp-console/dependencies` on every platform.
Windows has user-profile/home fallbacks when `LOCALAPPDATA` is unavailable.
Set `MCP_CONSOLE_CACHE_DIR` to an absolute directory to use `<MCP_CONSOLE_CACHE_DIR>/dependencies` instead.
It takes precedence over `XDG_CACHE_HOME` and platform defaults; an unset or empty value retains the default selection.
Selection uses the resolver's effective environment and is captured for the server connection.
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
`--no-sandbox` defaults to host caches and rejects `cache: console`.

The companion's source/build cache is separate from dependency caches.
It is not worker-writable under the default policy.

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

## Configuration

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
Paths are launch-relative and do not expand `~` or environment variables.
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
See [Requirements](REQUIREMENTS.md#host-resolution-and-trust) for supported request syntax and partial effects.

## Selecting resolver tools

Without R, managed Python requires `uv` on the launch PATH; there is no managed fallback to a system Python.
An [existing Python environment](CONFIGURATION.md#python-environment-selection) bypasses managed Python preparation.

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
