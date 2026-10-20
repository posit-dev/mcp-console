# User guides

Learn how Console works, connect a client, or look up the settings and behavior for a specific task.

## Learn Console {#learn-console}

<span id="using-console"></span> <span id="use-console"></span> <span id="work-with-console"></span> <span id="how-console-works"></span>

These pages introduce Console and show how its pieces fit together:

1. [Sessions and languages](BUILTIN_RUNTIME.md) shows how your agent can run code, query databases, and inspect help, plots, and results.
2. [Packages and environments](REQUIREMENTS.md) explains how Console prepares packages, adds dependencies during an analysis, and uses environments you already manage.
3. [Sandbox and trust](SANDBOX.md) explains where code runs, what it can access, and how session permissions differ from package-preparation permissions.

When you want to try it, [Get started](getting-started.qmd) walks through installation, client setup, and a first analysis.
To use the sandbox directly from your terminal or scripts, see [Run commands in a sandbox](SANDBOX_COMMAND.md).

## Reference {#reference}

<span id="configure-a-project"></span>

Choose the task you want to accomplish.
Each reference includes the relevant settings, accepted values, defaults, and precise behavior.

| I want to…                                                                                        | Reference                                                                                  |
| ------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------ |
| Create a configuration file, understand which settings win, or inspect the resolved configuration | [Configuration files](CONFIGURATION.md)                                                    |
| Use a Python environment or select an R installation                                              | [Interpreters and startup](RUNTIME_CONFIGURATION.md)                                       |
| Choose startup packages or control when more may be prepared                                      | [Package declarations and policies](RUNTIME_CONFIGURATION.md#startup-package-declarations) |
| Connect to a database before the first SQL call                                                   | [Database startup](RUNTIME_CONFIGURATION.md#session-startup-source)                        |
| Choose what languages, parameters, or instructions the agent sees                                 | [Tool configuration](TOOL_CONFIGURATION.md)                                                |
| Allow project writes, protect files, reach a service, or inherit Codex permissions                | [Sandbox permissions](SANDBOX_CONFIGURATION.md)                                            |
| Use a package mirror or change cache locations                                                    | [Package sources and caches](RESOLVER.md)                                                  |
| Set environment variables or look up how Console handles them                                     | [Environment variables](ENVIRONMENT.md)                                                    |
| Look up language helpers, SQL connections, plotting, or output behavior                           | [Runtime behavior](RUNTIME_REFERENCE.md)                                                   |
| Find recorded output or turn an analysis into a report                                            | [Recordings and reports](RECORDING.md)                                                     |
| Run a program with filesystem and network limits                                                  | [Run commands in a sandbox](SANDBOX_COMMAND.md)                                            |
| Register a client, check an installation, or look up a command                                    | [Command-line reference](CLI.md) and [CLI help](cli-help.qmd)                              |

For a particular configuration key, use the [settings index](CONFIGURATION.md#settings-by-task).

## Platform setup

The shared [installation instructions](getting-started.qmd#install) cover macOS, Linux, and Windows.
See [Linux host compatibility](LINUX_COMPATIBILITY.md) or [Build from source](BUILD_FROM_SOURCE.md) for additional setup.
The [glossary](GLOSSARY.md) defines terms used in these pages.

## Other documentation

<span id="public-reference"></span>

[Build on Console](BUILD_ON_CONSOLE.md) shows how to create a tool or data assistant with a customized interface and a prepared session.
[Client integration](CLIENTS.md) is for applications that call Console.
It includes MCP arguments, result handling, polling, interactive input, and the R and Python client APIs.

<span id="develop-console"></span>

[Development](DEVELOPMENT.md) is for changing Console itself, including architecture, internal protocols, testing, and release procedures.
