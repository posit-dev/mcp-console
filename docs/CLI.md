# Command-line reference

Use `serve` to connect an MCP client, `sandbox` to run one command under a sandbox, and `config resolve` to inspect configuration.
Console runs on the local host; it does not create remote connections.

The [CLI help page](cli-help.qmd) shows tested public-command help.
Run `mcp-console --help` for the version installed on your machine.

## MCP server

```sh
mcp-console serve
```

Your MCP client normally launches this command.
It communicates over standard input and output and waits for the client; it is not an interactive terminal.
See [Connect your client](getting-started.qmd#connect-your-client).

For ordinary project writes, create `.agents/console/config.yaml` in the launch directory:

```yaml
sandbox:
  profile: workspace
```

The [workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile) allows project writes while keeping workspace metadata read-only.
It retains the default private temporary storage and restricted networking.

To add a writable path through the client command instead:

```sh
mcp-console serve --writable-root .
```

`--writable-root PATH` may be repeated.
It adds filesystem grants without selecting the workspace profile, does not create missing directories, and remains subject to more specific permission rules.
[Sandbox configuration](SANDBOX_CONFIGURATION.md) documents path rules and protected metadata.

## Standalone sandbox

```sh
mcp-console sandbox -- Rscript analysis.R
mcp-console sandbox --writable-root . -- python script.py
mcp-console sandbox -c sandbox.network=enabled -- python download.py
```

Arguments after `--` and standard input belong to the target command.
This applies the sandbox and workload environment, but does not provide a persistent Console session, automatic package preparation, plot capture, or recordings.

The default permits host-file reads and private temporary writes, not workspace writes.
See [Sandbox and trust](SANDBOX.md) for networking and cleanup limits.

## Configuration options

These options work before or after `serve` and ordinary `sandbox`:

| Option                               | Configuration sources                                           |
| ------------------------------------ | --------------------------------------------------------------- |
| No selection flags                   | Global file, launch-directory project file, then CLI overrides. |
| `--no-project-config`                | Global file and CLI overrides.                                  |
| `--no-global-config`                 | Project file and CLI overrides.                                 |
| `--no-config`                        | CLI overrides only.                                             |
| `--config-file PATH`                 | One explicit YAML or JSON file, then CLI overrides.             |
| `-c KEY=VALUE`, `--config KEY=VALUE` | One dotted assignment; repeat to apply several in order.        |

An explicit configuration file is required to exist.
`--config-file` may occur once and cannot be combined with discovery exclusions.
`-c` accepts an assignment, not a file path or a complete root document.

```sh
mcp-console serve --config-file ./session.yaml
mcp-console serve --no-project-config
mcp-console serve -c 'languages=[python,sql]' -c 'python=.venv'
```

Examples use POSIX shell quoting.
In PowerShell, quote the complete assignment when it contains spaces or structured values.

Project configuration is trusted input and can widen permissions.
Use `--no-project-config` for an unfamiliar project until you have reviewed its settings.
[Configuration](CONFIGURATION.md) defines paths, precedence, and merging.

## Inspect configuration

```sh
mcp-console config resolve > session.yaml
mcp-console config resolve --full --no-config --format json
mcp-console serve --config-file session.yaml
```

`config resolve` prints settings without starting runtimes or preparing packages.
By default it uses `--prune`, omitting settings equal to the binary's built-in defaults.
`--full` includes defaults, tool descriptions, and parameter settings; it cannot be combined with `--prune`.

YAML is the default output.
Use `--format json` for JSON.
Configuration selection flags and `-c` work before `config` or after `resolve`.
Keep the same launch directory when exporting and using relative paths.

See [Inspect and edit resolved configuration](CONFIGURATION.md#inspect-and-edit-resolved-configuration) for editable fields and runtime-dependent defaults.

## Windows sandbox requirements

Native Windows sandboxed launches require MXC, including complete native policies.
Unsupported hosts and policies fail before the workload executes; there is no automatic weaker fallback.

MXC needs no provisioning command.
See [Windows setup](WINDOWS.md#native-sandbox).

## Run without sandboxing

```sh
mcp-console serve --no-sandbox
```

This runs evaluated code and dependency preparation with host permissions and removes native descendant-cleanup guarantees.
It also disables [managed temporary storage](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage).
It conflicts with writable-root options and explicit analysis or resolver sandbox settings.
It uses host cache mode and rejects `cache: console`.

For a configuration exported for host execution, pass `--no-sandbox` to both `config resolve` and `serve`; the launch flag is not stored in YAML.
See [Cache selection](CONFIGURATION.md#cache-selection) for clearing inherited permission settings deliberately.

## Complete native policy

Advanced standalone callers can use:

```sh
mcp-console sandbox --config-env POLICY_VARIABLE -- COMMAND
```

The named launch environment variable holds a complete native JSON policy, not a filename.
This bypasses automatic application configuration and Console's generated policy defaults; it does not merge with configuration-selection flags, `-c`, or writable-root options.

This is a separate advanced interface, not another spelling of `config.yaml`.
See [Explicit complete policy](SANDBOX_CONFIGURATION.md#explicit-complete-policy) for the pinned schema and runnable examples.

<span id="developer-only-commands"></span>

Contributor-only command contracts are documented in [Architecture](ARCHITECTURE.md) and the internal protocol references.
