# MCP Console

An interactive workbench for agents working with data in R, Python, and SQL.

Load data once, explore questions, refine plots, read help, and debug code with objects and models still in memory.
Use one language or combine them in the same process, without intermediate files.

Code runs in an [OS-level sandbox](docs/SANDBOX.md) by default.
Console prepares environments and packages automatically; no Console configuration file is required.
It works with MCP-compatible clients such as Codex and Claude Code and does not depend on a particular model provider.

**Development preview:** interfaces may change.
macOS, Linux, and Windows x64 are supported.

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then register Console with your client:

```sh
uvx mcp-console configure-client codex
# Or: uvx mcp-console configure-client claude
```

Restart your client after setup.
Run the client and Console on the same host, with the client's shell and filesystem tools using the same workspace.
[Get started](docs/getting-started.qmd) covers a first analysis; [Install and connect](docs/CLIENTS.md) covers platform prerequisites, optional R installation, and other clients.
[Build from source](docs/BUILD_FROM_SOURCE.md) covers the development version.

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

One connection can manage several independent sessions, each running one code cell at a time.
Errors do not roll back earlier effects.
Restart discards live objects but retains accepted requirements and recordings.

## Documentation

[Documentation website](https://posit-dev.github.io/mcp-console/) · [Get started](docs/getting-started.qmd) · [User guides](docs/README.md) · [Client integration](docs/CLIENTS.md) · [Development](docs/DEVELOPMENT.md)

`mcp-console` was previously called [`mcp-repl`](https://github.com/posit-dev/mcp-console/tree/e60f582d943727a17a38a4162ba63a55b6c773c6).
Licensed under the [MIT license](LICENSE).
