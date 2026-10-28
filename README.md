# MCP Console

> **MCP Console has moved to [posit-dev/mcp-console](https://github.com/posit-dev/mcp-console).** Use that repository for new issues, pull requests, and development, and visit the [documentation website](https://posit-dev.github.io/mcp-console/).
> This repository preserves earlier issues, pull requests, reviews, and Git history.

An interactive workbench for agents working with data in R, Python, and SQL.

Load data once, explore questions, refine plots, read help, and debug code with objects and models still in memory.
Use one language or combine them in the same process, without intermediate files.

Code runs in an [OS-level sandbox](docs/SANDBOX.md) by default.
Console prepares environments and packages automatically; no Console configuration file is required.
It works with MCP-compatible clients such as Codex and Claude Code and does not depend on a particular model provider.

**Development preview:** interfaces may change.
macOS, Linux, and Windows x64 are supported.

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

This registers Console globally, configures the client to launch `uvx mcp-console serve`, and loads Console tools up front.
Add `--directory .` to register it for the current directory instead.
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

[Documentation website](https://posit-dev.github.io/mcp-console/) · [Get started](docs/getting-started.qmd) · [User guides](docs/README.md) · [Client integration](docs/CLIENTS.md) · [Development](docs/DEVELOPMENT.md)

`mcp-console` was previously called [`mcp-repl`](https://github.com/posit-dev/mcp-console/tree/e60f582d943727a17a38a4162ba63a55b6c773c6).
Licensed under the [MIT license](LICENSE).
