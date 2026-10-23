# Build on Console

Use Console as the workbench for an assistant or workflow of your own.
You can choose the languages and arguments the model sees, describe the available data, and prepare the session with packages, connections, and helper functions.
The same settings work when you customize Console for an existing MCP client.

Your application chooses the model and manages the conversation and tool-calling loop.
Console manages the persistent session, package preparation, sandbox permissions, and recordings.
The [learning guides](README.md#learn-console) explain the workbench these integrations share.

## Build a data assistant

The [data assistant walkthrough](BUILD_ON_CONSOLE.md) starts with a sales database: expose SQL, select a read-only connection, describe the tables to the model, and connect the configured tool to a client.
It also shows how to prepare R objects or initialize a session from application code.

## Choose an integration

| Your application                 | Start here                                                                                                          |
| -------------------------------- | ------------------------------------------------------------------------------------------------------------------- |
| An ellmer chat in R              | [Use Console from R](R.md), including session configuration and a complete analysis example.                        |
| Python code or a model framework | [Use Console from Python](PYTHON.md), with synchronous and asynchronous clients and framework adapters.             |
| Your own MCP client              | [Drive send directly](SEND_OPERATIONS.md): submit cells, collect output, answer input, interrupt work, and restart. |
| An existing coding agent         | [Install and connect](CLIENTS.md), then use the same configuration files to customize its Console session.          |

Use the supplied adapters when integrating with a model framework so the model receives the connected server's actual tool schema.
For exact fields and signatures, see [Tool settings](TOOL_CONFIGURATION.md), the [send tool](API.md), the [R API](https://posit-dev.github.io/mcp-console/r/reference/), and the [Python API](https://posit-dev.github.io/mcp-console/python/reference/).

## Interface stability

Console is a development preview; public interfaces can change, with their changes documented.
Build against the documented configuration, MCP tool, and client packages.
Hidden subcommands, internal environment handoffs, process layouts, and recording journal schemas are implementation interfaces, not public compatibility contracts.
See the [interface boundaries](API.md#what-is-not-a-public-compatibility-contract) for details.
