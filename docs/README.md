# Documentation

These guides describe the implemented development preview.
Start with [Get started](getting-started.qmd); use the references when you need an exact argument, setting, or limit.

## Use Console

[Working with R, Python, and SQL](BUILTIN_RUNTIME.md) explains persistent state, language bridges, database connections, and plots.
[Calls, input, and control](SEND_OPERATIONS.md) explains polling, interactive input, cancellation, and restart.

Configure a session with [Configuration](CONFIGURATION.md), manage [Dependencies](REQUIREMENTS.md), and keep results with [Recordings and reports](RECORDING.md).
Integrate Console into an application with the [Python clients](PYTHON.md) or [R and ellmer](../r/README.md).

Before choosing permissions, read [Sandbox and trust](SANDBOX.md).
Host-specific setup is in [Linux compatibility](LINUX_COMPATIBILITY.md) and [Windows](WINDOWS.md).

## Public reference

| Interface                          | Reference                                                                               |
| ---------------------------------- | --------------------------------------------------------------------------------------- |
| MCP tool                           | [`send` API](API.md)                                                                    |
| Command line                       | [CLI](CLI.md), [help printouts](cli-help.qmd)                                           |
| Application configuration          | [Configuration](CONFIGURATION.md)                                                       |
| Worker permissions                 | [Sandbox configuration](SANDBOX_CONFIGURATION.md)                                       |
| Preparation permissions and caches | [Resolver](RESOLVER.md)                                                                 |
| Environment variables              | [Environment](ENVIRONMENT.md)                                                           |
| Python package                     | [Generated API](https://t-kalinowski.github.io/mcp-console/python/reference/index.html) |

## Develop Console

Read [Development](DEVELOPMENT.md) for the local workflow and [Architecture](ARCHITECTURE.md) for state and process ownership.
[Relay protocol](RELAY_PROTOCOL.md) and [Worker protocol](WORKER_PROTOCOL.md) are internal integration references, not the public MCP API.

[Tool descriptions](TOOL_DESCRIPTIONS.md) covers agent-facing prose.
[Release](../RELEASE.md) covers packaging and publishing.
[Open work](TODO.md) collects unresolved items without treating every current limitation as a promised feature.
The [glossary](GLOSSARY.md) defines shared terms.

Historical proposals in [`design-sketches/`](../design-sketches/) are not current specifications and are not published as website guides.
