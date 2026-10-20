# Sandbox and trust

Console runs your agent's code in an operating-system sandbox.
The sandbox controls which files that code can read or change and which network connections it can make.
These permissions also apply to programs the code starts.

This lets you give an agent access to data while choosing where it can save its work and which services it can contact.
The sandbox limits access; it does not establish that a project, dependency, or piece of code is trustworthy.

You can also [run commands directly](SANDBOX_COMMAND.md) from your terminal using the same sandbox.

## What code can do by default {#default-permissions}

With Console's built-in `read_only` profile, code can read files that your account can read, use private temporary storage, and start other programs.
It cannot ordinarily write to your project or make direct network connections.
For example, an agent can load a local CSV, calculate results, and make a plot without permission to edit the source data.

**Readable host files include readable secrets.** The `read_only` profile protects files from changes; it does not hide their contents.
You can deny access to specific paths when the agent should not be able to read them.

Your client setup and configuration can select a different profile.
In particular, [`mcp-console configure-client codex`](CLI.md#configure-a-client) sets up Console to inherit Codex's permissions unless a configuration file overrides that choice.

## Give a session the access it needs

If the agent needs to edit scripts or save reports in your project, the `workspace` profile permits those writes while keeping the project's `.git`, `.agents`, `.codex`, and `.aws` paths read-only by default.
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

The installation check, [`warmup`](CLI.md#check-your-installation), prepares dependencies with host permissions so they can be cached before a sandboxed session; its runtime checks still use the session's sandbox.

Installation and build steps can execute code, so the packages and package sources you choose still need to be trusted.
The session's `sandbox` settings and preparation's `resolver.sandbox` settings are independent.
[Package sources and caches](RESOLVER.md) explains how to change preparation permissions and download sources.

## Configuration is part of the trust decision

Console loads global and project configuration when it starts.
A project configuration can select executables, supply credentials, and widen permissions; global settings are not an upper limit.
Use `--no-project-config` for an unfamiliar project until you have reviewed and authorized its configuration.
[Configuration files](CONFIGURATION.md#files-and-precedence) explains which files Console reads.

When Console [inherits permissions from Codex](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex), it trusts the client to set its permissions.
This includes disabling both the session and package-preparation sandboxes when the client supplies full access.

## What remains after a session

Files saved to writable project paths are your data and remain after the session ends.
With the default managed temporary storage, Console removes its private temporary files after the sandboxed processes have stopped.
Cleanup failures are reported as errors.
A crashed or forcibly stopped runner can leave processes or temporary files behind; there is no separate recovery process.

Alternative temporary-storage settings and `--no-sandbox` have different cleanup guarantees.
See [Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage) and [Process cleanup](SANDBOX_CONFIGURATION.md#supported-hosts-and-lifetime-limits) for the exact behavior.
Recordings and reports can contain code, data, and output from the session; the sandbox does not make recorded output safe to share or execute.

## Further details

<span id="supported-hosts-and-lifetime-limits"></span> <span id="application-policy-and-launch"></span> <span id="policy-extensions-and-compatibility"></span>

Console reports sandbox setup failures without automatically retrying with weaker permissions.
The reference covers [platform support](SANDBOX_CONFIGURATION.md#native-capabilities) and [process cleanup](SANDBOX_CONFIGURATION.md#supported-hosts-and-lifetime-limits); [Architecture](ARCHITECTURE.md#application-policy-and-launch) covers implementation ownership and [policy extensions](ARCHITECTURE.md#policy-extensions-and-compatibility).

Continue to [Get started](getting-started.qmd) to connect your client, or use [Sandbox permissions](SANDBOX_CONFIGURATION.md) to configure access.
