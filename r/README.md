# mcp.console

`mcp.console` adds Console's persistent R, Python, and SQL workbench to an [ellmer](https://ellmer.tidyverse.org/) chat.

```r
pak::pak("github::t-kalinowski/mcp-console/r")

library(ellmer)
library(mcp.console)

chat <- chat_openai()
chat$register_tool(console_tool())
chat$chat("Explore mtcars with the console. Keep the data for follow-up.")
```

The selected ellmer provider's credentials belong in the calling R process.
State persists while the tool's server connection remains alive.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

The development interface requires the S7 version pinned in `DESCRIPTION`.
Install its declared GitHub dependency with pak/remotes; `R CMD INSTALL` alone does not process `Remotes`.
Use a matching Console executable for development configuration features.

With ellmer 0.5.0 and the pinned development S7, loading ellmer can warn that it is replacing the import `S7:::=` with `rlang:::=`.
This is an upstream namespace collision, reproducible with `loadNamespace("ellmer")` alone; it does not indicate a Console startup failure.
The dependency follow-up is tracked in [Open work](https://t-kalinowski.github.io/mcp-console/todo/#r-client-dependencies).

## Select Console

`console_tool()` uses Console on PATH, falling back to the latest published release through `reticulate::uv_run_tool()`.
Named `path` selects an executable; named `version` selects a published release regardless of PATH.
They are mutually exclusive, and `...` must be empty.

```r
console_tool(path = Sys.which("mcp-console"))
```

On Windows, `console_tool()` can resolve the Console x64 wheel through `reticulate::uv_run_tool()`.
See [installation requirements](https://t-kalinowski.github.io/mcp-console/clients/#supported-platforms).

## Session configuration

Pass an explicit configuration to select a project's existing Python environment:

```r
config <- ConsoleConfig(python = ExistingPython(".venv"))
tool <- console_tool(config = config, project = "/path/to/project")
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
tool <- console_tool(config = config, project = "/path/to/project")
```

Network permissions remain independent.
Use `SandboxFilesystem(read_write = "./output")` for a narrower write grant and create that directory before launching.
See [SandboxPolicy](https://t-kalinowski.github.io/mcp-console/r/reference/sandboxpolicy/) for temporary storage, path rules, and platform limits.

<span id="discover-configuration-once"></span> <span id="inspect-and-edit-configuration"></span> <span id="existing-callers"></span>

[Configure a Console session](https://t-kalinowski.github.io/mcp-console/r/articles/configuration/) covers discovery, inspection, editing tool descriptions, saved settings, and launching a configured session.
The [configuration workflow reference](https://t-kalinowski.github.io/mcp-console/r/reference/config-workflow/) documents exact signatures.

## Behavior and safety

The tool uses the connected server's public `send` schema.
Requirement actions are `get`, `add`, `set`, and `reset`; use `character()` for an explicitly empty list.
Changed live replacements normally require restart.
Read [Requirements](https://t-kalinowski.github.io/mcp-console/api/#requirements) before replacing a declaration.

By default, `console_tool()` discovers global and project configuration files.
Pass `config = ConsoleConfig()` to skip them and use built-in defaults: workers can read host files, write private temporary storage, and cannot use the network.
Dependency preparation uses its independent built-in permissions and may download packages.
`ConsoleConfig(sandbox = FALSE)` runs workers and preparation with host permissions, without the native runner's descendant-cleanup guarantee.
Read [Sandbox and trust](https://t-kalinowski.github.io/mcp-console/sandbox/) for default host reads, preparation permissions, and cleanup limits.

Garbage collection requests shutdown by closing server input, waits up to 15 seconds, then may forcibly stop the server process.
That fallback does not prove descendant cleanup.
Keep the tool reachable while the chat is using it.

[Console documentation](https://t-kalinowski.github.io/mcp-console/readme/) covers the shared workbench, language behavior, and configuration.
[API reference](https://t-kalinowski.github.io/mcp-console/reference/) links the MCP interface and client package references.
The package reference documents the exact R signature.

## Give an agent an R session

[Giving an agent an R session](https://t-kalinowski.github.io/mcp-console/r/articles/autonomous-report/) uses a scheduled report to walk through explicit settings, sandbox permissions, configuration inspection, and launching an ellmer chat.
It uses `RConfig(executable = current_r())` to select the R installation running the script on every platform.
Open it from an installed package with:

```r
vignette("autonomous-report", package = "mcp.console")
```
