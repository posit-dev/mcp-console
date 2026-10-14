# Command-line reference

Run `mcp-console --help` or a subcommand's `--help` for the installed build's complete option list.
The website's [CLI help](cli-help.qmd) shows the printouts checked by the CLI tests.
Console runs commands on the local host; it does not create remote connections.

## MCP server

```sh
mcp-console serve
mcp-console serve --writable-root .
mcp-console serve --no-project-config
```

`serve` speaks MCP over stdin and stdout.
It waits for a client; it is not an interactive terminal.
Keep stdout available for protocol traffic.

Sandboxing is enabled by default.
`--writable-root PATH` adds a writable path for this launch and may be repeated.
More specific permission rules still apply.
It does not create missing directories.

```sh
mcp-console serve --no-sandbox
```

`--no-sandbox` runs evaluated code and dependency preparation with host permissions and removes native descendant-cleanup guarantees.
It conflicts with writable-root options and explicit worker or resolver sandbox settings.
Host cache mode is used; `cache: console` is rejected.

## Standalone sandbox

```sh
mcp-console sandbox -- Rscript analysis.R
mcp-console sandbox --writable-root . -- python script.py
mcp-console sandbox -c sandbox.network=enabled -- python download.py
```

Arguments after `--` belong to the target command.
Stdin also belongs to the target.
This command applies the sandbox and workload environment; it does not provide Console's persistent session, automatic package preparation, plot capture, or recordings.

The default permits host reads and private temporary writes, not workspace writes.
Network access and cleanup limits are described in [Sandbox and trust](SANDBOX.md).

## Configuration options

These options work before or after `serve` and ordinary `sandbox`:

| Option                               | Configuration sources                                           |
| ------------------------------------ | --------------------------------------------------------------- |
| No selection flags                   | Global file, launch-directory project file, then CLI overrides. |
| `--no-project-config`                | Global file and CLI overrides.                                  |
| `--no-global-config`                 | Project file and CLI overrides.                                 |
| `--no-config`                        | CLI overrides only.                                             |
| `--config-file PATH`                 | One explicit YAML or JSON file, then CLI overrides.             |
| `-c KEY=VALUE`, `--config KEY=VALUE` | Apply one dotted assignment; repeat in the desired order.       |

`--config-file` may occur once and cannot be combined with discovery exclusions.
A missing explicit file is an error.
`-c` accepts an assignment, not a file path or a complete root document.

```sh
mcp-console serve --config-file ./session.yaml
mcp-console serve -c 'languages=[python,sql]' -c 'python=.venv'
mcp-console -c 'environment={OMP_NUM_THREADS: "4"}' serve
```

Examples use POSIX shell quoting.
In PowerShell, quote the complete assignment when it contains spaces or structured values.
[Configuration](CONFIGURATION.md) defines paths, merging, and trust.

## Inspect configuration

```sh
mcp-console config resolve > session.yaml
mcp-console config resolve --full --no-config --format json
mcp-console serve --config-file session.yaml
```

`config resolve` defaults to `--prune`, printing only settings that differ from the binary's built-in defaults.
`--full` includes all configuration defaults, generated tool prose, and every parameter setting.
The two flags are mutually exclusive.
Neither mode starts runtimes or prepares packages.
Configuration selection flags and `-c` work before `config` or after `resolve`.
YAML is the default output; `--format json` selects JSON.
For host execution, use `--no-sandbox` during both materialization and launch.
See [inspect and edit resolved configuration](CONFIGURATION.md#inspect-and-edit-resolved-configuration) for supported edits and runtime-dependent defaults.

## Windows sandbox requirements

Native Windows sandboxed launches require MXC, including complete native policies.
Unsupported hosts or policies fail before the workload executes.
MXC needs no provisioning command; existing legacy installations need the [one-time cleanup](WINDOWS.md#legacy-installation-cleanup).
See [Windows](WINDOWS.md#native-sandbox).

## Complete native policy

`mcp-console sandbox --config-env NAME -- COMMAND...` reads a complete native JSON policy from the named launch environment variable.
It bypasses automatic configuration and Console's generated policy defaults; it does not merge with `-c` or writable-root options.

This is an advanced integration interface.
Use the pinned schema and examples linked from [Explicit complete policy](SANDBOX_CONFIGURATION.md#explicit-complete-policy), rather than adapting a `config.yaml` document into it.

## Developer-only commands

`worker`, `worker-relay`, and `resolve`, along with `serve --worker`, `serve --relay`, and private handoff options, are not normal user workflows.
Their contracts live in the [architecture](ARCHITECTURE.md) and protocol guides.
Do not launch them to bypass the public configuration or sandbox boundary.
