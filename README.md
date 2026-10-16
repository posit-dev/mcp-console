# MCP Console

An interactive workbench for agents working with data in R, Python, and SQL.

Load data once, explore questions, refine plots, read help, and debug code with objects and models still in memory.
Use one language or combine them in the same process, without intermediate files.

Code runs in an [OS-level sandbox](docs/SANDBOX.md) by default.
Console prepares environments and packages automatically; no Console configuration file is required.
It works with MCP-compatible clients such as Codex and Claude Code and does not depend on a particular model provider.

**Development preview:** interfaces may change.
macOS and Linux are supported; Windows x64 support is [experimental](docs/WINDOWS.md).

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Console:

```sh
uv tool install mcp-console
```

[Get started](docs/getting-started.qmd) covers platform prerequisites, optional R installation, and client setup.
[Build from source](docs/BUILD_FROM_SOURCE.md) covers the development version.

Register Console with your client:

```sh
mcp-console configure-client codex
# Or: mcp-console configure-client claude
```

This configures the client to launch `uvx mcp-console serve` and load Console tools up front.
Use `--dry-run` to preview the settings or `--no-auto-load` to keep the client's default tool discovery.
You can also start with `uvx mcp-console configure-client codex claude` to configure both clients without first installing Console.
Use `--pin-version VERSION` to select the Console version resolved by uvx, or `--no-uvx` to configure an absolute executable path.
`--pin-version current` pins the version running setup.
See [client setup](docs/getting-started.qmd#connect-your-client) for project/global scope and other clients.
Run the client and Console on the same host, with the client's shell and filesystem tools using the same workspace.

Ask your agent:

> Use Console to generate a small dataset, fit a model, and plot the result.
> Keep the data and model for follow-up.

## Where Console fits

- **Unattended analysis:** run long analyses with limits on filesystem writes and network access.
- **Model evaluations:** evaluate how an agent explores data, checks its work, and responds to results.
- **Conversational analysis:** discuss questions and results while the agent works in its own session.
- **Large datasets:** keep data and intermediate results in memory for follow-up questions.
- **Debugging and package development:** pause execution and inspect live objects where a failure occurs.

## Limits and trust boundaries

The default sandbox permits host-file reads, restricts direct networking, and permits regular-file writes only in private temporary storage.
**Readable secrets are not protected.** Project configuration is trusted input and may widen permissions.

Dependency preparation has its own permissions.
Recordings contain code, input, and output without redaction and have no automatic cleanup.
Read [Sandbox and trust](docs/SANDBOX.md) before changing permissions and [Recordings](docs/RECORDING.md) before sharing session files.

There is one session and one active code cell per connection.
Errors do not roll back earlier effects.
Restart discards live objects but retains accepted requirements and recordings.

## Documentation

[Get started](docs/getting-started.qmd) · [User guides](docs/README.md) · [Client integration](docs/CLIENTS.md) · [Development](docs/DEVELOPMENT.md)

MCP Console grew out of [mcp-repl](https://github.com/posit-dev/mcp-repl).
Licensed under the [MIT license](LICENSE).
