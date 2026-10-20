# Sandbox and trust

Console runs your agent's code in an operating-system sandbox.
The sandbox controls which files that code can read or change and which network connections it can make.
These permissions also apply to programs the code starts.

For example, you can let an agent read a dataset and calculate results while limiting where it can save files or which services it can contact.
The sandbox limits access; it does not establish that a project, dependency, or piece of code is trustworthy.
Console reports sandbox setup failures without automatically retrying with weaker permissions.

For standalone programs, see [Run commands in a sandbox](SANDBOX_COMMAND.md).
For exact settings and platform limits, see [Sandbox permissions](SANDBOX_CONFIGURATION.md).

## What code can do by default {#default-permissions}

With Console's built-in `read_only` profile, code can read files that your account can read, use private temporary storage, and start other programs.
It cannot ordinarily write to your project or make direct network connections.
For example, an agent can load a local CSV, calculate results, and make a plot without permission to edit the source data.

**Readable host files include readable secrets.** The `read_only` profile protects files from changes; it does not hide their contents.
You can deny access to specific paths when the agent should not be able to read them.

Your client setup and configuration can select a different profile.
In particular, [MCP client setup for Codex](CLIENTS.md#codex-and-claude-code) configures Console to inherit Codex's permissions unless a configuration file overrides that choice.

## Give a session the access it needs

If the agent needs to edit scripts or save reports in your project, you can select the `workspace` profile.
Run this from the directory where your client launches Console:

```sh
uvx mcp-console config init
```

This creates `.agents/console/config.yaml` with `sandbox.profile: workspace`.
Restart the client to load it.
The profile permits project writes while keeping the project's `.git`, `.agents`, `.codex`, and `.aws` paths read-only by default.
The workspace is the directory where Console starts; changing directories during a session does not move that boundary.
You can also grant writes to selected output directories or block reads from particular paths.

Network access is a separate choice.
You can keep networking restricted, allow selected destinations through Console's proxy, or permit host networking.
Allowing an API through the proxy does not automatically grant every other kind of network connection.

See [Sandbox permissions](SANDBOX_CONFIGURATION.md) for profiles, file access, networking, and their platform limits.

## Why packages have separate permissions

Console may need to download and install a package before running code that uses it.
Package preparation therefore has its own permissions: by default, it can read host files, write dependency caches and private temporary files, and download from approved package sources.
Your session can use prepared packages while its own networking remains restricted.

Installation and build steps can execute code, so the packages and package sources you choose still need to be trusted.
The installation check, `warmup`, can also prepare dependencies ahead of a session; that preparation uses host permissions.

Reference: [Package-preparation permissions](PACKAGE_CONFIGURATION.md) and [installation checks](CLI.md#check-your-installation).

## Configuration is part of the trust decision

Console loads global and project configuration when it starts.
A project configuration can select executables, supply credentials, and widen permissions; global settings are not an upper limit.
Use `--no-project-config` for an unfamiliar project until you have reviewed and authorized its configuration.

When Console inherits permissions from Codex, it trusts the client to set its permissions.
This includes disabling both the session and package-preparation sandboxes when the client supplies full access.

Exact settings: [Inherited permissions](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex).
See [Configuration precedence](CONFIGURATION.md#files-and-precedence) for how files and launch options combine.

## What remains after a session

Files saved to writable project paths are your data and remain after the session ends.
With the default managed temporary storage, Console removes its private temporary files after the sandboxed processes have stopped.
Crashes, cleanup failures, and other temporary-storage settings can leave files behind.

Recordings also remain after the session ends.
They can contain code, data, and output from the session; review them before sharing or executing recorded code.

Reference: [Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage) and [process cleanup](SANDBOX_CONFIGURATION.md#supported-hosts-and-lifetime-limits).

<span id="supported-hosts-and-lifetime-limits"></span> <span id="application-policy-and-launch"></span> <span id="policy-extensions-and-compatibility"></span>

Continue with [Recordings and reports](RECORDING.md) to learn how to inspect and reuse the agent's code, output, and plots.
