# Tool configuration

Customize which languages, parameters, and descriptions the agent sees through your [configuration file](CONFIGURATION.md).
These settings change presentation, not sandbox permissions.
For interpreter selection and database startup, see [Languages and startup](RUNTIME_CONFIGURATION.md).
For startup packages and preparation policies, see [Package settings](PACKAGE_CONFIGURATION.md).
For a guide with examples, see [Build a data assistant](BUILD.md#build-a-data-assistant).

## Model-visible languages

The [tool field reference](CONFIGURATION.md#tools-and-sessions) lists `languages` and its default.


```yaml
languages: [sql, python]
```

Choose a nonempty subset of `r`, `python`, and `sql`.
A later list replaces the whole list.
This controls the tool schema and accepted source fields, not interpreter availability or permissions.
SQL can use a hidden R or Python provider.
Notes, requirements, polling, input, and controls remain available unless hidden through `tool.parameters`.

A hidden source field is rejected even when its value is `null`.
Exposing a language does not install its runtime.
This is a presentation setting, not a security boundary.

## Tool descriptions and parameters

The `tool` mapping accepts `description`, `instructions`, and `parameters`; see the [tool field reference](CONFIGURATION.md#tools-and-sessions) for their values and defaults.

For example:

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

| Setting                              | Lifecycle and purpose                                                                                                                                                            |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `tool.instructions`                  | Available to the client when the connection initializes. Introduce the workbench and when to select it; a client may use this text for a server summary or deferred tool search. |
| `tool.description`                   | Returned when the client lists tools, alongside `send`'s input schema. Explain when and how to call `send`, including polling, state, and result handling.                       |
| `tool.parameters.<name>.description` | Part of that input schema. Explain the corresponding argument and how to supply its value.                                                                                       |

The client controls when these texts enter the model's context and whether initialization instructions supply a server summary.
See [deferred tool discovery](TOOL_DESCRIPTIONS.md#deferred-tool-discovery) for client-specific behavior.

### Parameter settings

Each entry under `tool.parameters.<name>` accepts `enabled`, `description`, nested `parameters`, and read-only `schema` metadata; see the [parameter field reference](CONFIGURATION.md#tools-and-sessions).

The [parameter field reference](CONFIGURATION.md#tools-and-sessions) lists available names, including nested requirement fields.
Source fields are available only for the selected `languages`.

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

To inspect generated descriptions and parameter metadata before editing, see [Inspect and edit resolved configuration](CONFIGURATION.md#inspect-and-edit-resolved-configuration).
