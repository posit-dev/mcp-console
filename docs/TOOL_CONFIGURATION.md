# Tool configuration

Customize what the agent sees for client integrations and evaluations.
These settings change presentation, not sandbox permissions.
Ordinary session setup is in [Configuration](CONFIGURATION.md).

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

`tool.description` replaces the complete `send` description returned by `tools/list`.
`tool.instructions` replaces the server instructions returned in the earlier `initialize` response.
Despite their location under `tool`, instructions describe the server as a whole.

| Setting                              | Lifecycle and purpose                                                                                                                                                            |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tool.instructions`                  | Available to the client when the connection initializes. Introduce the workbench and when to select it; a client may use this text for a server summary or deferred tool search. |
| `tool.description`                   | Returned when the client lists tools, alongside `send`'s input schema. Explain when and how to call `send`, including polling, state, and result handling.                       |
| `tool.parameters.<name>.description` | Part of that input schema. Explain the corresponding argument and how to supply its value.                                                                                       |

The client controls when these texts enter the model's context.
A client can fetch `tools/list` at connection time while deferring the full definition until the model searches for it.
In that workflow, initialization instructions may supply the initial server summary; other clients use their own server description or expose tools immediately.
Do not assume instructions are always shown, shown in full, or the only text visible before loading a tool.
See [deferred tool discovery](TOOL_DESCRIPTIONS.md#deferred-tool-discovery) for client-specific guidance and the protocol references.

Each parameter's `description` replaces its complete description; nested parameters use another `parameters` mapping.
Strings are used verbatim, including empty strings, without appending generated guidance or permission text.
Omitted descriptions and instructions keep their generated text.
Override each description you want to change; replacing the tool description does not clear parameter descriptions.

Set `enabled: false` to remove a parameter from the advertised schema and reject calls that supply it, including `null` values.
Omission keeps the parameter's existing runtime default.
For example, hiding `timeout_ms` makes every call use the existing 60,000 ms wait budget; work continues after that budget expires, and an empty call polls for more output.
Generated timeout guidance follows this choice.
Generated Python guidance uses installed packages when `requirements.python` or its parent `requirements` field is hidden under explicit resolution.
When hiding other parameters, supply matching description overrides if their generated prose refers to the hidden fields.

Parameter settings apply after `languages`; unknown or unavailable names and nested parameters under scalar fields fail launch.
Both `serve` and ordinary `sandbox` launches validate these settings before starting a worker or target process.
These settings customize descriptions and visibility, while parameter names, types, constraints, and runtime defaults retain their existing contracts.
They use the ordinary configuration merge rules and are captured once per server connection.
To hide only the timeout through the command line:

```sh
mcp-console serve -c tool.parameters.timeout_ms.enabled=false
```

## Inspect and edit resolved configuration

Materialize configuration before editing descriptions or running an evaluation:

```sh
mcp-console config resolve --full > session.yaml
# Inspect and edit session.yaml, then launch with that file alone.
mcp-console serve --config-file session.yaml
```

`config resolve` uses the same file selection, ordered `-c` overrides, and validation as `serve`.
Use `--no-config` for built-in configuration without discovered files, or `--config-file PATH` to start from one selected file.
`--format json` emits JSON instead of YAML.
The command does not start interpreters, prepare packages, or create session/cache directories.

With no output flag, `config resolve` uses `--prune`: it omits settings equal to the binary's built-in defaults, independently of global and project configuration.
The comparison uses the same sandbox choice as the export.
An unchanged configuration produces `{}`.
Mappings are pruned recursively; changed arrays and scalar values remain complete, including empty arrays and strings.
Overrides needed to preserve conditional defaults, such as resolver environment inheritance, remain explicit.
Tool prose is compared with generated text for the selected languages, permissions, and timeout visibility, retaining explicit text when those conditional defaults differ.
Use `--full` to include all configuration defaults; it cannot be combined with `--prune`.

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

Use the same launch directory for export and execution so relative permission paths retain their meaning.
For host execution, pass `--no-sandbox` to both `config resolve` and `serve`; launch flags are not stored in application YAML.
The selected configuration file bypasses automatic global/project discovery.

Values determined during runtime preparation remain policies: omitted automatic package lists, `python: null` for normal interpreter selection, inherited environment variables, and runtime-created cache and temporary paths.
Python `first_available` lists retain their candidates, including `active_venv`; the next launch evaluates them against its own environment and filesystem.
Managed fallback entries include their effective resolution policy even when an existing candidate is selected.
The export does not pin installed dependencies or copy the host environment.
Exported interpreter paths must be representable as UTF-8; other native paths produce a configuration error.
Changing `languages` may make saved parameter entries unavailable; resolve the desired language selection before editing the tool.

R exposes these operations through [`discover_console_config()`, `materialize_console_config()`, and `prune_console_config()`](https://t-kalinowski.github.io/mcp-console/r/articles/configuration.html#inspect-and-edit-configuration).
They return ordinary S7 `ConsoleConfig` objects with file discovery disabled.
`read_console_config()` imports one required JSON or YAML file; `ConsoleConfig(...)` builds settings directly in R.
Edit properties with `@`; use `$` to select named parameter entries.
The sandbox choice remains in the configuration, while executable and launch-directory choices remain explicit arguments to each call.
`console_tool()` discovers optional files by default; pass `config = ConsoleConfig(...)` to skip them.
