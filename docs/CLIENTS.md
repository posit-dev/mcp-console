# Client integration

Use these references when writing an application that connects to Console or configuring the tool definition a model receives.
To connect an existing MCP client without writing integration code, use [Get started](getting-started.qmd).

## Connect from application code

[Python clients and adapters](PYTHON.md) provides direct calls and integrations with model frameworks.
[R and ellmer](../r/README.md) adds Console to an R chat.

The [Python API](https://t-kalinowski.github.io/mcp-console/python/reference/index.html) and [R API](https://t-kalinowski.github.io/mcp-console/r/reference/index.html) document exact signatures and complete examples.

## Use the MCP tool

[`send` reference](API.md) documents arguments, results, and output limits.
[Calls, input, and control](SEND_OPERATIONS.md) covers sequencing, polling, interactive input, interruption, and restart.

[Requirements API](REQUIREMENTS_API.md) documents dependency actions and their effects on a running session.
[Runtime reference](RUNTIME_REFERENCE.md) provides detailed language behavior and helper contracts.

## Configure the model's interface

[Tool configuration](TOOL_CONFIGURATION.md) covers language visibility, descriptions, parameter visibility, and configuration exports for evaluations.
These settings change the interface presented to a client; they are not sandbox permissions.

## Developing Console itself

The relay and worker protocols are implementation details, not alternative MCP interfaces.
They are documented under [Development](DEVELOPMENT.md).
