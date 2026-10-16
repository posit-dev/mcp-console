# Configuration reference

Look up the fields accepted in Console's YAML or JSON configuration, their possible values, and what happens when you omit them.
For setup examples and file precedence, see [Configuration](CONFIGURATION.md).

Defaults below describe a sandboxed built-in session without configuration overrides.
Some defaults depend on the host or selected runtime; those cases are called out below.
Table paths use dots to show nesting, so `r.packages` means the `packages` field inside `r`.
To inspect the defaults for your installation:

```sh
mcp-console config resolve --no-config --full
```

This prints configuration without starting interpreters or preparing packages.
Use `mcp-console config resolve --full` to include your global, project, and command-line settings.

## Top-level settings

| Node                  | Accepted values                                         | Meaning and default                                                                                                                                                                                                      |
| --------------------- | ------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| `r`                   | Executable path, mapping, or `null`                     | Select R, its startup packages, and native startup behavior. Omission uses [normal R discovery](#r).                                                                                                                     |
| `python`              | Executable/venv path, selection mapping, or `null`      | Select an existing or managed Python environment. Omission keeps [normal Python selection](#python).                                                                                                                     |
| `languages`           | Nonempty list of `r`, `python`, and/or `sql`; or `null` | Choose source fields exposed to the agent. Default: `[r, python, sql]`. This does not install runtimes or change permissions.                                                                                            |
| `startup`             | Mapping or `null`                                       | Run source that selects a SQL connection once per worker. Default: no startup source. See [Session startup](#session-startup).                                                                                           |
| `cache`               | `console`, `host`, or `null`                            | Choose dependency cache locations. Default: `console` when sandboxed, `host` with `--no-sandbox`. See [Caches](#caches).                                                                                                 |
| `inherit_environment` | `true` or `false`                                       | Inherit launch environment variables. Default: `true`. See [Environment](#environment).                                                                                                                                  |
| `environment`         | Mapping of names to string values                       | Supply environment variables to execution and preparation. Default: `{}`.                                                                                                                                                |
| `sandbox`             | Mapping or `null`                                       | Set evaluated-code permissions. Default: host reads, private temporary writes, and restricted networking. See [Sandbox](#sandbox).                                                                                       |
| `resolver`            | Mapping                                                 | Set package-preparation environment and permissions independently. Default: host reads, cache writes, and a download proxy on macOS/Linux; host permissions on Windows. See [Package preparation](#package-preparation). |
| `tool`                | Mapping                                                 | Customize tool descriptions, initialization instructions, and parameter visibility. Default: generated presentation. See [Tool presentation](#tool-presentation).                                                        |

Configuration is trusted launcher input and can widen permissions or select executables.
Unknown fields are errors.
For launch flags, including custom workers and `--no-sandbox`, see [Command-line reference](CLI.md).

## R

`r: /opt/R/bin/R` is shorthand for `r: {executable: /opt/R/bin/R}`.
The mapping accepts:

| Node           | Accepted values                             | Meaning and default                                                                                                                                                                             |
| -------------- | ------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r.executable` | Nonempty path string                        | Installed R executable or launcher. Omission uses installation hints and PATH discovery. Select the launcher, not an R installation directory or Rscript.                                       |
| `r.packages`   | List of single-line `ir` package references | Startup packages to prepare. Omission keeps the [default R packages](REQUIREMENTS.md#defaults-and-startup-configuration); `[]` requests no optional packages. Preparation does not attach them. |
| `r.resolution` | `implicit`, `explicit`, `fixed`, `disabled` | When Console may prepare packages. Default: `implicit`. See [Resolution policies](#resolution-policies).                                                                                        |
| `r.vanilla`    | `true` or `false`                           | Request R's native `--vanilla` startup when true. Default: `false`, retaining normal environment/profile/workspace startup.                                                                     |

```yaml
r:
  packages: [dplyr, ggplot2]
  resolution: explicit
  vanilla: true
```

An explicit executable overrides installation hints and PATH discovery.
Relative executable paths use the launch directory; a leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
The selected installation is retained across worker restarts.
R settings require the built-in worker.
See [R executable selection](CONFIGURATION.md#r-executable-selection) and [Native R startup](CONFIGURATION.md#native-r-startup).

## Python

Choose one selection form:

| Node                     | Accepted values             | Meaning                                                                                                     |
| ------------------------ | --------------------------- | ----------------------------------------------------------------------------------------------------------- |
| `python`                 | Omitted or `null`           | Keep launch-time `RETICULATE_PYTHON` compatibility behavior; otherwise use managed selection.               |
| `python`                 | A path string               | Use an existing Python executable or standard virtual environment. Equivalent to setting `python.existing`. |
| `python.existing`        | A path string               | Use installed packages in that environment; Console does not manage its Python packages.                    |
| `python.managed`         | Mapping                     | Prepare a uv-managed CPython environment with the settings below.                                           |
| `python.first_available` | Nonempty list of candidates | Select the first available candidate from an ordered list.                                                  |

A selection mapping contains exactly one of `python.existing`, `python.managed`, or `python.first_available`.
Existing Python requires CPython 3.10 or newer with a usable shared embedding library.
Venv directories need `pyvenv.cfg`; recognized Conda environments are unsupported.
Interpreter paths use the launch directory and support a leading `~`, as R executable paths do.
Python selection requires the built-in worker.

### Managed Python

Fields under `python.managed`:

| Node                        | Accepted values                                                                      | Meaning and default                                                                                                                                                                                  |
| --------------------------- | ------------------------------------------------------------------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python.managed.version`    | One quoted Python version or constraint string, such as `"3.13"` or `">=3.12,<3.14"` | Restrict the managed CPython version. Omission adds no version constraint; CPython older than 3.10 is always excluded.                                                                               |
| `python.managed.packages`   | List of named PEP 508 registry requirements                                          | Startup packages to prepare. Omission keeps the [default Python packages](REQUIREMENTS.md#defaults-and-startup-configuration); `[]` requests no optional packages. Preparation does not import them. |
| `python.managed.resolution` | `implicit`, `explicit`, `fixed`                                                      | When Console may prepare Python packages. Default: `explicit`. Set `implicit` to opt in to preparation on missing imports. For preinstalled-only use, select an existing environment.                |

```yaml
python:
  managed:
    version: ">=3.12,<3.14"
    packages: [numpy, pandas]
    resolution: fixed
```

### First available Python

Each entry in `python.first_available` is one of the following.
Here, `[]` in a path denotes a list item.

| Node                                | Accepted values | Selection behavior                                                                                                                         |
| ----------------------------------- | --------------- | ------------------------------------------------------------------------------------------------------------------------------------------ |
| `python.first_available[].existing` | A path string   | Use that executable or venv when its path exists. An absent path advances to the next candidate; a present but unusable environment fails. |
| `python.first_available[]`          | `active_venv`   | Use the incoming `VIRTUAL_ENV`. An unset or empty variable advances; a supplied but unusable environment fails.                            |
| `python.first_available[].managed`  | Mapping         | Use managed Python with the [managed settings above](#managed-python). Must be the last candidate.                                         |

Managed candidate fields have paths such as `python.first_available[].managed.packages`.
The list must be nonempty and flat, with at most one `active_venv` and at most one managed candidate.
Exhausting the list is an error.

```yaml
python:
  first_available:
    - existing: .venv
    - active_venv
    - managed:
        packages: [numpy, pandas]
```

See [Python environment selection](CONFIGURATION.md#python-environment-selection) for interpreter requirements and launch-environment behavior.

## Resolution policies

These values apply to `r.resolution` and `python.managed.resolution`, including a managed fallback candidate:

R defaults to `implicit`; managed Python defaults to `explicit`.

| Value               | What Console prepares                                                                                        |
| ------------------- | ------------------------------------------------------------------------------------------------------------ |
| `implicit`          | Startup packages, explicit requirement changes, and reached missing-package loads/imports.                   |
| `explicit`          | Startup packages and explicit requirement changes. Missing-package loads/imports do not request preparation. |
| `fixed`             | Prepare startup packages and keep the declaration fixed for the connection.                                  |
| `disabled` (R only) | No R package preparation, including Console's R infrastructure. Supplied `r.packages` must be empty.         |

An omitted package list keeps the runtime's default declaration; an explicit list replaces it.
An empty list does not uninstall packages or remove infrastructure/caches.
Package strings use the [accepted requirement syntax](REQUIREMENTS_API.md#accepted-requirement-input).
These policies control Console's preparation; code still has the installation capabilities allowed by its permissions.
See [Packages and environments](REQUIREMENTS.md#resolution-policy-admission) for live requirement operations.

## Session startup

The optional `startup` mapping accepts both required fields:

| Node               | Accepted values                    | Meaning                                                                                                                                 |
| ------------------ | ---------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `startup.language` | `r` or `python`                    | Interpreter that runs the startup source.                                                                                               |
| `startup.code`     | Nonempty source string without NUL | Program that creates and selects a SQL connection with `.console$sql_connection(...)` in R or `_console.sql_connection(...)` in Python. |

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect(":memory:")
    _console.sql_connection(connection)
```

Default: `null`, using managed DuckDB without user startup source.
Configured startup requires the built-in worker and relay.
Configured source runs once per worker, after runtime helpers are available and before the first SQL cell.
Its encoded transport, including escaping, is limited to 32 KiB.
Failure withholds SQL until explicit restart; restart can repeat earlier side effects.
See [Session startup source](CONFIGURATION.md#session-startup-source).

## Environment

| Setting               | Accepted values                      | Meaning and default                                                                                      |
| --------------------- | ------------------------------------ | -------------------------------------------------------------------------------------------------------- |
| `inherit_environment` | `true`                               | Start with inherited launch variables, then apply `environment`. This is the default.                    |
| `inherit_environment` | `false`                              | Omit inherited workload variables. Explicit variables and Console-owned runtime assignments still apply. |
| `environment`         | Mapping of variable names to strings | Shared values for the worker and package preparation. Default: `{}`.                                     |

```yaml
inherit_environment: true
environment:
  OMP_NUM_THREADS: "4"
  APP_MODE: analysis
```

Quote numbers and booleans when supplying environment values.
An empty string is a value; `null` cannot unset a variable.
On Windows, configured names must remain unique under case-insensitive comparison after merging; use the same spelling when overriding a name.
Names remain case-sensitive on Unix.

Console's runtime, cache, temporary-storage, proxy, and transport assignments take precedence over these settings.
Shared credentials also reach package preparation.
See [Environment variables](ENVIRONMENT.md) for recognized host settings and [Package preparation](#package-preparation) for resolver-specific overrides.

## Caches

| Value of `cache` | Meaning                                                                                                                           |
| ---------------- | --------------------------------------------------------------------------------------------------------------------------------- |
| `console`        | Use Console-specific dependency caches and generated resolver write grants. Default for sandboxed execution; requires sandboxing. |
| `host`           | Use ordinary host cache locations and installed package environments where supported. Default with `--no-sandbox`.                |

Omitted or `null` selects the appropriate default for the launch.
For the `inherit_codex` profile, automatic cache selection waits for client metadata: sandboxed profiles use Console caches, and inherited full access uses host caches.
`config resolve --full` retains `cache: null` for that automatic choice.
`cache: host` retains resolver sandboxing on macOS/Linux.
Existing Python still uses its preinstalled packages.
See [Cache locations](RESOLVER.md#cache-locations) for directories, environment overrides, and write grants.

## Sandbox

The following schema applies under both `sandbox` (evaluated code) and `resolver.sandbox` (package preparation).
These are independent policies.
Default worker permissions allow host-file reads, including readable secrets, and private temporary writes; ordinary workspace writes and direct networking are restricted.

| Node                                                  | Accepted values                                                                  | Meaning and default                                                                                        |
| ----------------------------------------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------- |
| `sandbox.profile`                                     | `read_only`, `workspace`, `inherit_codex`, an `inherit_codex` mapping, or `null` | Select the worker's base profile. Omitted or `null`: host-read baseline without workspace writes.          |
| `resolver.sandbox.profile`                            | `read_only`, `workspace`, or `null`                                              | Select preparation filesystem permissions. Omitted, `null`, or `read_only`: no workspace profile.          |
| `sandbox.tempdir`<br>`resolver.sandbox.tempdir`       | `managed`, `read_write`, `read_only`                                             | Select automatic temporary-storage grants. Default: `managed`.                                             |
| `sandbox.filesystem`<br>`resolver.sandbox.filesystem` | Mapping                                                                          | Set concrete read, write, and deny paths. Omission retains generated defaults for that policy.             |
| `sandbox.network`<br>`resolver.sandbox.network`       | `restricted`, `enabled`, or proxy mapping                                        | Select network access. Worker default: `restricted`; macOS/Linux resolver default: managed download proxy. |

`sandbox.profile: inherit_codex` inherits usable client sandbox metadata from the first `send`, before runtime preparation.
Its default fallback is `read_only`: host reads, private temporary writes, and restricted networking before explicit local settings are applied.
For a configuration shared with Claude or another client without this metadata, use `sandbox.profile: {inherit_codex: {fallback: workspace}}` to configure workspace permissions as the fallback.
The `inherit_codex` mapping accepts an optional `fallback: read_only` or `fallback: workspace`.
Missing metadata selects the fallback silently; malformed or unsupported metadata emits a warning on server stderr and selects the same configured fallback.
Fallback applies only when metadata is unavailable, not when the selected sandbox or runtime preparation fails.
Explicit filesystem entries compose with the selected base, and explicit network and temporary-storage settings apply afterward.
Inherited full access disables both sandboxes, including local restrictions, and selects host caches by default.
The `inherit_codex` profile is not accepted under `resolver.sandbox.profile`, with or without a fallback.
See [Inherit permissions from Codex](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex) for mapping and connection-lifetime rules.

Setting the whole `sandbox` or `resolver.sandbox` node to `null` clears inherited settings and restores that policy's built-in defaults.
Enforcement remains enabled.
Explicit policies conflict with `--no-sandbox`; Windows rejects explicit `resolver.sandbox` policies.

### Workspace profile

Setting `sandbox.profile` or `resolver.sandbox.profile` to `workspace` grants ordinary writes in Console's captured launch directory.
The workspace's `.git`, `.agents`, `.codex`, and `.aws` remain readable but protected from writes and creation; Git pointer targets are also protected.
Explicit filesystem rules can override these defaults.
The profile does not add network access or change `sandbox.tempdir` or `resolver.sandbox.tempdir`.
See [Workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile) for examples, the equivalent manual filesystem configuration, and protecting `.claude`.

### Temporary storage

| Value of `sandbox.tempdir` or `resolver.sandbox.tempdir` | Automatic grants and lifetime                                                                                                                                                                   |
| -------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `managed` (default)                                      | Create private writable storage, redirect temporary environment variables, and remove it after sandbox retirement.                                                                              |
| `read_write`                                             | Retain configured/inherited host temp locations and grant writes there and to the platform default. Console does not own or clean those directories.                                            |
| `read_only`                                              | Retain temporary environment variables and add no temporary write grants or managed storage. Existing filesystem grants still apply. R can fail to initialize without a writable temp location. |

On Unix, the platform default write grant is `/tmp`; `TMPDIR`, `TEMP`, and `TMP` add their locations.
On Windows, `read_write` exports the granted host default when all three child variables are unset or empty.
Relative temp locations receive launch-relative grants; use absolute paths if code changes directories.
See [Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage) for cleanup and runtime limits.

### Filesystem

Fields under `sandbox.filesystem` or `resolver.sandbox.filesystem`:

| Node                                                                        | Accepted values      | Access to each path and its descendants                             |
| --------------------------------------------------------------------------- | -------------------- | ------------------------------------------------------------------- |
| `sandbox.filesystem.read_only`<br>`resolver.sandbox.filesystem.read_only`   | List of path strings | Permit reads, with no writes at that path.                          |
| `sandbox.filesystem.read_write`<br>`resolver.sandbox.filesystem.read_write` | List of path strings | Permit reads and writes, including creation, editing, and deletion. |
| `sandbox.filesystem.deny`<br>`resolver.sandbox.filesystem.deny`             | List of path strings | Deny reads and writes.                                              |

Each list defaults to `[]` within a supplied mapping.
Paths use the launch directory and support a leading `~`; see [Paths](CONFIGURATION.md#paths).
Globs and environment-variable references are not expanded.
They do not create directories; create intended writable directories before Linux launch.
Narrower rules override ancestors; at the same path deny wins over write, then read.

Worker entries compose with the host-read/private-write baseline and selected profile.
An explicit **resolver** filesystem mapping replaces generated filesystem entries, including host reads and cache writes.
See [Filesystem grants](RESOLVER.md#filesystem-grants) before supplying one.

### Network access

| Value of `sandbox.network` or `resolver.sandbox.network` | Access                                                                                                                                            |
| -------------------------------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------- |
| `restricted`                                             | Restricted native networking without a managed proxy. This is the worker default.                                                                 |
| `enabled`                                                | Host networking while filesystem enforcement remains active.                                                                                      |
| Mapping with `proxy`                                     | Restricted native networking plus a managed proxy and the optional socket/local-binding settings below. This is the macOS/Linux resolver default. |

Selecting either scalar value removes the generated resolver proxy.
A network mapping requires `sandbox.network.proxy` or `resolver.sandbox.network.proxy`; `full` is not a supported network value.

Fields in a network mapping:

| Node                                                                                    | Accepted values                 | Meaning and default                                                                                                                   |
| --------------------------------------------------------------------------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `sandbox.network.proxy`<br>`resolver.sandbox.network.proxy`                             | Required mapping                | Managed HTTP/HTTPS and optional SOCKS proxy. See [Proxy settings](#proxy-settings).                                                   |
| `sandbox.network.sockets`<br>`resolver.sandbox.network.sockets`                         | Mapping                         | Direct Unix-socket permissions. Omission permits no Unix sockets.                                                                     |
| `sandbox.network.allow_local_binding`<br>`resolver.sandbox.network.allow_local_binding` | `auto`, `null`, `true`, `false` | Local binding and related native socket access. Omission selects `auto`; see [Local sockets and binding](#local-sockets-and-binding). |

### Proxy settings

Fields under `sandbox.network.proxy` or `resolver.sandbox.network.proxy`:

| Node                                                                                                  | Accepted values                                    | Meaning and default                                                                                                                     |
| ----------------------------------------------------------------------------------------------------- | -------------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------- |
| `sandbox.network.proxy.mode`<br>`resolver.sandbox.network.proxy.mode`                                 | `full`, `limited`                                  | Proxy methods and protocols. Default: `full`.                                                                                           |
| `sandbox.network.proxy.socks5`<br>`resolver.sandbox.network.proxy.socks5`                             | `disabled`, `tcp`; `tcp_udp` is currently rejected | SOCKS support in full mode. Default: `tcp`. Omit the entire field in limited mode.                                                      |
| `sandbox.network.proxy.allow_upstream_proxy`<br>`resolver.sandbox.network.proxy.allow_upstream_proxy` | `true` or `false`                                  | Permit onward routing through the trusted launch environment's proxy when true. Default: `false`; destination/method rules still apply. |
| `sandbox.network.proxy.domains`<br>`resolver.sandbox.network.proxy.domains`                           | Mapping                                            | Destination allow/deny rules. Omission uses no worker destinations or the resolver's generated download allowlist.                      |

| Value of `sandbox.network.proxy.mode` or `resolver.sandbox.network.proxy.mode` | Proxy behavior                                                                                                              |
| ------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------- |
| `full` (default)                                                               | HTTP/HTTPS proxying and the selected SOCKS mode.                                                                            |
| `limited`                                                                      | Plain HTTP GET, HEAD, and OPTIONS only. POST, HTTPS CONNECT, and SOCKS are rejected; this does not provide read-only HTTPS. |

`socks5: disabled` disables SOCKS, while `tcp` enables SOCKS5 TCP.
The schema recognizes `tcp_udp`, but Console rejects it on every supported platform.
Supplying any `socks5` field with limited mode is invalid, including `disabled`.
Direct channels granted through socket/local permissions do not pass through the proxy's HTTP-method filter.

### Destination rules

Fields under `sandbox.network.proxy.domains` or `resolver.sandbox.network.proxy.domains`:

| Node                                                                                    | Accepted values                          | Meaning and default                                               |
| --------------------------------------------------------------------------------------- | ---------------------------------------- | ----------------------------------------------------------------- |
| `sandbox.network.proxy.domains.allow`<br>`resolver.sandbox.network.proxy.domains.allow` | List of hostname patterns or IP literals | Permitted destinations. Default within an explicit mapping: `[]`. |
| `sandbox.network.proxy.domains.deny`<br>`resolver.sandbox.network.proxy.domains.deny`   | List of hostname patterns or IP literals | Blocked destinations; matching deny rules win. Default: `[]`.     |

Patterns use hostnames without URL schemes, paths, or ports:

- `api.example.com` matches that host.
- `*.example.com` matches subdomains, excluding `example.com` itself.
- `**.example.com` matches the domain and its subdomains.

An explicit `sandbox.network.proxy.domains` or `resolver.sandbox.network.proxy.domains` mapping replaces the generated destination defaults; `{}` permits no destinations.
Ordinary configuration-layer merging still applies first.
See [Resolver defaults](RESOLVER.md#expanded-public-default) for the complete generated package-download allowlist.

### Local sockets and binding

The `sandbox.network.sockets` and `resolver.sandbox.network.sockets` mappings each have one field:

| Value of `sandbox.network.sockets.unix_sockets` or `resolver.sandbox.network.sockets.unix_sockets` | Direct Unix-socket access                                                                                |
| -------------------------------------------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------- |
| `[]` (default)                                                                                     | No Unix-socket grants.                                                                                   |
| List of absolute path strings, after leading-`~` expansion                                         | Grant the named sockets. Currently supported on macOS; nonempty lists are rejected on Linux and Windows. |
| `dangerously_allow_all`                                                                            | Permit all Unix sockets. Supported on macOS/Linux; rejected on Windows.                                  |

| Value of `sandbox.network.allow_local_binding` or `resolver.sandbox.network.allow_local_binding` | Behavior                                                                                      |
| ------------------------------------------------------------------------------------------------ | --------------------------------------------------------------------------------------------- |
| `auto` or `null` (default)                                                                       | On Unix, false for workers and true for resolvers. On Windows, true for a managed proxy.      |
| `true`                                                                                           | Permit local binding and related native local-socket access.                                  |
| `false`                                                                                          | Prohibit it. Rejected for Windows managed proxies, which require bidirectional host loopback. |

On macOS, local permissions include loopback traffic and DNS.
Linux uses a private network namespace; local binding does not expose it to a host browser.
Filesystem permissions still apply to Unix sockets.
See [Sandbox configuration](SANDBOX_CONFIGURATION.md#unix-sockets-and-local-binding) for channel and platform limits.

## Package preparation

Fields under `resolver`:

| Node                           | Accepted values                   | Meaning and default                                                                                                                                                               |
| ------------------------------ | --------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `resolver.inherit_environment` | `true` or `false`                 | Inherit launch variables for package preparation. Omission follows the top-level `inherit_environment` value.                                                                     |
| `resolver.environment`         | Mapping of names to string values | Override individual shared `environment` values for preparation. Default: `{}`.                                                                                                   |
| `resolver.sandbox`             | Sandbox mapping or `null`         | Independent preparation permissions using the [sandbox schema](#sandbox). On macOS/Linux, omission supplies host reads, cache writes, private temp storage, and a download proxy. |

```yaml
resolver:
  inherit_environment: true
  environment:
    OMP_NUM_THREADS: "1"
```

Windows preparation uses host permissions and rejects explicit `resolver.sandbox` settings.
The `cache` setting controls generated cache locations and grants.
See [Package preparation and caches](RESOLVER.md) for mirrors, trust, and custom cache grants.

## Tool presentation

Fields under `tool`:

| Node                | Accepted values                        | Meaning and default                                                                                |
| ------------------- | -------------------------------------- | -------------------------------------------------------------------------------------------------- |
| `tool.description`  | String or `null`                       | Replace the complete `send` tool description. Omitted or `null`: generated text.                   |
| `tool.instructions` | String or `null`                       | Replace the server instructions returned during initialization. Omitted or `null`: generated text. |
| `tool.parameters`   | Mapping of parameter names to settings | Customize descriptions and visibility. Omission keeps the normal schema and descriptions.          |

Strings are used verbatim; an empty string replaces generated text with empty text.
These settings affect what the client/agent sees, while execution permissions and argument contracts remain unchanged.

### Parameter settings

Each entry under `tool.parameters.<name>` accepts:

| Node                                 | Accepted values                                                    | Meaning and default                                                                                                                                                           |
| ------------------------------------ | ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tool.parameters.<name>.enabled`     | `true` or `false`                                                  | Advertise and accept the parameter when true. Default: `true`. False hides it and rejects explicitly supplied values, including `null`; omission retains the runtime default. |
| `tool.parameters.<name>.description` | String or `null`                                                   | Replace that parameter's description. Omitted or `null`: generated text.                                                                                                      |
| `tool.parameters.<name>.parameters`  | Mapping of nested parameter names to the same settings             | Customize fields inside an object parameter. Omission keeps their defaults; scalar fields accept no nested names.                                                             |
| `tool.parameters.<name>.schema`      | Unchanged metadata mapping from `config resolve --full`, or `null` | Read-only inspection of argument types/constraints. Changing metadata is invalid; omit it when writing configuration by hand.                                                 |

Available top-level names are `r`, `python`, `sql`, `requirements`, `control`, `stdin`, and `timeout_ms`.
Source fields are available only for the selected `languages`.
Under `tool.parameters.requirements.parameters`, the names are `action`, `r`, `python`, `python_version`, `duckdb`, and `exclude_newer`.
Each uses the same parameter settings above.

```yaml
tool:
  parameters:
    requirements:
      parameters:
        python:
          description: Python registry package requirements.
    timeout_ms:
      enabled: false
```

Hiding `timeout_ms` retains its 60,000 ms wait default; it does not stop work when the wait expires.
Unknown or unavailable parameter names fail launch.
See [Tool configuration](TOOL_CONFIGURATION.md) for complete text overrides and editing materialized schemas, and [MCP send reference](API.md) for argument values.

## Null, empty values, and merging

| Value         | Effect                                                                                                                                             |
| ------------- | -------------------------------------------------------------------------------------------------------------------------------------------------- |
| Omitted field | Keep the inherited value, or use its default when no earlier layer supplies it.                                                                    |
| `{}`          | An empty mapping merges with earlier entries; it does not clear an inherited mapping.                                                              |
| `[]`          | Replace an earlier list with an empty list, where an empty list is accepted. `languages` and `python.first_available` must remain nonempty.        |
| `null`        | Replace the earlier value. Accepted only where listed above; for whole permission nodes it clears inherited policy settings and restores defaults. |
| `""`          | An actual empty string. Valid for environment values and tool prose; not a way to unset a variable.                                                |

Mappings merge recursively; lists, scalars, and `null` replace earlier values.
Launch `--default-config KEY=VALUE` assignments apply below configuration files; `-c KEY=VALUE` overrides apply above them.
The final merged settings must be valid: for example, `sandbox.filesystem`, `sandbox.network.proxy`, `environment`, and `resolver` require mappings rather than `null`.
To replace an inherited mapping, clear it with one override and supply the replacement with a later override.
See [Merge rules](CONFIGURATION.md#merge-rules) for examples.

Configuration is captured when the server starts.
Open a new server connection after editing it; worker restart does not reload configuration.
