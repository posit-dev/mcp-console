# Install and connect

Install Console's prerequisites, then connect your coding agent or another MCP client.
[Get started](getting-started.qmd) walks through a first analysis once you are connected.

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) so `uvx` is available to your client.
It can run Console without a separate Console installation.

## Supported platforms

Prebuilt packages are available for macOS and Linux on ARM64 and x86-64, and for Windows x64.
Sandboxed Windows sessions require Windows 11 with [Microsoft Execution Containers (MXC)](https://support.microsoft.com/en-us/servicing/os/windows-11/2026/08/kb5120998-windows-11-24h2-25h2-update#microsoft-execution-containers).

If Console fails to start on Linux, see [Linux host compatibility](LINUX_COMPATIBILITY.md) for missing libraries or sandbox restrictions.

## R, Python, and SQL

To let your agent use R, install it separately, for example with [rig](https://github.com/r-lib/rig#id-installation) and `rig add release`.
Python and SQL work without R.
The first session may download Python and packages.

To verify the selected runtimes and prepare caches before opening a client, follow [Check your installation](CLI.md#check-your-installation).

<span id="build-from-source"></span>

To install the development version, follow [Build from source](BUILD_FROM_SOURCE.md).

<span id="mcp-clients"></span>

## Codex and Claude Code

Run the command for your client:

```sh
uvx mcp-console configure-client codex
uvx mcp-console configure-client claude
```

This registers Console globally, so it is available across projects, and loads its tools up front.
It also adds Console's `send` tool to the client's allowlist so calls do not need individual approval, subject to explicit client restrictions.
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

## R and Python clients {#connect-from-application-code}

<span id="from-r"></span> <span id="from-python"></span>

To use Console from R or Python, follow [Use Console from R](R.md) or [Use Console from Python](PYTHON.md).
These clients use the same workbench and settings.

<span id="configure-the-models-interface"></span>

[Build a data assistant](BUILD_ON_CONSOLE.md) shows how to change the tool descriptions, exposed languages and parameters, and prepared environment.
[Tool configuration](TOOL_CONFIGURATION.md) documents the exact settings.

After connecting, [Sessions and languages](BUILTIN_RUNTIME.md) explains how your agent can work with Console.
