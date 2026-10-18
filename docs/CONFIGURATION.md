# Configuration

Console works without a configuration file.
Use one to choose interpreters, startup packages, environment variables, and permissions.

For project settings, create `.agents/console/config.yaml` in the directory where Console will start.
For example, to use the project's Python environment and allow project writes:

```yaml
python: .venv
sandbox:
  profile: workspace
```

The Python environment must already exist.
Standard-library code needs no additional packages; install third-party packages for the analysis you intend to run.
Console does not manage Python package installation for an existing environment.

Project configuration is trusted input and can widen permissions.
See [Files and precedence](#files-and-precedence) before loading settings from an unfamiliar project.
Worker and package-preparation permissions are independent; see [Sandbox configuration](SANDBOX_CONFIGURATION.md) and [Package preparation and caches](RESOLVER.md).

[Configuration reference](CONFIGURATION_REFERENCE.md) lists every public field, its accepted values, and its default behavior.

## Create a configuration file

```sh
mcp-console config init
mcp-console config init --global
mcp-console config init --directory /path/to/project
mcp-console config init -c r.resolution=disabled -c 'r.packages=[]' --dry-run
```

By default, `config init` creates `.agents/console/config.yaml` in the current directory, including outside Git.
It does not search parent directories.
`--directory PATH` selects an existing project directory and interprets relative settings there.
`--global` creates `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
The two options are mutually exclusive.
These files are shared by clients that launch Console with the same configuration locations.
Client registration remains a separate operation; see [Connect your client](getting-started.qmd#connect-your-client).

The starter selects `sandbox.profile: workspace`, with comments explaining that it allows ordinary workspace writes and makes the project's `.git`, `.agents`, `.codex`, and `.aws` paths read-only.
Other readable host files remain readable.
Annotated examples cover temporary storage, network access and localhost services, package preparation, Python environments, environment variables, and cache locations.
Lines beginning with `##` are explanations; lines beginning with `#` are YAML examples to uncomment.
The header links to the full configuration reference.

Use repeated `-c` or `--config` assignments to override the starter settings, with the same ordering and merge rules as launch overrides.
Assignments may appear before `config` or after `init`.
Supplied sections show active YAML with their guidance; unconfigured sections retain commented examples.
To start with read-only permissions instead, replace the starter profile:

```sh
mcp-console config init -c sandbox.profile=read_only
```

Later profile overrides replace the starter profile without retaining explicit workspace write grants.
Filesystem settings add exceptions to the selected profile; to restrict writes to selected directories, choose `read_only` and supply those directories in `sandbox.filesystem.read_write`.

Only the starter permissions and supplied settings are saved: initialization does not copy discovered global or project configuration or expand built-in defaults.
Explicit values are retained even when they equal built-in defaults, so a new project layer can override different global values.
Configuration-source flags such as `--config-file` and `--no-config` are not accepted.
`--default-config` is a launch setting and is not accepted by initialization; use `-c` for settings to save in the new file.

The command validates settings without starting interpreters or preparing packages, creates the configuration directory if needed, and reports the file path and contents.
An existing destination causes an error and is left untouched, including with `--dry-run` or `-c`.
`--dry-run` prints the proposed path and contents without creating files or directories.

## Python environment selection

Select a preinstalled interpreter or standard virtual environment:

```yaml
python: .venv
```

Existing Python requires CPython 3.10 or newer with a usable shared embedding library.
Standard-library code needs no third-party packages.

The equivalent mapping is `python: {existing: .venv}`.
A venv directory needs `pyvenv.cfg` and its ordinary `bin/python` or Windows `Scripts/python.exe`.
On Windows, select a real interpreter or virtual environment, not a Windows App Execution Alias.
Executable paths preserve venv identity and symlinks.
Recognized Conda installations are unsupported.

Explicit selection overrides launch-time `RETICULATE_PYTHON`, uses preinstalled packages, and disables managed Python preparation.
Existing selections accept no package or version settings.
A broken selection fails rather than falling back.

To choose the first available candidate:

```yaml
python:
  first_available:
    - existing: .venv
    - active_venv
    - managed:
        version: "3.13"
        packages: [numpy, pandas, matplotlib]
```

`active_venv` reads the incoming `VIRTUAL_ENV`; the launcher must preserve it.
Only absence advances to the next candidate.
A present but broken environment fails.
The list must be flat and nonempty, with at most one `active_venv` and at most one managed candidate.
The managed candidate must be last.
Exhausting it is an error.

For an explicitly managed environment without a fallback list:

```yaml
python:
  version: ">=3.12,<3.14"
  packages: [numpy, pandas]
```

`python.packages`, `python.version`, and `python.resolution` select managed Python.
The nested `python.managed` form remains supported, including inside `first_available`.
Do not combine these direct fields with `existing`, `managed`, or `first_available` in the same mapping.
An empty `python: {}` explicitly selects managed Python with its defaults.

Managed selection uses uv-managed CPython, not a fallback interpreter on `PATH`.
It excludes interpreters older than CPython 3.10 before ranking available versions or applying version constraints.
Without R, it requires `uv` on the execution host.
Omitted or `null` `python` retains the launch-time `RETICULATE_PYTHON` compatibility behavior described in [Environment](ENVIRONMENT.md).

Before merging configuration layers, Console expands the direct managed fields to `python.managed`.
Flat and nested settings in separate layers therefore combine with the same [merge rules](#merge-rules).
`config resolve` continues to print the nested form.

## R executable selection

```yaml
r: /opt/R/bin/R
```

Or combine selection with other R settings:

```yaml
r:
  executable: /opt/R/bin/R
  packages: [dplyr]
  vanilla: true
```

Select an installed R executable or ordinary launcher, not a directory, Rscript, or command with arguments.
Explicit selection overrides installation hints and PATH discovery.
The scalar form expands to `r: {executable: PATH}` before merging.

R and Python executable selectors follow the shared [path rules](#paths).

Console retains the selected installation across restarts.
If its captured executable/resources change incompatibly, start a new server connection.
Executables and code they load remain trusted inputs, not immutable sandbox assets.

## Startup package declarations

```yaml
r:
  packages: [dplyr, dbplyr, ggplot2]
python:
  packages: [numpy, pandas, matplotlib]
```

Omitting a package list keeps that runtime's defaults.
A supplied list replaces them; `[]` selects no optional packages.
This does not remove infrastructure, installed packages, or caches.
Preparation makes packages available without attaching or importing them.

Managed Python `version` is one quoted version or constraint string.
The selected fallback candidate can carry the same managed options.
Later `requirements.reset` restores the effective startup declaration, not the contents of a subsequently edited file.

See [Dependencies](REQUIREMENTS.md) for default packages, accepted requirement syntax, and live changes.

## Dependency-resolution policies

Use `r.resolution` and `python.resolution` to choose `implicit`, `explicit`, or `fixed`.
R defaults to `implicit`; managed Python defaults to `explicit`, so the agent declares dependencies through `requirements.python` before importing them.
R also accepts `disabled`; for preinstalled-only Python, select an existing environment.

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  packages: [numpy, pandas]
  resolution: fixed
```

See [resolution policies](REQUIREMENTS.md#resolution-policy-admission) for what each mode permits.
These settings govern Console's preparation, not arbitrary installation code or network access inside cells.

To let missing Python imports request package preparation, opt in through `config.yaml`:

```yaml
python:
  resolution: implicit
```

## Native R startup

R uses normal startup by default: environment files, profiles, workspace restoration, `.First()`, and default-package attachment are R's responsibility.
Startup runs inside the worker with its ordinary permissions.
Console passes `--quiet`, `--interactive`, and `--no-save`, and does not save a workspace at shutdown.

Set `r.vanilla: true` to request R's native `--vanilla` behavior.
This affects the session's R runtime; discovery and dependency preparation remain profile-free.

Console attaches its bridges and managed graphics after native startup.
Startup plots therefore use R's native device.
A profile diagnostic that R survives does not make the worker unusable.
If R fails initialization or is interrupted, fix the file externally and explicitly restart; ordinary calls do not retry initialization.
Edits to `.Rprofile` take effect on worker restart because R reads it again.

## Session startup source

`startup` selects a native SQL connection before the first SQL cell:

```yaml
startup:
  language: python
  code: |
    import sqlite3
    connection = sqlite3.connect(":memory:")
    _console.sql_connection(connection)
```

Use `language: r` with a normal DBI connection and `.console$sql_connection(connection)` for R.
The program runs after runtime helpers are available, once per worker generation.
It must finish with a usable, user-created connection selected on that same interpreter.
Retrieving or resetting the managed default does not satisfy startup.

Source is captured at server launch and limited to 32 KiB in its encoded transport form, including escaping.
It is not automatically echoed as a submitted cell, but emitted output and errors are visible.
Startup code has ordinary worker permissions and may have side effects.

Failure or interruption withholds SQL until explicit restart; there is no managed fallback or automatic replay.
Restart may repeat earlier side effects.
After success, resetting the SQL connection restores managed DuckDB without rerunning startup.
See [SQL connections](RUNTIME_REFERENCE.md#sql-and-duckdb).

## Shared environment

```yaml
environment:
  OMP_NUM_THREADS: "4"
resolver:
  environment:
    OMP_NUM_THREADS: "1"
```

The worker gets inherited launch values followed by `environment`.
Preparation gets those values followed by `resolver.environment`.
Console-owned runtime, cache, storage, proxy, and transport assignments take precedence afterward.

`inherit_environment` defaults to true; `resolver.inherit_environment` defaults to the shared setting.
False removes inherited workload values but preserves explicit mappings.
It does not clear the supervisor's environment.
Values must be strings; empty strings are values, not unsetting instructions.
On Windows, each effective environment must have unique names under the process environment's case-insensitive comparison, including after file/CLI layering and shared/resolver merging.
Use the same spelling when overriding a variable; `TEMP` and `temp` cannot both be configured.
Names remain case-sensitive on Unix.

Shared credentials also reach preparation.
Environment changes inside cells do not reconfigure the resolver.
See [Environment variables](ENVIRONMENT.md).

## Cache selection

Sandboxed sessions default to Console-specific caches.
Set `MCP_CONSOLE_CACHE_DIR` to an absolute directory to relocate those caches.
Set `cache: host` to reuse host cache locations.
`--no-sandbox` defaults to host caches and rejects `cache: console` and explicit sandbox policies.
The [`inherit_codex` profile](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex) also selects host caches when the first call requests full access.
[Resolver caches](RESOLVER.md#cache-locations) documents locations and permission implications.

To retain other file settings while disabling enforcement, clear inherited permission nodes explicitly:

```sh
mcp-console serve --no-sandbox -c sandbox=null -c resolver.sandbox=null
```

## Files and precedence

`serve` and ordinary `sandbox` launches read, in order:

1. Each `--default-config KEY=VALUE` assignment, in argument order.
2. Global `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
3. Project `.agents/console/config.yaml` in the launch directory.
   No ancestor search is performed.
4. Each `-c KEY=VALUE` override, in argument order.

Missing automatic files are normal.
Selected files must be readable YAML mappings; invalid files fail launch.
`--config-file PATH` instead selects one required YAML or JSON mapping and skips both automatic locations.
Launch defaults still apply beneath the selected file, and CLI overrides apply afterward.
Discovery exclusions skip files, not `--default-config` or `-c` assignments.

| Option                | Automatic files read |
| --------------------- | -------------------- |
| No option             | Global, then project |
| `--no-project-config` | Global only          |
| `--no-global-config`  | Project only         |
| `--no-config`         | Neither              |

Discovery exclusions cannot accompany `--config-file`.
Options may appear before or after the subcommand.
They do not edit files or change recording-location selection.

**Project configuration is trusted launcher input.** It can select executables, supply credentials, and widen sandbox permissions.
Console loads this layer automatically without a separate approval step, whether or not the directory is in Git and including with global client registration.
Review and authorize these settings before launching Console; `--no-project-config` skips the project layer for unfamiliar projects.
Global settings and launch defaults, including inherited client permissions, are not an enforcement ceiling: project settings can replace the selected profile or add permissions.

Settings are captured when the server starts.
Editing YAML requires a new server connection; restarting a worker does not reload it.

## Paths

Relative paths use the launch directory, including paths written in a global file.
A leading `~` expands to the launching user's home in interpreter selectors, sandbox and resolver filesystem paths, supported Unix-socket paths, configuration filenames, and `--writable-root`.
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

## Merge rules

Mappings merge recursively.
Lists, scalars, and `null` replace the earlier value.
An empty mapping does not clear an existing mapping; an empty list does clear a list.
Validation and defaults apply to the final merged settings.

For example, global `environment: {KEEP: global, CHANGE: global}` and project `environment: {CHANGE: project}` retain `KEEP` and replace `CHANGE`.

Use `null` followed by a new mapping to replace a whole branch through ordered overrides:

```sh
mcp-console serve -c sandbox.network=null -c 'sandbox.network={proxy: {mode: limited}}'
```

The final value must still be valid for that setting.
Changing one selector does not implicitly remove incompatible siblings.

## Command-line overrides

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
The separate native `sandbox --config-env` interface cannot be combined with application configuration options; see [CLI](CLI.md).

## Console home

Set `MCP_CONSOLE_HOME` in the launching environment to a directory to relocate global configuration and fallback session storage.
It does not relocate package caches, change `HOME`, or override project-local recording selection.
The directory must be absolute after leading-`~` expansion; empty or relative values are errors when it is needed.

Without it, Console uses an absolute `HOME`.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-home discovery.
See [Recordings](RECORDING.md) for project-local storage.

## Inspect and edit resolved configuration

Inspect the settings Console would use before starting a session:

```sh
mcp-console config resolve
mcp-console config resolve --full > session.yaml
# Review session.yaml, then launch with that file alone.
mcp-console serve --config-file session.yaml
```

`config resolve` uses the same file selection, ordered `-c` overrides, and validation as `serve`.
It does not start interpreters, prepare packages, or create session/cache directories.
Use `--no-config` for built-in settings without discovered files, `--config-file PATH` for one required file, and `--format json` for JSON instead of YAML.

The default `--prune` omits values equal to the binary's built-in defaults, independently of discovered files.
An unchanged configuration produces `{}`.
Changed arrays and scalar values stay complete, including empty arrays and strings; overrides needed to preserve conditional defaults remain explicit.
`--full` includes defaults and generated tool descriptions; it cannot be combined with `--prune`.
See [Tool configuration](TOOL_CONFIGURATION.md#inspect-and-edit-resolved-configuration) before editing that generated text or schema metadata.

Use the same launch directory for export and execution so relative paths retain their meaning.
For host execution, pass `--no-sandbox` to both commands; launch flags are not stored in YAML.
Runtime-dependent interpreter selection, automatic package lists, and inherited environment values remain policies.
An export does not pin installed dependencies or copy the host environment.
Exported interpreter paths must be representable as UTF-8.

R exposes the same operations through its [configuration workflow](https://t-kalinowski.github.io/mcp-console/r/reference/config-workflow.html).

## Model-visible languages

See [language visibility](TOOL_CONFIGURATION.md#model-visible-languages).
Visibility changes the exposed tool fields, not runtime isolation or permissions.

## Tool descriptions and parameters

See [Tool configuration](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters) for descriptions, initialization instructions, and parameter visibility.

## Migration

Global configuration now contributes defaults even when a project file exists.
Use discovery exclusions to opt out.
Older native-shaped YAML, `extends`, and camelCase permission fields are not accepted.
Application profiles select workspace permissions or inherited client permissions with an explicit fallback; they are separate from complete native policy selectors.
Use the public [sandbox schema](SANDBOX_CONFIGURATION.md); complete native JSON remains a separate explicit interface.
