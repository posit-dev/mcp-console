# MCP Console

An interactive, sandboxed workspace for AI agents working with data in R, Python, and SQL.

Load data once, explore questions, refine plots, read help, and debug code with objects and models still in memory.

Console provides a persistent session—a REPL—through one MCP tool, `send`.
The agent can run code, inspect the result, and continue from the same live state.

Code runs in an [OS-level sandbox](docs/SANDBOX.md) by default, with configurable limits on filesystem writes and direct network access.
By default, Console prepares environments and packages automatically; no Console configuration file is required.

The agent can use [R, Python, or SQL](docs/BUILTIN_RUNTIME.md), or combine them in one session.
The runtimes are embedded in one process, so data can move between languages without intermediate files.
Console works with MCP-compatible clients such as Codex and Claude Code and does not depend on a particular model provider.

**Development preview:** interfaces may change.
macOS and Linux are supported; Windows x64 support is [experimental](docs/WINDOWS.md).

## Quickstart

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install Console from PyPI:

```sh
uv tool install mcp-console
```

PyPI provides wheels for macOS and Linux on ARM64 and x86-64, plus experimental Windows x64 support.
Linux wheels require glibc 2.35 or later and the runtime libraries listed in [Get started](docs/getting-started.qmd).
That guide also covers optional R installation and [building from source](docs/getting-started.qmd#build-from-source).

Configure your MCP client to launch `mcp-console serve` over stdio, following the [client setup examples](docs/getting-started.qmd#connect-your-client).
Run the client and Console on the same host, with the client's shell and filesystem tools using the same workspace.

By default, Console prepares R packages, Python packages, and DuckDB extensions for the session.
The agent can request additional R packages, Python packages, and DuckDB extensions as it works.
See [Requirements and environments](docs/REQUIREMENTS.md) for details.

Ask your agent:

> Use Console to generate a small dataset, fit a model, and plot the result.
> Keep the data and model for follow-up.

## Where Console fits

- **Unattended analysis:** run long analyses with limits on where the agent can write files and connect to the network.
- **Model evaluations:** evaluate how an agent explores data, checks its work, and responds to results.
- **Conversational analysis:** discuss questions and results while the agent runs the analysis in its own session.
- **Large datasets:** keep data and intermediate results in memory for follow-up questions.
- **Debugging and package development:** pause execution, inspect live objects, and evaluate code where a failure occurs.

## Limits and trust boundaries

Code has shell-class capability.
The default sandbox permits host-file reads, restricts direct networking, and permits regular-file writes only in private temporary storage.
**Readable secrets are not protected.** Project configuration is trusted input and may widen permissions.

Dependency preparation has its own permissions.
Recordings contain code, input, and output without redaction, and have no automatic cleanup.
Read [Sandbox and trust](docs/SANDBOX.md) before changing permissions and [Recordings](docs/RECORDING.md) before sharing session files.

There is one session and one active cell per connection.
Errors do not roll back earlier effects.
Restart discards live objects but retains accepted requirements and recordings.

## Documentation

[Get started](docs/getting-started.qmd) · [User guides and reference](docs/README.md) · [Python clients](docs/PYTHON.md) · [R and ellmer](r/README.md) · [Development](docs/DEVELOPMENT.md)

MCP Console grew out of [mcp-repl](https://github.com/posit-dev/mcp-repl).
Licensed under the [MIT license](LICENSE).
