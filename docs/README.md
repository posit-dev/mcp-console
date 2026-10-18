# User guides

Start with [Get started](getting-started.qmd) to install Console and connect your MCP client.

## Using Console

<span id="use-console"></span> <span id="work-with-console"></span> <span id="how-console-works"></span>

[Sessions and languages](BUILTIN_RUNTIME.md) explains how your agent builds on earlier work, combines R, Python, and SQL, and inspects results.

[Packages and environments](REQUIREMENTS.md) explains automatic package preparation and existing environments.
[Recordings and reports](RECORDING.md) explains retained output and report creation.

## Configure a project

Use [Configuration](CONFIGURATION.md) to choose interpreters, startup packages, and environment settings.
[Configuration reference](CONFIGURATION_REFERENCE.md) lists each field's accepted values and default behavior.
Read [Sandbox and trust](SANDBOX.md) before granting permissions; [Sandbox configuration](SANDBOX_CONFIGURATION.md) documents the settings.

For command-line options, use [Command-line reference](CLI.md) or [CLI help](cli-help.qmd).
[Environment variables](ENVIRONMENT.md) and [Package downloads and caches](RESOLVER.md) cover less common setup needs.

## Platform setup

The shared [installation instructions](getting-started.qmd#install) cover macOS, Linux, and Windows.
See [Linux host compatibility](LINUX_COMPATIBILITY.md) or [Build from source](BUILD_FROM_SOURCE.md) for additional setup.
The [glossary](GLOSSARY.md) defines terms used in the guides.

## Other documentation

<span id="public-reference"></span>

[Client integration](CLIENTS.md) is for applications that call Console, model-framework adapters, and MCP tool configuration.
It contains the protocol-level details, including polling and input handling.

<span id="develop-console"></span>

[Development](DEVELOPMENT.md) is for changing Console itself.
It links to architecture, internal protocols, testing, and release procedures.
