# mcp.console

`mcp.console` adds Console's persistent R, Python, and SQL workbench to an [ellmer](https://ellmer.tidyverse.org/) chat.

```r
pak::pak("github::posit-dev/mcp-console/r")

library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tools(console_tools())
chat$chat("Explore mtcars with the console. Keep the data for follow-up.")
```

The selected ellmer provider's credentials belong in the calling R process.
The returned tools share one server connection; state persists while any tool remains reachable.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

The development interface requires the ellmer and S7 development versions declared in `DESCRIPTION`.
Install the declared GitHub dependencies with pak/remotes; `R CMD INSTALL` alone does not process `Remotes`.
Use a matching Console executable for development configuration features.

## Select Console

`console_tools()` uses Console on PATH, falling back to the latest published release through `reticulate::uv_run_tool()`.
Named `path` selects an executable; named `version` selects a published release regardless of PATH.
They are mutually exclusive, and `...` must be empty.

```r
console_tools(path = Sys.which("mcp-console"))
```

On Windows, `console_tools()` can resolve the Console x64 wheel through `reticulate::uv_run_tool()`.
See [installation requirements](https://posit-dev.github.io/mcp-console/clients/#supported-platforms).

## Session configuration

Pass an explicit configuration to select a project's existing Python environment:

```r
config <- ConsoleConfig(python = ExistingPython(".venv"))
tools <- console_tools(config = config, project = "/path/to/project")
```

The environment and project directory must already exist.
Standard-library Python needs no third-party packages; install additional packages for your analysis.
Console does not manage Python package installation in an existing environment.
`project` sets the child's working directory without changing the caller's directory.
Relative paths use it.
An explicitly supplied `ConsoleConfig()` skips file discovery; omitting `config` discovers global and project settings, which are trusted inputs.

## Sandbox policies

To allow project writes while protecting metadata paths:

```r
config <- ConsoleConfig(sandbox = SandboxPolicy(profile = "workspace"))
tools <- console_tools(config = config, project = "/path/to/project")
```

Network permissions remain independent.
Use `SandboxFilesystem(read_write = "./output")` for a narrower write grant and create that directory before launching.
See [SandboxPolicy](https://posit-dev.github.io/mcp-console/r/reference/sandboxpolicy/) for temporary storage, path rules, and platform limits.

<span id="discover-configuration-once"></span> <span id="inspect-and-edit-configuration"></span> <span id="existing-callers"></span>

[Configure a Console session](https://posit-dev.github.io/mcp-console/r/articles/configuration/) covers discovery, inspection, editing tool descriptions, saved settings, and launching a configured session.
The [configuration workflow reference](https://posit-dev.github.io/mcp-console/r/reference/config-workflow/) documents exact signatures.

## Behavior and safety

Each tool uses the name, description, and argument schema advertised by the connected server.
`console_tools()` discovers every advertised tool, including tools introduced by future server versions.
Select a tool by name from the returned list for direct calls, for example `tools$send(r = "1 + 1")`.

For `send`, requirement actions are `get`, `add`, `set`, and `reset`; use `character()` for an explicitly empty list.
Changed live replacements normally require restart.
Read [Requirements](https://posit-dev.github.io/mcp-console/api/#requirements) before replacing a declaration.

By default, `console_tools()` discovers global and project configuration files.
Pass `config = ConsoleConfig()` to skip them and use built-in defaults: workers can read host files, write private temporary storage, and cannot use the network.
Dependency preparation uses its independent built-in permissions and may download packages.
`ConsoleConfig(sandbox = FALSE)` runs workers and preparation with host permissions, without the native runner's descendant-cleanup guarantee.
Read [Sandbox and trust](https://posit-dev.github.io/mcp-console/sandbox/) for default host reads, preparation permissions, and cleanup limits.

Garbage collection requests shutdown by closing server input, waits up to 15 seconds, then may forcibly stop the server process.
That fallback does not prove descendant cleanup.
Keep the tools reachable while the chat is using them.
Shutdown begins after the last tool becomes unreachable.

[Console documentation](https://posit-dev.github.io/mcp-console/readme/) covers the shared workbench, language behavior, and configuration.
[API reference](https://posit-dev.github.io/mcp-console/reference/) links the MCP interface and client package references.
The package reference documents the exact R signature.

## Give an agent an R session

[Giving an agent an R session](https://posit-dev.github.io/mcp-console/r/articles/autonomous-report/) uses a scheduled report to walk through explicit settings, sandbox permissions, configuration inspection, and launching an ellmer chat.
It uses `RConfig(executable = current_r())` to select the R installation running the script on every platform.
Open it from an installed package with:

```r
vignette("autonomous-report", package = "mcp.console")
```
