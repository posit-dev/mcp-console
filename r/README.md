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

Native Windows support is experimental and requires a locally installed Console because the release workflow does not publish Windows wheels.
See [Windows installation](https://t-kalinowski.github.io/mcp-console/WINDOWS.html).

## Behavior and safety

The tool uses the connected server's public `send` schema.
Requirement actions are `get`, `add`, `set`, and `reset`; use `character()` for an explicitly empty list.
Changed live replacements normally require restart.
Read [Requirements](https://t-kalinowski.github.io/mcp-console/REQUIREMENTS.html) before replacing a declaration.

By default, `console_tool()` skips global and project configuration files.
Workers can read host files, write private temporary storage, and cannot use the network.
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
Older published binaries may not understand this schema or `--no-config`.

`ConsoleConfig()` combines runtime selection, package preparation, permissions, and configuration discovery.
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

The common launch choices are short:

```r
console_tool() # Built-in defaults; no config files
console_tool(config = ConsoleConfig(sandbox = FALSE))
console_tool(config = ConsoleConfig(discovery = ConfigDiscovery()))
console_tool(
  config = ConsoleConfig(
    discovery = ConfigDiscovery(global = FALSE)
  ),
  project = "/path/to/project"
)
```

Discovery loads global configuration first, then project `.agents/console/config.yaml`, then settings supplied in `ConsoleConfig`.
No ancestors are searched.
`ConfigDiscovery(project = FALSE)` uses global configuration alone.
Setting both discovery flags to `FALSE` reads neither file.
Discovery trusts the whole application configuration, including executable selection, environments, and permissions.

`NULL` fields are omitted and inherit selected file settings before native defaults apply.
An explicit package list replaces the inherited list; `character()` requests no optional startup packages.
Runtime and environment mappings merge recursively.
Python selections replace the whole Python node so managed and existing choices cannot mix.
An explicit `SandboxPolicy()` replaces its entire worker or resolver permission node, removing inherited grants.
`sandbox = FALSE` clears both permission nodes and disables enforcement while retaining other discovered settings.
It cannot be combined with an explicit resolver policy.

`as.list(config)` returns JSON-ready application settings, excluding discovery and the disabled-sandbox launch control.
Those choices remain available as `config@discovery` and `config@sandbox`.
The native CLI owns final validation, defaults, supported paths, and platform capabilities.

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
See `?ToolConfig` and the [tool configuration contract](../docs/CONFIGURATION.md#tool-descriptions-and-parameters).
