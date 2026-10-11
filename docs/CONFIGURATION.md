# Configuration

Console works without a configuration file.
Use configuration to choose interpreters, startup packages, environment variables, permissions, and the exposed tool definition.
All settings use snake_case; environment-variable names retain their spelling.

A project using its own Python environment and allowing workspace writes might use:

```yaml
python: .venv
sandbox:
  filesystem:
    read_write: [.]
```

Worker permissions and dependency-preparation permissions are independent.
See [Sandbox configuration](SANDBOX_CONFIGURATION.md) and [Resolver](RESOLVER.md).

## Files and precedence

`serve` and ordinary `sandbox` launches read, in order:

1. Global `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
2. Project `.agents/console/config.yaml` in the launch directory.
   No ancestor search is performed.
3. Each `-c KEY=VALUE` override, in command-line order.

Missing automatic files are normal.
Selected files must be readable YAML mappings; invalid files fail launch.
`--config-file PATH` instead selects one required YAML or JSON mapping and skips both automatic locations.
CLI overrides still apply afterward.

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
Global settings are defaults, not a security ceiling.
Use `--no-project-config` when opening an unfamiliar project.

Settings are captured when the server starts.
Editing YAML requires a new server connection; restarting a worker does not reload it.
Relative paths are based on the launch directory, including paths written in a global file.

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

Set `MCP_CONSOLE_HOME` in the launching environment to an absolute directory to relocate global configuration and fallback session storage.
It does not relocate package caches, change `HOME`, or override project-local recording selection.
Empty or relative values are errors when the directory is needed; `~` is not expanded.

Without it, Console uses an absolute `HOME`.
On Windows, an absent or empty `HOME` uses `USERPROFILE` or native user-home discovery.
See [Recordings](RECORDING.md) for project-local storage.

## Model-visible languages

```yaml
languages: [sql, python]
```

Choose a nonempty subset of `r`, `python`, and `sql`.
A later list replaces the whole list.
This controls the tool schema and accepted source fields, not interpreter availability or permissions.
SQL can use a hidden R or Python provider.
Requirements, polling, input, and controls remain available unless hidden through `tool.parameters`.

A hidden source field is rejected even when its value is `null`.
Exposing a language does not install its runtime.
This is a presentation setting, not a security boundary.

## Tool descriptions and parameters

Use `tool` to customize the exposed `send` definition for evaluations or other clients:

```yaml
languages: [python]
tool:
  description: |-
    Run Python code in a persistent session.
    Poll with an empty call when work is still running.
  instructions: Persistent Python session.
  parameters:
    python:
      description: Execute one complete Python cell.
    requirements:
      description: Prepare dependencies before execution.
      parameters:
        python:
          description: Python registry package requirements.
    timeout_ms:
      enabled: false
```

`tool.description` replaces the complete tool description.
`tool.instructions` replaces the separate MCP initialization instructions.
Each parameter's `description` replaces its complete description; nested parameters use another `parameters` mapping.
Strings are used verbatim, including empty strings, without appending generated guidance or permission text.
Omitted descriptions and instructions keep their generated text.
Override each description you want to change; replacing the tool description does not clear parameter descriptions.

Set `enabled: false` to remove a parameter from the advertised schema and reject calls that supply it, including `null` values.
Omission keeps the parameter's existing runtime default.
For example, hiding `timeout_ms` makes every call use the existing 60,000 ms wait budget; work continues after that budget expires, and an empty call polls for more output.
Generated timeout guidance follows this choice.
When hiding other parameters, supply matching description overrides if their generated prose refers to the hidden fields.

Parameter settings apply after `languages`; unknown or unavailable names and nested parameters under scalar fields fail launch.
Both `serve` and ordinary `sandbox` launches validate these settings before starting a worker or target process.
These settings customize descriptions and visibility, while parameter names, types, constraints, and runtime defaults retain their existing contracts.
They use the ordinary configuration merge rules and are captured once per server connection.
To hide only the timeout through the command line:

```sh
mcp-console serve -c tool.parameters.timeout_ms.enabled=false
```

## Python environment selection

Select a preinstalled interpreter or standard virtual environment:

```yaml
python: .venv
```

The equivalent mapping is `python: {existing: .venv}`.
A venv directory needs `pyvenv.cfg` and its ordinary `bin/python` or Windows `Scripts/python.exe`.
Executable paths preserve venv identity and symlinks.
Recognized Conda installations are unsupported.

Explicit selection overrides launch-time `RETICULATE_PYTHON`, uses preinstalled packages, and disables managed Python preparation.
Existing selections accept no package or version settings.
A broken selection fails rather than falling back.
Python selection is unavailable with custom workers.

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
  managed:
    version: ">=3.12,<3.14"
    packages: [numpy, pandas]
```

Managed selection uses uv-managed CPython, not a fallback interpreter on `PATH`.
It excludes interpreters older than CPython 3.10 before ranking available versions or applying version constraints.
Without R, it requires `uv` on the execution host.
Omitted `python` retains the launch-time `RETICULATE_PYTHON` compatibility behavior described in [Environment](ENVIRONMENT.md).

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

For both R and Python executable selectors, relative paths use the launch directory and a leading `~` uses the server's absolute `HOME`.
`~user` and environment-variable references are not expanded.
This expansion is specific to interpreter selectors, not sandbox paths or `--config-file`.

Console retains the selected installation across restarts.
If its captured executable/resources change incompatibly, start a new server connection.
Executables and code they load remain trusted inputs, not immutable sandbox assets.

## Startup package declarations

```yaml
r:
  packages: [dplyr, dbplyr, ggplot2]
python:
  managed:
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

Use `r.resolution` and `python.managed.resolution` to choose `automatic` (the default), `explicit`, or `startup_only`.
R also accepts `disabled`; for preinstalled-only Python, select an existing environment.

```yaml
r:
  packages: [dplyr]
  resolution: explicit
python:
  managed:
    packages: [numpy, pandas]
    resolution: startup_only
```

The [policy table](REQUIREMENTS.md#resolution-policy-admission) defines what each mode permits.
These settings govern Console's preparation, not arbitrary installation code or network access inside cells.

## Native R startup

R uses normal startup by default: environment files, profiles, workspace restoration, `.First()`, and default-package attachment are R's responsibility.
Startup runs inside the worker with its ordinary permissions.
Console passes `--quiet`, `--interactive`, and `--no-save`, and does not save a workspace at shutdown.

Set `r.vanilla: true` to request R's native `--vanilla` behavior.
This affects the built-in worker, not discovery or dependency preparation, which remain profile-free.
Explicit R settings are unsupported with custom workers/relays.

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
See [SQL connections](BUILTIN_RUNTIME.md#sql-and-duckdb).

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

Shared credentials also reach preparation.
Environment changes inside cells do not reconfigure the resolver.
See [Environment variables](ENVIRONMENT.md).

## Cache selection

Sandboxed sessions default to Console-specific caches.
Set `cache: host` to reuse host cache locations.
`--no-sandbox` defaults to host caches and rejects `cache: console` and explicit sandbox policies.
[Resolver caches](RESOLVER.md#cache-locations) documents locations and permission implications.

To retain other file settings while disabling enforcement, clear inherited permission nodes explicitly:

```sh
mcp-console serve --no-sandbox -c sandbox=null -c resolver.sandbox=null
```

## Migration

Global configuration now contributes defaults even when a project file exists.
Use discovery exclusions to opt out.
Older native-shaped YAML, profiles, `extends`, and camelCase permission fields are not accepted.
Use the public [sandbox schema](SANDBOX_CONFIGURATION.md); complete native JSON remains a separate explicit interface.
