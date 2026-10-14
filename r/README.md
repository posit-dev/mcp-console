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

State persists while the tool's server connection remains alive.
For dependent calls through `chat$chat_async()`, select `tool_mode = "sequential"`.

## Select Console

`console_tool()` uses Console on PATH, falling back to the latest published release through `reticulate::uv_run_tool()`.
Named `path` selects an executable; named `version` selects a published release regardless of PATH.
They are mutually exclusive, and `...` must be empty.

```r
console_tool(path = Sys.which("mcp-console"))
```

Starting with Console 0.0.5, published releases include an experimental Windows x64 wheel that `console_tool()` can resolve through `reticulate::uv_run_tool()`.
See [Windows installation](https://t-kalinowski.github.io/mcp-console/WINDOWS.html).

## Behavior and safety

The tool uses the connected server's public `send` schema.
Requirement actions are `get`, `add`, `set`, and `reset`; use `character()` for an explicitly empty list.
Changed live replacements normally require restart.
Read [Requirements](https://t-kalinowski.github.io/mcp-console/REQUIREMENTS.html) before replacing a declaration.

By default, `console_tool()` discovers global and project configuration files.
Pass `config = ConsoleConfig()` to skip them and use built-in defaults: workers can read host files, write private temporary storage, and cannot use the network.
Dependency preparation uses its independent built-in permissions and may download packages.
`ConsoleConfig(sandbox = FALSE)` runs workers and preparation with host permissions, without the native runner's descendant-cleanup guarantee.
Read [Sandbox and trust](https://t-kalinowski.github.io/mcp-console/SANDBOX.html) for default host reads, preparation permissions, and cleanup limits.

Garbage collection requests shutdown by closing server input, waits up to 15 seconds, then may forcibly stop the server process.
That fallback does not prove descendant cleanup.
Keep the tool reachable while the chat is using it.

[Console guides](https://t-kalinowski.github.io/mcp-console/README.html) cover runtime behavior, configuration, input, plots, and recordings.
The package reference documents the exact R signature.

## Session configuration

The development interface uses S7 from `RConsortium/S7`, pinned in `DESCRIPTION`.
Install the declared GitHub dependency with pak/remotes before building the package; `R CMD INSTALL` alone does not process `Remotes`.
During development, use `path=` to select the current checkout's Console binary.
Use a matching Console executable; older published binaries may not support these configuration operations.

`ConsoleConfig()` combines runtime selection, package preparation, permissions, and tool presentation.
It reads no configuration files.
The class constructors have explicit named arguments matching the application schema.

```r
config <- ConsoleConfig(
  r = RConfig(
    executable = "/opt/R/bin/R",
    packages = c("dplyr", "ggplot2"),
    resolution = "startup_only"
  ),
  python = ManagedPython(
    version = "3.13",
    packages = "pandas",
    resolution = "explicit"
  ),
  sandbox = SandboxPolicy(
    filesystem = SandboxFilesystem(read_write = "./output", deny = "./secrets"),
    network = SandboxNetwork(
      proxy = SandboxProxy(domains = SandboxDomains(allow = "api.example.com"))
    )
  ),
  resolver = ResolverConfig(
    environment = c(UV_INDEX_URL = "https://pypi.org/simple")
  )
)

tool <- console_tool(config = config, project = "/path/to/project")
```

`project` sets the child's working directory without changing the caller's directory.
Relative paths use that launch directory.
`ExistingPython(".venv")` selects a preinstalled interpreter or standard virtual environment; it accepts no preparation options.
`ManagedPython()` prepares a uv-managed environment.
R and managed Python independently support `automatic`, `explicit`, and `startup_only` resolution.
R also supports `disabled` for preinstalled packages.
Worker restart retains the captured runtime and configuration.
See `?RConfig`, `?ExistingPython`, and `?ResolverConfig`.

The launch choices make file discovery explicit:

```r
console_tool() # Discover global and project files, materialize, then launch
console_tool(config = ConsoleConfig()) # Built-in defaults, no config files
console_tool(config = ConsoleConfig(sandbox = FALSE)) # Host execution
```

**Default change:** `console_tool()` now discovers configuration files.
To retain the previous behavior, pass `config = ConsoleConfig()`.
Discovery trusts the whole application configuration, including executable selection, environments, and permissions.

## Discover configuration once

Read the global and project files into an R object before inspecting or modifying them:

```r
config <- discover_console_config(project = "/path/to/project")
print(prune_console_config(config, project = "/path/to/project"))
tool <- console_tool(config = config, project = "/path/to/project")
```

Discovery reads `config.yaml` in `MCP_CONSOLE_HOME` (otherwise `~/.agents/console/config.yaml`), then `.agents/console/config.yaml` in the launch directory.
It does not search ancestors.
Both paths are optional: a missing file is skipped, but a malformed or unreadable file fails.
The result is an ordinary, fully materialized S7 `ConsoleConfig` with file discovery disabled.
Changing the source files afterward does not change this object or its launch.

Choose the sources directly, or set either to `NULL`:

```r
config <- discover_console_config(
  global_file = NULL,
  project_file = "settings/report.yaml",
  config = ConsoleConfig(inherit_environment = FALSE),
  project = "/path/to/project"
)

# Require one selected file. JSON and YAML use the same configuration schema.
config <- read_console_config("report.json", project = "/path/to/project")
```

`read_console_config()` fails if its file is missing and reads no other files.
Relative paths, including paths inside configuration files, use `project`.
In either reader, `config` overrides the files: mappings merge recursively, package lists replace inherited lists, and explicit Python selections and sandbox policies replace their entire nodes.
An empty package vector means no optional startup packages.
`sandbox = FALSE` clears worker and resolver permissions and disables enforcement; it cannot be combined with an explicit resolver policy.

## Inspect and edit configuration

Materialization and pruning are two views of the same class:

| Operation                            | Result                                                                       |
| ------------------------------------ | ---------------------------------------------------------------------------- |
| `ConsoleConfig(...)`                 | Only the settings supplied in R; no file reads                               |
| `materialize_console_config(config)` | Settings plus native defaults, complete tool prose, and signature metadata   |
| `prune_console_config(config)`       | Settings that differ from native defaults, retaining meaningful empty values |
| `discover_console_config(...)`       | Optional file layers captured and materialized once                          |
| `read_console_config(file)`          | One required JSON or YAML file, materialized without discovery               |

Omitting `config` from materialization or pruning starts with `ConsoleConfig()` and reads no files.
The selected executable supplies the defaults and validates the result without starting interpreters or installing packages.
Pruning is useful for reviewing changes; omissions follow the defaults of the binary used on the next launch.
Use the same `path` or `version` and `project` when comparing, editing, and launching configurations.

```r
config <- materialize_console_config(ConsoleConfig(languages = "r"))
print(config) # Full contents, including untruncated descriptions
config@tool@description <- "Run one R cell in a persistent workbench."
config@tool@parameters$timeout_ms@enabled <- FALSE
config@tool@parameters$r@description <- "Evaluate a complete R expression."
print(prune_console_config(config))
tool <- console_tool(config = config)
```

Use `@` for S7 properties and `$` for entries in the named `parameters` list.
The parameter `schema` contains read-only types and constraints; native validation rejects schema edits.
Use `enabled = FALSE` to hide a field; assigning `NULL` removes an override and restores its default.
Materialized descriptions are literal overrides: changing visibility, languages, or permissions does not regenerate saved prose.
Clear the affected description with `NULL` before materializing again to regenerate it.

Compare or save the application settings with ordinary R tools:

```r
edited_config <- config
edited_config@tool@parameters$timeout_ms@enabled <- TRUE
before <- prune_console_config(config)
after <- prune_console_config(edited_config)
all.equal(as.list(before), as.list(after))

writeLines(
  as.character(jsonlite::toJSON(
    as.list(after),
    auto_unbox = TRUE,
    pretty = TRUE
  )),
  "report.json"
)
restored <- read_console_config("report.json")
```

`as.list()` excludes launch-only controls: legacy `discovery` and `sandbox = FALSE`.
For host execution, retain the S7 object (for example with `saveRDS()`) or pass `config = ConsoleConfig(sandbox = FALSE)` when reading JSON.
Runtime-dependent interpreter selection, automatic package lists, and runtime-owned cache paths remain policies.
Materialization does not lock dependencies or copy inherited environment variables.

`config@tool@instructions` is server guidance returned during MCP initialization, which some clients use for deferred tool discovery.
`config@tool@description` and parameter descriptions arrive through `tools/list`.
The ellmer adapter uses the latter tool definition and does not automatically insert server instructions into the chat prompt.
See [tool presentation and lifecycle](https://t-kalinowski.github.io/mcp-console/CONFIGURATION.html#tool-descriptions-and-parameters).

## Give an agent an R session

[Giving an agent an R session](https://t-kalinowski.github.io/mcp-console/r/articles/autonomous-report.html) uses a scheduled report to walk through explicit settings, sandbox permissions, configuration inspection, and launching an ellmer chat.
It uses `RConfig(executable = current_r())` to select the R installation running the script on every platform.
Open it from an installed package with:

```r
vignette("autonomous-report", package = "mcp.console")
```

## Existing callers

`resolve_console_config()` remains an alias for full materialization.
Its result now uses S7 properties instead of the earlier list result, and executable/project choices remain explicit arguments:

```r
config <- materialize_console_config(path = binary, project = project)
config@tool@description <- "Run a report cell."
tool <- console_tool(config = config, path = binary, project = project)
```

`ConsoleConfig(discovery = ConfigDiscovery(...))` remains supported for existing code that explicitly requests discovery at launch.
For new code, use `discover_console_config(global_file = ..., project_file = ...)` to make that file read visible before launch.
Explicit `config = NULL` retains its old meaning of `ConsoleConfig()`; omitting `config` uses discovery.
Constructed, discovered, materialized, and pruned objects share validation and temporary-file transport, so long descriptions and startup programs do not consume command-line space.

Python fallback policies and SQL startup source use the same typed constructors when supplied directly or returned by materialization:

```r
ConsoleConfig(
  python = FirstAvailablePython(list(
    ExistingPython(".venv"),
    "active_venv",
    ManagedPython(packages = character())
  )),
  startup = StartupConfig(
    language = "python",
    code = "import sqlite3; _console.sql_connection(sqlite3.connect(':memory:'))"
  )
)
```

## Sandbox policies

A `SandboxPolicy()` can configure worker or resolver permissions.
Its nested classes are `SandboxFilesystem`, `SandboxNetwork`, `SandboxProxy`, `SandboxDomains`, and `SandboxSockets`.
An empty worker policy grants no workspace writes or networking.
This is a host-read/private-write baseline, not filesystem secrecy: sensitive readable locations need explicit denial.
Create required output directories on the host before granting them; R does not create or normalize configured paths.

```r
policy <- SandboxPolicy(filesystem = SandboxFilesystem(read_write = "./output"))
tool <- console_tool(config = ConsoleConfig(sandbox = policy))
```

`NULL` policy properties are omitted, `SandboxFilesystem()` is an explicit empty mapping, and `read_write = character()` is an explicit empty sequence.
Serialization preserves these distinctions.
Worker and resolver policies are independent; an omitted resolver filesystem retains its native cache grants, while an explicit mapping replaces them.
Explicit resolver sandbox policies are unsupported on Windows, where preparation runs with host permissions.

See `?ConsoleConfig` and `?SandboxPolicy`.

Use `ToolConfig()` to replace descriptions or hide parameters for evaluations:

```r
console_tool(
  config = ConsoleConfig(
    tool = ToolConfig(
      description = "Run R code in a persistent session.",
      parameters = list(
        r = ToolParameterConfig(description = "Execute one complete R cell."),
        timeout_ms = ToolParameterConfig(enabled = FALSE),
        requirements = ToolParameterConfig(
          parameters = list(
            r = ToolParameterConfig(description = "R package requirements.")
          )
        )
      )
    )
  )
)
```

Descriptions replace the complete selected text, including empty strings.
`instructions` independently replaces MCP initialization instructions.
Hidden parameters are removed from the signature and rejected if supplied; omission retains their runtime defaults.
Hiding `timeout_ms` retains the 60,000 ms wait budget.
See `?ToolConfig` and the [tool configuration contract](https://t-kalinowski.github.io/mcp-console/CONFIGURATION.html#tool-descriptions-and-parameters).
