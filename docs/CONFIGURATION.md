---
filters:
  - _filters/config-init.py
---

# Configuration

This page lists the settings accepted in `config.yaml`, their values and defaults, and how configuration files combine.
Use it for project settings or defaults shared across projects.
For R lists with the same structure, see [Configure a Console session in R](https://posit-dev.github.io/mcp-console/r/articles/configuration/).

## Settings {#top-level-settings}

Write a YAML mapping using any of the fields below.
Omitted fields use defaults or settings from an earlier file.
Dotted names in the tables describe nesting; for example, `r.packages` is written as:

```yaml
r:
  packages: [dplyr, ggplot2]
  resolution: fixed
sandbox:
  profile: workspace
  network: [api.example.com]
```

This prepares the selected R packages, keeps their declaration fixed during the session, permits ordinary project writes, and allows the worker to contact one hostname through a proxy.
Package preparation has its own permissions.

The complete field reference is grouped below: [R](#r-executable-selection), [Python](#python-environment-selection), [packages](#dependency-resolution-policies), [permissions](#sandbox-settings), [environment and caches](#environment-and-caches), and [tools and sessions](#tools-and-sessions).
Lists replace earlier lists; mappings merge recursively.
See [Merge rules](#merge-rules) for `null` and empty values.

### R {#r-executable-selection}

| Setting        | Accepted values and defaults                                                                                                                                                                                                 |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `r`            | An executable path, a mapping of the fields below, or `null`. A path is shorthand for `r.executable`.                                                                                                                        |
| `r.executable` | A nonempty path to an R executable or launcher, or `null`. Omission uses installation hints and PATH; `null` clears an earlier selection. R package callers supply their calling R installation as a lower-priority default. |
| `r.packages`   | A list of single-line `ir` package references. Omission keeps the default startup packages; `[]` requests no optional packages.                                                                                              |
| `r.resolution` | `implicit` (default), `explicit`, `fixed`, or `disabled`; see [package policies](#dependency-resolution-policies).                                                                                                           |
| `r.vanilla`    | `true` or `false` (default). True requests native `--vanilla` startup.                                                                                                                                                       |

Select an executable, not an installation directory, Rscript, or a command with arguments.
See [R startup behavior](RUNTIME_CONFIGURATION.md#native-r-startup) for profiles and workspace restoration.

### Python {#python-environment-selection}

| Setting                  | Accepted values and defaults                                                                                                                                                                                           |
| ------------------------ | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `python`                 | An existing executable/virtual-environment path, a mapping, or `null`. Omission honors launch-time `RETICULATE_PYTHON` when set, otherwise selects managed Python. An empty mapping explicitly selects managed Python. |
| `python.existing`        | A nonempty path to an existing CPython executable or standard virtual environment. Use its installed packages; accept no managed settings alongside it.                                                                |
| `python.managed`         | A mapping with `version`, `packages`, and `resolution`. These fields may instead appear directly under `python`.                                                                                                       |
| `python.version`         | A quoted version or constraint, such as `"3.13"` or `">=3.12,<3.14"`. Omission adds no constraint; CPython older than 3.10 is excluded.                                                                                |
| `python.packages`        | A list of named PEP 508 registry requirements. Omission keeps default startup packages; `[]` requests no optional packages.                                                                                            |
| `python.resolution`      | `explicit` (default), `implicit`, or `fixed`; see [package policies](#dependency-resolution-policies).                                                                                                                 |
| `python.first_available` | A nonempty list of `{existing: PATH}`, `active_venv`, and `{managed: {...}}` choices. At most one `active_venv` and one managed choice; managed must be last.                                                          |

A Python mapping uses direct managed fields or exactly one of `existing`, `managed`, and `first_available`.
Managed fields also apply inside a managed candidate.
An absent candidate advances to the next choice; a present but unusable environment fails.
For example:

```yaml
python:
  first_available:
    - existing: .venv
    - active_venv
    - managed:
        version: "3.13"
        packages: [pandas]
        resolution: fixed
```

See [Python environment behavior](RUNTIME_CONFIGURATION.md#python-environment-selection) for embedding requirements and selection details.

### Package policies {#dependency-resolution-policies}

| `r.resolution` / `python.resolution` | At startup                                                       | During execution                                                                                      |
| ------------------------------------ | ---------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------- |
| `implicit`                           | Prepare startup packages.                                        | Also prepare on explicit requests and supported missing-package loads/imports. The cell is not rerun. |
| `explicit`                           | Prepare startup packages.                                        | Accept explicit package requests; ordinary loads/imports do not prepare packages.                     |
| `fixed`                              | Prepare startup packages.                                        | Reject changes to the declaration.                                                                    |
| `disabled` (R only)                  | Use preinstalled packages, including Console's R infrastructure. | No managed preparation; `r.packages` must be omitted or empty.                                        |

For preinstalled-only Python, select an existing environment.
These policies control Console's preparation, not arbitrary installation commands in submitted code.
See [default startup packages](PACKAGE_CONFIGURATION.md#default-startup-packages) and [live package requests](API.md#requirements).

### Permissions {#sandbox-settings}

`sandbox` and `resolver.sandbox` accept mappings with the fields below, or `null` to restore their context's defaults.
Workers default to host reads, private temporary writes, and restricted networking without a proxy.
Preparation defaults additionally allow dependency-cache writes and package-source downloads.
Neither policy hides readable host secrets by default.

| Setting                                      | Accepted values and defaults                                                                                                                                                                                                            |
| -------------------------------------------- | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `sandbox`                                    | Permission mapping or `null` to restore worker defaults.                                                                                                                                                                                |
| `sandbox.profile`                            | `read_only` (default), `workspace`, `inherit_codex`, or `{inherit_codex: {fallback: workspace}}`. `null` uses the default.                                                                                                              |
| `sandbox.profile.inherit_codex.fallback`     | `read_only` (default) or `workspace`. Used when client permissions are unavailable.                                                                                                                                                     |
| `sandbox.filesystem.read_only`               | List of paths permitting reads without writes. Default within a supplied filesystem mapping: `[]`.                                                                                                                                      |
| `sandbox.filesystem.read_write`              | List of paths permitting reads, creation, edits, and deletion. Default within a supplied filesystem mapping: `[]`.                                                                                                                      |
| `sandbox.filesystem.deny`                    | List of paths denying reads and writes. Default within a supplied filesystem mapping: `[]`.                                                                                                                                             |
| `sandbox.tempdir`                            | `managed` (default): private writable storage removed after cleanup. `read_write`: grants writes to existing host temporary directories. `read_only`: no automatic temporary write grants.                                              |
| `sandbox.network`                            | `true` / `enabled` for direct networking; `false` / `restricted` for restricted networking without a proxy; a hostname pattern or sequence of patterns for a managed proxy; or a mapping below. `null` omits this field from its layer. |
| `sandbox.network.proxy`                      | Required mapping when network is a mapping; selects the managed proxy.                                                                                                                                                                  |
| `sandbox.network.proxy.mode`                 | `full` (default) or `limited`. Limited permits plain HTTP GET, HEAD, and OPTIONS, with no HTTPS CONNECT or SOCKS.                                                                                                                       |
| `sandbox.network.proxy.socks5`               | `tcp` (default) or `disabled`. Omit in limited mode. `tcp_udp` is currently rejected.                                                                                                                                                   |
| `sandbox.network.proxy.allow_upstream_proxy` | `false` (default) or `true` to permit onward routing through the trusted launch environment's proxy.                                                                                                                                    |
| `sandbox.network.proxy.domains.allow`        | List of hostname patterns or IP literals. An explicit domains mapping defaults to `[]`; omitting the mapping keeps the context's destination defaults.                                                                                  |
| `sandbox.network.proxy.domains.deny`         | List of blocked hostname patterns or IP literals, default `[]`. Matching deny rules win.                                                                                                                                                |
| `sandbox.network.sockets.unix_sockets`       | `[]` (default), absolute socket paths on macOS, or `dangerously_allow_all` on macOS/Linux. Windows rejects both forms of grant.                                                                                                         |
| `sandbox.network.allow_local_binding`        | `auto` / `null` (default), `true`, or `false`. Auto disables worker binding and enables resolver binding on Unix. Windows managed proxies require binding.                                                                              |

The same nested fields apply under `resolver.sandbox`, except its profile accepts only `read_only`, `workspace`, or `null`.
An explicit resolver filesystem replaces its generated filesystem entries, including cache grants; see [preparation filesystem grants](PACKAGE_CONFIGURATION.md#filesystem-grants).

`read_only` permits host reads and private temporary writes.
`workspace` additionally permits ordinary writes under the launch directory, protecting metadata such as `.git` and `.agents`; network access remains separate.
`inherit_codex` selects permissions supplied by a trusted client, including full access when granted, or uses its fallback.
Workspace profiles decline automatic writes at a filesystem root, home, or an ancestor of home.

Filesystem paths are concrete paths, not globs; create intended output directories before launch.
Narrower rules override broader ones; at the same path, deny wins over write, then read.
Domain patterns are hostnames rather than URLs: `*.example.org` matches subdomains and `**.example.org` also matches the apex.
An empty network sequence selects an empty proxy allow list.
For example, to allow report writes and one API:

```yaml
sandbox:
  filesystem:
    read_write: [./reports]
    deny: [./secrets]
  network: [api.example.com]
```

See [Sandbox permissions](SANDBOX_CONFIGURATION.md) for enforcement, platform limits, and examples.
Use the CLI's `--no-sandbox` or R's `ConsoleConfig(sandbox = FALSE)` for host execution; YAML `sandbox: false` does not disable enforcement.

### Environment, preparation, and caches {#environment-and-caches}

| Setting                        | Accepted values and defaults                                                                                                                                                                 |
| ------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `environment`                  | Mapping of names to string values, default `{}`. Shared by workers and preparation. Quote numbers and booleans; empty strings are values, not unset instructions.                            |
| `inherit_environment`          | `true` (default) or `false`. Whether children inherit the launch environment; explicit mappings still apply.                                                                                 |
| `resolver`                     | Mapping of `environment`, `inherit_environment`, and `sandbox` for package preparation.                                                                                                      |
| `resolver.environment`         | Mapping of string values overriding individual shared values for preparation, default `{}`.                                                                                                  |
| `resolver.inherit_environment` | `true` or `false`. Omission follows the shared `inherit_environment`.                                                                                                                        |
| `resolver.sandbox`             | Independent permission mapping, or `null` for preparation defaults; see [Permissions](#sandbox-settings).                                                                                    |
| `cache`                        | `managed` (alias `console`), `host`, `{directory: PATH}`, or `null`. Omission / `null` selects managed caches with sandboxing and host caches without it. Managed caches require sandboxing. |
| `cache.directory`              | Nonempty path; stores managed dependency caches under `<directory>/dependencies`. Relative paths use the launch directory.                                                                   |

For example, use a separate package source during preparation:

```yaml
inherit_environment: false
environment:
  TZ: UTC
resolver:
  environment:
    UV_INDEX_URL: https://packages.example.org/simple
    UV_HTTP_TIMEOUT: "120"
cache:
  directory: /path/to/console-cache
```

Console applies its own runtime/cache/proxy assignments after these values.
With client-derived permissions, automatic cache selection waits for the client's permissions.
See [recognized environment variables](ENVIRONMENT.md), [package sources](PACKAGE_CONFIGURATION.md#configuration), and [cache locations](PACKAGE_CONFIGURATION.md#cache-locations).

### Tools, startup, and sessions {#tools-and-sessions}

| Setting                              | Accepted values and defaults                                                                                                                                              |
| ------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `languages`                          | Nonempty list of `r`, `python`, and/or `sql`, or `null`. Default: all three. Changes tool visibility and accepted source fields, not runtime availability or permissions. |
| `tool`                               | Mapping of `description`, `instructions`, and `parameters`.                                                                                                               |
| `tool.description`                   | String replacing the complete `send` description, or `null` for generated text.                                                                                           |
| `tool.instructions`                  | String replacing server initialization instructions, or `null` for generated text.                                                                                        |
| `tool.parameters`                    | Mapping of parameter names to the entries below.                                                                                                                          |
| `tool.parameters.<name>.enabled`     | `true` (default) or `false`. False hides the argument and rejects calls supplying it, while omission retains its runtime default.                                         |
| `tool.parameters.<name>.description` | String replacing the parameter description, or `null` for generated text.                                                                                                 |
| `tool.parameters.<name>.parameters`  | Mapping of nested names to the same parameter settings.                                                                                                                   |
| `tool.parameters.<name>.schema`      | Unchanged metadata mapping from an exported configuration, or `null`. Read-only; omit when authoring settings.                                                            |
| `startup`                            | Mapping with `language` and `code`, or `null` (default: no startup program).                                                                                              |
| `startup.language`                   | Required `r` or `python`.                                                                                                                                                 |
| `startup.code`                       | Required nonblank source string without NUL. Must create and select a user-owned SQL connection on the chosen interpreter. Runs once per worker, including after restart. |
| `sessions`                           | Mapping with `max`.                                                                                                                                                       |
| `sessions.max`                       | Positive integer or `null`. Omission / `null` means unlimited; `1` permits only the default session.                                                                      |

Tool strings are used verbatim, including empty strings.
Top-level parameter names are `r`, `python`, `sql`, `note`, `session`, `requirements`, `control`, `stdin`, and `timeout_ms`; source fields must be selected by `languages`.
Nested `requirements` fields are `action`, `r`, `python`, `python_version`, `duckdb`, and `exclude_newer`.
For example, show only R and SQL and hide the wait-budget argument:

```yaml
languages: [r, sql]
tool:
  parameters:
    timeout_ms:
      enabled: false
sessions:
  max: 2
```

See [tool presentation](TOOL_CONFIGURATION.md), [database startup](RUNTIME_CONFIGURATION.md#session-startup-source), and [session limits](RUNTIME_CONFIGURATION.md#session-limits) for behavior and examples.

## Files and precedence

When Console starts, it reads configuration from these sources, in order:

| Source       | Location or option                                                  | Use                              |
| ------------ | ------------------------------------------------------------------- | -------------------------------- |
| Global file  | `~/.agents/console/config.yaml`                                     | Settings shared across projects. |
| Project file | `.agents/console/config.yaml` in the directory where Console starts | Settings for that project.       |
| Command line | `-c KEY=VALUE`                                                      | Settings for this launch.        |

You can use all three together.
Console layers them in the order shown: later settings override matching earlier settings, while other settings stay in effect.
Neither file is required.

See [Merge rules](#merge-rules) for how nested settings combine, or [Choose which files to load](#choose-which-files-to-load) to select a different file or skip either location.
To change the global file's location, see [Console home](#console-home).

## Create a configuration file

To create a project configuration, run this in your project directory:

```sh
mcp-console config init
```

This creates `.agents/console/config.yaml`.
To create a global configuration shared across projects, run:

```sh
mcp-console config init --global
```

Both commands create a [starter file](#generated-configuration) with commented examples.
The starter uses `sandbox.profile: workspace`, which allows ordinary project writes; see [Sandbox permissions](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage).
See [Initialization options](CLI.md#initialize-configuration) to preview the file, choose another project directory, or customize the starter.

## Inspect the combined settings {#inspect-and-edit-resolved-configuration}

From the directory where your client starts Console, inspect the combined settings:

```sh
mcp-console config resolve
```

This uses the same files and command-line overrides as starting a session, without starting interpreters or preparing packages.
Built-in defaults are omitted; add `--full` to include them.
You can [save the resolved configuration](#export-a-configuration) for a later session or [edit its generated tool descriptions and parameter settings](#edit-exported-tool-settings).
Add `--editable` to retain authored tool settings and omit generated tool descriptions and schema.
This keeps generated presentation responsive to later configuration edits while preserving explicit text, including empty strings and text equal to a generated default.
Combine it with `--full` to include configuration defaults in the editable document.

<span id="settings-by-task"></span> <span id="native-r-startup"></span> <span id="startup-package-declarations"></span> <span id="session-startup-source"></span> <span id="cache-selection"></span> <span id="shared-environment"></span> <span id="model-visible-languages"></span> <span id="tool-descriptions-and-parameters"></span>

Use [Settings](#top-level-settings) above to look up a configuration key.

## Choose which files to load

By default, Console reads the global file and then the project file.
Missing default files are skipped.
To select one required file instead of both defaults:

```sh
mcp-console serve --config-file session.yaml
```

**`--config-file` is repeatable.** Each occurrence adds a file, and files are read in argument order:

```sh
mcp-console serve --config-file defaults.yaml --config-file session.yaml
```

Here both files must exist.
`session.yaml` overrides matching settings from `defaults.yaml`; other settings remain in effect.
Neither the usual global file nor the usual project file is read.

To keep automatic discovery but skip one or both defaults:

| Option                | Files read           |
| --------------------- | -------------------- |
| No option             | Global, then project |
| `--no-project-config` | Global only          |
| `--no-global-config`  | Project only         |
| `--no-config`         | None                 |

Launch defaults still apply before these files, and command-line overrides apply afterward.
The options for skipping files cannot be combined with `--config-file`.
Options may appear before or after the subcommand.
They do not edit files or change recording-location selection.

### Allow a selected file to be absent

`--optional-config-file PATH` relaxes the existence requirement for a file already selected by `--config-file PATH`.
It is useful for launchers that layer a local customization file when present.
It does not select another file or add a second configuration layer:

```sh
mcp-console serve \
  --config-file defaults.yaml \
  --config-file local.yaml \
  --optional-config-file local.yaml
```

`defaults.yaml` is required; `local.yaml` is read afterward if it exists.
If `local.yaml` exists but is unreadable, not a regular file, or invalid, launch still fails.
Repeat the optional flag only for additional selected files that may be missing, using exactly the same path spelling as their `--config-file` options.
Passing only `--optional-config-file` is an error.

Console looks for the automatic project file only in the launch directory; it does not search parent directories.
The same underlying file is read only once, at its first selected position.
Configuration must be a readable YAML or JSON mapping; invalid files and unknown fields are errors.

Configuration files can choose which programs Console runs, provide credentials, and change permissions.
Console loads project files automatically, including when the client was registered globally.
Review a project's settings before launching Console; use `--no-project-config` to skip them for an unfamiliar project.
A project file can grant permissions beyond those set in a global file or inherited from the client.

Settings are captured when the server starts.
Restarting a worker does not reload configuration.

## Command-line overrides

For `serve` and ordinary `sandbox` launches, settings are applied in this order:

1. Each `--default-config KEY=VALUE` assignment, in argument order.
2. Global `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
3. Project `.agents/console/config.yaml` in the launch directory.
4. Each `-c KEY=VALUE` override, in argument order.

When `--config-file` is supplied, its ordered file list replaces the global and project sources.

```sh
mcp-console serve -c 'sandbox.filesystem.read_write=[.]'
mcp-console serve --no-project-config -c 'python=.venv'
mcp-console serve --config-file ./session.yaml
```

Use `--default-config KEY=VALUE` to supply a preference that configuration files can override:

```sh
mcp-console serve --default-config sandbox.profile=inherit_codex
mcp-console config resolve --default-config sandbox.profile=inherit_codex
```

Both assignment options accept the same dotted keys and YAML values.
Repeated assignments preserve order within their layer, including assignments before and after the subcommand.
Files always take precedence over `--default-config`, and `-c` always takes precedence over files, regardless of argument placement.
Explicit `null` and empty lists retain their normal replacement semantics; they are not treated as missing values.

Dotted keys address mappings, not list indexes.
Values accept YAML strings, booleans, finite numbers, lists, objects, and `null`.
Quote the assignment for the shell and quote strings that resemble numbers or booleans.
To set a literal dotted environment name, supply its containing mapping:

```sh
mcp-console serve -c 'environment={"APP.VERSION": "v1", COUNT: "42"}'
```

There is no variable expansion or file inclusion in these values.
`-c` accepts an assignment, not a file path or root configuration document.

## Supply sandbox configuration through an environment variable {#configuration-environment}

For standalone commands, `sandbox --config-env NAME` reads a YAML or JSON mapping from the named launch environment variable.
It accepts the same structure as a configuration file, including `sandbox`, `environment`, and `inherit_environment`.

```sh
CONSOLE_CONFIG='sandbox: {profile: workspace, network: restricted}' \
  mcp-console sandbox --config-env CONSOLE_CONFIG -- python analysis.py
```

This replaces global and project file discovery.
`--default-config` settings apply before the environment document, and ordered `-c` overrides apply after it.
`--writable-root` adds launch-specific write access as usual.
The selector cannot be combined with `--config-file`, `--optional-config-file`, `--no-config`, `--no-global-config`, or `--no-project-config`.

The variable must contain one configuration mapping; an unset variable, empty text, or invalid configuration fails before the command starts.
Use `{}` to select Console's defaults.
Relative paths use the launch directory, and omitted settings retain their ordinary defaults, including managed temporary storage.
The selected variable is removed from the command's environment, and stdin remains available to the command.
Environment and argument size limits still apply.

Runnable examples use child-specific environments in [shell](../examples/sandbox-config.sh), [Python](../examples/sandbox-config.py), and [R](../examples/sandbox-config.R).

## Merge rules

Mappings merge recursively.
Lists, scalars, and `null` replace the earlier value, after the shorthand normalization described below.
Validation and defaults apply to the final merged settings.

| Value         | Effect                                                                                                                                                       |
| ------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| Omitted field | Keep the earlier layer's value, or use the default when no layer supplies one.                                                                               |
| `{}`          | Merge an empty mapping; do not clear earlier entries. Shorthands are expanded first, as described below.                                                     |
| `[]`          | Replace a list with an empty list, where allowed. `languages` and `python.first_available` must remain nonempty.                                             |
| `null`        | Replace the earlier value, where allowed. Clearing `sandbox` or `resolver.sandbox` restores the policy defaults. A `network: null` field is omitted instead. |
| `""`          | Supply an empty string. This is valid for environment values and tool prose; it does not unset a variable.                                                   |

For example, global `environment: {KEEP: global, CHANGE: global}` and project `environment: {CHANGE: project}` retain `KEEP` and replace `CHANGE`.

To replace a whole mapping, clear it and then supply the replacement through ordered overrides:

```sh
mcp-console serve -c sandbox.network.proxy=null -c 'sandbox.network.proxy={mode: limited}'
```

The final value must still be valid for that setting.
For example, `sandbox.filesystem`, `sandbox.network.proxy`, `environment`, and `resolver` require mappings rather than `null`.
Changing one selector does not implicitly remove incompatible siblings.

Before merging each layer, Console expands an R path to `r.executable` and direct managed Python fields to `python.managed`.
An empty `python: {}` also selects the managed form.
Flat and nested runtime settings therefore merge at the same paths; [Languages and startup](RUNTIME_CONFIGURATION.md) describes the selection rules.

Network shorthands are also normalized before merging, for both worker and resolver policies.
Booleans become `enabled` or `restricted`; destination sequences and non-mode strings become `proxy.domains.allow`.
Thus a later proxy option does not discard an earlier shorthand allow list, and inherited deny rules remain in effect.
`network: null` is removed from its layer, so it preserves an earlier value or leaves the default when none was supplied.
These rules apply to files, environment configuration, defaults, and ordered command-line overrides.
See [Network access](SANDBOX_CONFIGURATION.md#network-choices) for the accepted forms and empty-list semantics.

## Export a configuration

To save the combined settings, including built-in defaults, and launch with that file:

```sh
mcp-console config resolve --full > session.yaml
# Review session.yaml, then launch with that file alone.
mcp-console serve --config-file session.yaml
```

`config resolve` uses the same file selection, ordered `-c` overrides, and validation as `serve`.
It does not start interpreters, prepare packages, or create session/cache directories.
Use `--no-config` to skip discovered files, `--config-file PATH` for one required file, and `--format json` for JSON instead of YAML.
To inspect the built-in defaults without launch overrides, run `mcp-console config resolve --no-config --full`.

The default `--prune` omits values equal to the binary's built-in defaults, independently of discovered files.
The comparison uses defaults for the same sandbox choice as the export.
An unchanged configuration produces `{}`.
Mappings are pruned recursively.
Changed arrays and scalar values stay complete, including empty arrays and strings; overrides needed to preserve conditional defaults remain explicit.
`--full` includes defaults and generated tool descriptions; it cannot be combined with `--prune`.

Tool prose is pruned against generated text for the selected languages, permissions, and timeout visibility.
Explicit text remains when those conditional defaults differ.

Use the same launch directory for export and execution so relative paths retain their meaning.
For host execution, pass `--no-sandbox` to both commands; launch flags are not stored in YAML.
Runtime-dependent interpreter selection, automatic package lists, inherited environment values, and runtime-created cache and temporary paths remain policies.
Python `first_available` retains its candidates, including `active_venv`; the next launch evaluates them against its own environment and filesystem.
Managed fallback entries include their effective resolution policy even when an existing candidate is selected.
An export does not pin installed dependencies or copy the host environment.
Exported interpreter paths must be representable as UTF-8.

### Edit exported tool settings

The full output contains captured settings, configuration defaults, generated instructions and descriptions, and every parameter selected by `languages`, including disabled parameters.
Each parameter has explicit `enabled` and `description` fields, read-only `schema` metadata for its type and constraints, and a `parameters` mapping for nested fields.
Generated Python parameter descriptions export as `null` so the next launch regenerates guidance for its selected environment and resolution policy.
An explicit Python description remains verbatim, including an empty string.
Changes to the `schema` metadata fail validation.
Use `enabled: false` to hide a parameter while retaining its details for inspection or re-enabling.

Materialized text is an explicit override: subsequently hiding `timeout_ms` does not rewrite the saved tool description.
Edit that text too if the experiment should remove its timeout guidance.
Removing an override or setting `description: null` restores generated text; an empty string replaces it with empty text.
The exported document keeps the ordinary configuration semantics; deleting a parameter entry restores its defaults rather than hiding it.

Changing `languages` may make saved parameter entries unavailable; resolve the desired language selection before editing the tool.

### Inspect and edit settings from R

R has two configuration constructors: [`ConsoleConfig()` and `load_console_config()`](https://posit-dev.github.io/mcp-console/r/articles/configuration/).
`ConsoleConfig()` accepts named R fields or one mapping, including `ConsoleConfig(yaml12::read_yaml("config.yaml"))`, without discovery or resolution.
`load_console_config()` combines selected global/project files with optional R `overrides` and returns the same editable S7 class.
Missing conventional files are skipped; explicitly supplied paths must exist.
Set `global_file = NULL` or `project_file = NULL` to skip that source.
`MCP_CONSOLE_HOME` affects the default global path only.

Edit properties with `@`; nested sections accept replacement lists.
The loader retains authored tool metadata and leaves generated descriptions to the server, so later permission or visibility edits do not freeze stale generated text.
The calling R installation is a low-priority launch default; file settings and explicit `r.executable` values win.
A selected file's `r: null` or `r.executable: null` clears that preference through subsequent edits and launch, leaving R discovery to Console.
`console_tools()` loads conventional files when `config` is omitted.
A supplied `ConsoleConfig` never discovers additional files, including on worker restart.
Executable and launch-directory choices remain explicit arguments; inherited environment variables are not snapshotted.

## Paths

Relative paths use the launch directory, including paths written in a global file.
A leading `~` expands to the launching user's home in interpreter selectors, `cache.directory`, sandbox and resolver filesystem paths, supported Unix-socket paths, configuration filenames, and `--writable-root`.
`MCP_CONSOLE_HOME` also accepts a leading `~`.

Expansion uses a nonempty, absolute `HOME` from the launch environment.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-profile discovery.
`R_USER`, `MCP_CONSOLE_HOME`, and child `environment` or `resolver.environment` overrides do not redefine that home.
An invalid home is an error when expansion is needed.

Only `~`, `~/...`, and, on Windows, `~\...` expand.
Quote a standalone `"~"` in YAML, where an unquoted `~` means `null`.
Use `./~/...` to name a literal `~` directory.
`~user`, globs, and environment-variable references are not expanded.
Expansion preserves symlinks and does not create directories.
Settings expand after file and CLI layering; configuration filenames expand before reading.
Materialized settings expose expanded home paths, including paths in Python candidate lists.
Other strings, including environment values and startup programs, retain their contents.

## Console home

Set `MCP_CONSOLE_HOME` in the launching environment to a directory to relocate global configuration and fallback session storage.
It does not relocate package caches, change `HOME`, or override project-local recording selection.
The directory must be absolute after leading-`~` expansion; empty or relative values are errors when it is needed.

Without it, Console uses an absolute `HOME`.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-home discovery.
See [Recordings](RECORDING.md) for project-local storage.

## Generated configuration

With no overrides, `config init` generates this `config.yaml`:

::: {.config-init-template}
[View the generated configuration](../tests/snapshots/cli/interface/test_config_init/dry_run_previews_the_created_contents.yaml).
:::
