---
sidebar: guides
---

# Build from source

Use a source build to install the development version.
For a published version with a prebuilt package, use [Get started](getting-started.qmd).

## Install build tools

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), Git, and [Rust through rustup](https://rustup.rs/).
The Rust compiler must meet the version requirement in [`Cargo.toml`](../Cargo.toml).

On macOS, install the Xcode Command Line Tools:

```sh
xcode-select --install
```

On Ubuntu, install the build tools and system libraries:

```sh
sudo apt-get update
sudo apt-get install -y build-essential git pkg-config libcap-dev libssl-dev libcurl4-openssl-dev binutils
```

For native Windows, follow [Windows build instructions](WINDOWS_DEVELOPMENT.md#build-from-source).

## Install Console

```sh
uv tool install git+https://github.com/posit-dev/mcp-console
```

This builds Console and its sandbox runner.
Preparing R or Python packages from source can require additional system libraries.

To install a specific branch, tag, or commit, append `@` and its name or identifier to the URL:

```sh
uv tool install git+https://github.com/posit-dev/mcp-console@BRANCH
```

If your shell cannot find `mcp-console`, run `uv tool update-shell` to add uv's tool directory to `PATH`, then open a new terminal.

## Connect a client

Run the command for your client to register the installed executable:

```sh
mcp-console configure-client codex --no-uvx
mcp-console configure-client claude --no-uvx
```

Restart your client after setup.
The `--no-uvx` option registers the executable by its absolute path.
Without it, the client launches `uvx mcp-console serve`, which may run a published version instead.
For other MCP clients, configure a stdio server with the absolute path of the installed `mcp-console` executable as the command and `["serve"]` as the arguments.
See [client configuration](CLI.md#configure-a-client) for the remaining options.

## Update

Rerun the installation command to fetch the latest commit and rebuild:

```sh
uv tool install git+https://github.com/posit-dev/mcp-console
```

The saved executable path stays the same, so you only need to restart your client.

To work on a local checkout, use the build and validation workflow in [Development](DEVELOPMENT.md#setup).
The [release guide](../RELEASE.md#private-sandbox-executable) covers native bundle requirements.
