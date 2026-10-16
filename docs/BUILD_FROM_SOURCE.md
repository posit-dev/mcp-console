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
uv tool install git+https://github.com/t-kalinowski/mcp-console
```

This builds Console and its sandbox runner.
Preparing R or Python packages from source can require additional system libraries.

Configure your client to launch the installed `mcp-console serve` command.
A launcher using `uvx mcp-console serve` may instead run a published version.

To work on a local checkout, use the build and validation workflow in [Development](DEVELOPMENT.md#setup).
The [release guide](../RELEASE.md#private-sandbox-executable) covers native bundle requirements.
