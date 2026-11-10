# mcp.console

Add a persistent, sandboxed R, Python, and SQL workbench to an [ellmer](https://ellmer.tidyverse.org/) chat.

```r
pak::pak("github::posit-dev/mcp-console/r")

library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tools(console_tools())
chat$chat("Explore mtcars with the console. Keep the data for follow-up.")
```

The provider's credentials belong in the calling R process.
The tools share one server connection; keep them reachable while the chat is using them.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

Install the ellmer and S7 development dependencies declared in `DESCRIPTION` with pak/remotes; `R CMD INSTALL` alone does not process `Remotes`.
Use a matching Console executable for development configuration features.

## Select Console

`console_tools()` uses Console on PATH, falling back to the latest published release through `reticulate::uv_run_tool()`.
Use `path` to select an executable or `version` to select a published release; they are mutually exclusive.

```r
console_tools(path = Sys.which("mcp-console"))
```

See [supported platforms](https://posit-dev.github.io/mcp-console/clients/#supported-platforms) for installation requirements.

## Session configuration

With no `config` argument, `console_tools()` loads trusted global and project settings.
Pass a `ConsoleConfig()` to use explicit settings and skip file discovery:

```r
config <- ConsoleConfig(
  r = list(packages = "dplyr"),
  python = ".venv",
  sandbox = list(profile = "workspace", network = "api.example.com")
)
tools <- console_tools(config, project = "/path/to/project")
```

The project and existing Python environment must already exist.
Relative paths use `project`, without changing the caller's working directory.
Console uses the calling R installation unless `r.executable` selects another, and does not install packages into an existing Python environment.

To load files once and edit the result, use `load_console_config()`:

```r
config <- load_console_config(overrides = list(r = list(packages = "dplyr")))
print(config)
tools <- console_tools(config)
```

<span id="discover-configuration-once"></span> <span id="inspect-and-edit-configuration"></span> <span id="existing-callers"></span>

[Configure a Console session](https://posit-dev.github.io/mcp-console/r/articles/configuration/) explains discovery, permissions, editing, and saved settings.
[Giving an agent an R session](https://posit-dev.github.io/mcp-console/r/articles/autonomous-report/) walks through an unattended report.
Both are also installed package vignettes.

## Behavior and safety

Built-in worker permissions allow host reads and private temporary writes, with no worker networking.
The example above adds project writes and access to one hostname.
Dependency preparation has independent permissions and may download packages.
`ConsoleConfig(sandbox = FALSE)` runs with host permissions and lacks the native runner's descendant-cleanup guarantee.
Read [Sandbox and trust](https://posit-dev.github.io/mcp-console/sandbox/) before granting more access.

The returned list follows the server's advertised tools and schemas.
For a direct call, select the tool by name, for example `tools$send(r = "1 + 1")`.
See the [send reference](https://posit-dev.github.io/mcp-console/api/) for requirement changes, polling, and controls.

Garbage collection of the last tool requests shutdown and waits up to 15 seconds before forcibly stopping the server process; that fallback does not prove descendant cleanup.
The [R reference](https://posit-dev.github.io/mcp-console/r/reference/) documents the three package functions and their arguments.
The [Console guides](https://posit-dev.github.io/mcp-console/readme/) cover the shared runtime and workflows.
