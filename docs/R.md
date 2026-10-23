# Use Console from R

The `mcp.console` package adds Console's persistent R, Python, and SQL workbench to an ellmer chat.
The client runs in your R session; the agent's analysis runs in Console.

## Add Console to a chat

Install the package and its dependencies:

```r
pak::pak("github::posit-dev/mcp-console/r")
```

Register a Console tool with your chat:

```r
library(ellmer)
library(mcp.console)

chat <- chat_openai()
tool <- console_tool()
chat$register_tool(tool)
chat$chat("Explore mtcars with Console. Keep the data for follow-up.")
```

Your model provider's credentials belong in the calling R process.
Keep the tool available while the chat is using it; state persists while its Console connection remains alive.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

`console_tool()` uses Console on PATH or launches the latest published release through `reticulate::uv_run_tool()`.
The [package guide](../r/README.md#select-console) covers selecting an executable or version.

## Choose the workspace and settings

Pass `project` to select the directory where Console starts.
Without an explicit `config`, Console discovers global and project configuration files.
For example:

```r
tool <- console_tool(project = "/path/to/project")
```

The directory must already exist.
Relative paths in Console use this directory without changing the calling R session's working directory.

To supply settings directly, pass a `ConsoleConfig` object:

```r
config <- ConsoleConfig(sandbox = SandboxPolicy(profile = "workspace"))
tool <- console_tool(config = config, project = "/path/to/project")
```

An explicit configuration skips file discovery.
Here the `workspace` profile allows ordinary project writes; network access is configured separately.
[Configure a Console session](https://posit-dev.github.io/mcp-console/r/articles/configuration/) covers inspecting, editing, saving, and reusing settings.
The [R API](https://posit-dev.github.io/mcp-console/r/reference/) documents the complete configuration interface.

## Work with the session

The shared guides explain [sessions and languages](BUILTIN_RUNTIME.md), [packages and environments](REQUIREMENTS.md), [sandbox permissions](SANDBOX.md), and [recordings and reports](RECORDING.md).
These describe the same workbench your agent uses from R.

[Build a data assistant](BUILD_ON_CONSOLE.md) shows how to change the descriptions, languages, and parameters the agent sees.
For a complete analysis example, see [Giving an agent an R session](https://posit-dev.github.io/mcp-console/r/articles/autonomous-report/).
