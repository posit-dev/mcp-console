# Install and connect

Install Console's prerequisites, then connect your coding agent or another MCP client.
[Get started](getting-started.qmd) walks through a first analysis once you are connected.

For the `uvx` launcher, install [uv](https://docs.astral.sh/uv/getting-started/installation/).
It can run Console without a separate Console installation.

## Supported platforms

Console ships wheels and standalone archives for macOS, Linux, and Windows on x86-64 and ARM64.
Linux builds require glibc 2.28 or newer.
Sandboxed Windows sessions require Windows 11 with [Microsoft Execution Containers (MXC)](https://support.microsoft.com/en-us/servicing/os/windows-11/2026/08/kb5120998-windows-11-24h2-25h2-update#microsoft-execution-containers).
See [Windows sandbox requirements](SANDBOX_CONFIGURATION.md#windows-mxc-requirement) for host setup and troubleshooting, or [Windows development](WINDOWS_DEVELOPMENT.md) for native builds and validation.

If Console fails to start on Linux, see [Linux host compatibility](LINUX_COMPATIBILITY.md) for missing libraries or sandbox restrictions.

## R, Python, and SQL

To let your agent use R, install it separately, for example with [rig](https://github.com/r-lib/rig#id-installation) and `rig add release`.
Python and SQL work without R.
The first session may download Python and packages.

If `uvx` uses native Windows ARM64 Python and Console discovers R, the default R package setup needs a separate `ir` executable on `PATH`.
PyPI has no Windows ARM64 wheel for `r-lib-ir`.
To use R without `ir`, preinstall the R packages you need, including `reticulate` for sessions that use both R and Python.
Before connecting your client, omit `r.packages` and set `r.resolution` in your project's `.agents/console/config.yaml`.

```yaml
r:
  resolution: disabled
```

To verify the selected runtimes and prepare caches before opening a client, follow [Check your installation](CLI.md#check-your-installation).

<span id="mcp-clients"></span>

## Codex and Claude Code

Run the command for your client:

```sh
uvx mcp-console configure-client codex
uvx mcp-console configure-client claude
```

This registers Console globally, so it is available across projects, and loads its tools up front.
It also allows calls to all of Console's tools without individual approval, subject to explicit client restrictions.
Setup starts runtime checks and cache warming in the background, printing a log path for the results.
Use `--no-warmup` to skip that step.
Restart the client after setup.
Your client launches Console on the same host, usually in the workspace where the agent is working.

To register Console for a specific project, run the command from the project directory and add `--directory .`.
See [client configuration](CLI.md#configure-a-client) for project registration, version pinning, and launch options.

## Other MCP clients

Configure a stdio server with command `uvx` and arguments `["mcp-console", "serve"]`.
Clients using an `mcpServers` mapping can use:

```json
{
  "mcpServers": {
    "console": {
      "command": "uvx",
      "args": ["mcp-console", "serve"]
    }
  }
}
```

Console runs on the same host as the client, usually in the agent's workspace.
For remote work, run both on the remote host; Console does not manage remote connections or containers.

<span id="build-from-source"></span>

## Development version

The development version builds from source and requires the [build tools](BUILD_FROM_SOURCE.md#install-build-tools).
Install Console from GitHub, then run the command for your client to register the installed executable:

```sh
uv tool install git+https://github.com/posit-dev/mcp-console
mcp-console configure-client codex --no-uvx
mcp-console configure-client claude --no-uvx
```

Restart your client after setup.
The `--no-uvx` option registers the installed executable by its absolute path; the default `uvx` launch may run a published version instead.
To update, rerun the `uv tool install` command and restart your client.
[Build from source](BUILD_FROM_SOURCE.md) covers shell setup, other clients, and installing a specific branch, tag, or commit.

## Standalone executable

To embed Console in an application, download the archive for the host's OS and architecture and verify it against `SHA256SUMS` from the same [GitHub release](https://github.com/posit-dev/mcp-console/releases).
Extract the whole archive, preserving permissions.
Keep `bin`, `libexec`, and `share` together when moving the installation.

To run a program with Console's built-in `read_only` policy, invoke `bin/mcp-console sandbox --no-config -- PROGRAM ARG...`.
On Windows, use `bin\mcp-console.exe` with the same arguments.
For an application-owned policy, replace `--no-config` with `--config-file` followed by the absolute path to the application's YAML file.
Omit both options when the application intends to honor global and project settings, which can widen filesystem and network permissions.
See [Run commands in a sandbox](SANDBOX_COMMAND.md) and [Configuration is part of the trust decision](SANDBOX.md#configuration-is-part-of-the-trust-decision).

The launcher requires no MCP server, `ir`, Python, pip, or uv.
The target program uses its own runtime and dependencies.

For an MCP client, set the client command to the absolute path of the same executable and pass `serve`.
If the application supplies R and its packages, set `r.resolution` to `disabled` and omit `r.packages`.
Console then uses those libraries without invoking the R package resolver (`ir`).
Python can also use an [existing environment](RUNTIME_CONFIGURATION.md#use-an-existing-environment).

## R and Python clients {#connect-from-application-code}

<span id="from-r"></span> <span id="from-python"></span>

To use Console from R or Python, follow [Use Console from R](R.md) or [Use Console from Python](PYTHON.md).
These clients use the same workbench and settings.

<span id="configure-the-models-interface"></span>

[Build on Console](BUILD.md) shows how to change the tool descriptions, exposed languages and parameters, and prepared environment.
[Tool configuration](TOOL_CONFIGURATION.md) documents the exact settings.

After connecting, [Sessions and languages](BUILTIN_RUNTIME.md) explains how your agent can work with Console.
