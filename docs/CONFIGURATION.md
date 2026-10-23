---
filters:
  - _filters/config-init.py
---

# Configuration files

Use configuration files to customize Console's default settings for a project or across projects.

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

<span id="settings-by-task"></span> <span id="top-level-settings"></span> <span id="r-executable-selection"></span> <span id="native-r-startup"></span> <span id="python-environment-selection"></span> <span id="startup-package-declarations"></span> <span id="dependency-resolution-policies"></span> <span id="session-startup-source"></span> <span id="cache-selection"></span> <span id="shared-environment"></span> <span id="model-visible-languages"></span> <span id="tool-descriptions-and-parameters"></span>

Use the [Reference index](REFERENCE.md#configuration) to look up a configuration key.

## Choose which files to load

Use these options to select or skip configuration files:

| Option                | Files read           |
| --------------------- | -------------------- |
| No option             | Global, then project |
| `--config-file PATH`  | Only the named file  |
| `--no-project-config` | Global only          |
| `--no-global-config`  | Project only         |
| `--no-config`         | None                 |

Launch defaults still apply before these files, and command-line overrides apply afterward.
The options for skipping files cannot be combined with `--config-file`.
Options may appear before or after the subcommand.
They do not edit files or change recording-location selection.

Console looks for the project file only in the directory where it starts; it does not search parent directories.
Missing automatic files are skipped, but an explicitly selected file must exist.
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
The selector cannot be combined with `--config-file`, `--no-config`, `--no-global-config`, or `--no-project-config`.

The variable must contain one configuration mapping; an unset variable, empty text, or invalid configuration fails before the command starts.
Use `{}` to select Console's defaults.
Relative paths use the launch directory, and omitted settings retain their ordinary defaults, including managed temporary storage.
The selected variable is removed from the command's environment, and stdin remains available to the command.
Environment and argument size limits still apply.

Runnable examples use child-specific environments in [shell](../examples/sandbox-config.sh), [Python](../examples/sandbox-config.py), and [R](../examples/sandbox-config.R).

## Merge rules

Mappings merge recursively.
Lists, scalars, and `null` replace the earlier value.
Validation and defaults apply to the final merged settings.

| Value         | Effect                                                                                                           |
| ------------- | ---------------------------------------------------------------------------------------------------------------- |
| Omitted field | Keep the earlier layer's value, or use the default when no layer supplies one.                                   |
| `{}`          | Merge an empty mapping; do not clear earlier entries. Runtime shorthands are expanded first, as described below. |
| `[]`          | Replace a list with an empty list, where allowed. `languages` and `python.first_available` must remain nonempty. |
| `null`        | Replace the earlier value, where allowed. Clearing `sandbox` or `resolver.sandbox` restores the policy defaults. |
| `""`          | Supply an empty string. This is valid for environment values and tool prose; it does not unset a variable.       |

For example, global `environment: {KEEP: global, CHANGE: global}` and project `environment: {CHANGE: project}` retain `KEEP` and replace `CHANGE`.

To replace a whole mapping, clear it and then supply the replacement through ordered overrides:

```sh
mcp-console serve -c sandbox.network=null -c 'sandbox.network={proxy: {mode: limited}}'
```

The final value must still be valid for that setting.
For example, `sandbox.filesystem`, `sandbox.network.proxy`, `environment`, and `resolver` require mappings rather than `null`.
Changing one selector does not implicitly remove incompatible siblings.

Before merging each layer, Console expands an R path to `r.executable` and direct managed Python fields to `python.managed`.
An empty `python: {}` also selects the managed form.
Flat and nested runtime settings therefore merge at the same paths; [Languages and startup](RUNTIME_CONFIGURATION.md) describes the selection rules.

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

R exposes these operations through [`discover_console_config()`, `materialize_console_config()`, and `prune_console_config()`](https://posit-dev.github.io/mcp-console/r/articles/configuration/#inspect-and-edit-configuration).
They return ordinary S7 `ConsoleConfig` objects with file discovery disabled.
`read_console_config()` imports one required JSON or YAML file; `ConsoleConfig(...)` builds settings directly in R.
Edit properties with `@`; use `$` to select named parameter entries.
The sandbox choice remains in the configuration, while executable and launch-directory choices remain explicit arguments to each call.
`console_tool()` discovers optional files by default; pass `config = ConsoleConfig(...)` to skip them.

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
