# Command-line reference

Use `serve` to run the MCP server, `sandbox` to run one command under a sandbox, `config resolve` to inspect configuration, and `configure-client` to register Console with a client.
Console runs on the local host; it does not create remote connections.

The [CLI help page](cli-help.qmd) shows tested public-command help.
Run `mcp-console --help` for the version installed on your machine.

## Configure a client

```sh
uvx mcp-console configure-client codex claude
mcp-console configure-client codex --scope global
mcp-console configure-client claude --scope project --dry-run
mcp-console configure-client claude --no-auto-load
mcp-console configure-client codex --pin-version current
mcp-console configure-client codex --pin-version v0.0.5
mcp-console configure-client claude --no-uvx
mcp-console configure-client codex --name console-local -- --writable-root /data
```

`configure-client` registers a stdio MCP server named `console` that launches `uvx mcp-console serve`.
Pass one or more client names; all options apply to every selected client, and repeated client names are configured once.
Use separate invocations for different settings per client.
`--name NAME` changes the server name; arguments after `--` are appended after `serve`.
Codex server names must match `^[a-zA-Z0-9_:@/.-]+$`; other names are rejected before installation or configuration changes.
Its built-in `codex_apps` name is also reserved and rejected before changes.
When Claude Code is selected, its reserved names (`workspace`, `claude-in-chrome`, `computer-use`, `Claude Preview`, and `Claude Browser`) are rejected before configuration changes.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) so `uvx` is on the client's `PATH`.
The client itself does not need to be installed when writing its configuration.

The default uvx command has no version suffix.
`--pin-version VERSION` appends a Console version suffix after `@`, for example `mcp-console@v0.0.5` or `mcp-console@latest`.
`--pin-version current` saves the concrete version running setup, so later launches stay on that version.
Exact versions use PEP 440 syntax and are validated before configuration changes; `latest` and `current` are also accepted, while version ranges and invalid suffixes are rejected.
Package availability is checked by uv when the client launches the server.
An unqualified uvx command may reuse an installed or cached version; `latest` requests the latest available release at launch.

`--no-uvx` configures an absolute path to the running executable instead.
Invoke the desired installation to select it, for example `/opt/mcp-console/bin/mcp-console configure-client claude --no-uvx`.
When the executable is in a uv-created environment inside a disposable cache, the command uses `uv pip freeze --python ENVIRONMENT` to identify Console's recorded direct source, then runs `uv tool install REQUIREMENT` and configures the absolute launcher path reported by `uv tool dir --bin`.
Only uv's disposable cache layout triggers installation; persistent uv tools, Cargo builds, and non-uv environments retain their executable paths even beneath unrelated tagged caches.
Direct wheel, archive, and Git installations retain their recorded source, including recorded hashes and resolved Git commits.
Automatic persistent installation requires uv 0.11.29 or newer, which preserves recorded archive hashes in `uv pip freeze` output.
Directory sources are rebuilt from their recorded path.
Keep that checkout unchanged during setup; the resulting executable may differ from the temporary build.
This includes temporary `uvx --no-cache` runs and custom uv cache locations.
Temporary index and `--find-links` installations record only a version, so setup rejects them before installation or configuration changes rather than resolving through a potentially different source.
Install persistently with `uv tool install` using the same resolver options first, then invoke the installed executable's `configure-client --no-uvx` command.
If source metadata for a temporary uv environment cannot be identified, setup fails before installation or configuration changes; install the desired source persistently with `uv tool install SOURCE`, then run its `configure-client --no-uvx` command.
The persistent installation requires `uv` during setup; subsequent client launches use the saved executable directly.
The command prints the installation command and executable path before installing.
The recorded source's URL query is redacted from that report and captured uv output; the original requirement is passed to uv unchanged.
It leaves existing-tool conflicts to uv and does not force replacement of executable files.
`--no-uvx` conflicts with `--pin-version`; select the desired version before configuring a direct executable.

The default `--scope auto` uses the nearest ancestor with a `.git` directory or worktree marker file.
Outside Git, it selects global scope.
`--scope project` uses that Git root, or the current directory outside Git.
`--scope global` selects global scope regardless of the current directory.

| Client      | Project configuration | Global configuration                                        |
| ----------- | --------------------- | ----------------------------------------------------------- |
| Codex       | `.codex/config.toml`  | `$CODEX_HOME/config.toml`, default `~/.codex/config.toml`   |
| Claude Code | `.mcp.json`           | `$CLAUDE_CONFIG_DIR/.claude.json`, default `~/.claude.json` |

The command prints the selected scope, file path, and fields it sets.
Use `--dry-run` to preview those settings and any persistent installation without writing files or installing, or to apply them manually.
Existing settings outside the printed fields are preserved, including other servers, environment variables, and client preferences.
TOML key comments and comments around replaced values are preserved.
Comments inside the managed `args` and `omit_tools_from` arrays are discarded when those arrays are replaced.
JSON is formatted on write.
Malformed configuration and an existing remote server with the selected name cause an error without overwriting the file.
Every selected client's configuration is parsed before installation or writes begin.
Existing files are updated in place to retain file identity, symlinks, ownership, and access-control lists, including Windows DACLs.
These updates are not atomic: an interrupted or failed write may leave an incomplete file.
New files are created atomically.
A later file failure can leave earlier files changed, and their completed actions remain in stdout.
Configuration edits assume no concurrent client or setup writer.

By default, configuration also requests that Console tools be visible without a tool search:

- Codex: `enabled = true` and `omit_tools_from = ["deferred"]` in `[mcp_servers.console]`.
- Claude Code: [`alwaysLoad: true`](https://code.claude.com/docs/en/mcp#exempt-a-server-from-deferral) in `mcpServers.console`.

`--no-auto-load` writes `omit_tools_from = []` for Codex or `alwaysLoad: false` for Claude Code, leaving tool discovery to the client.
These settings affect only Console.
Client startup limits still apply; a cold `uvx` launch can delay tool availability.
Codex's Strict Code Mode Only controls tool exposure independently and overrides the visibility setting.
Use a current client version that supports the respective setting.

Restart the client after configuration.
Codex requires project trust before loading project configuration, and Claude Code may ask you to approve a project server.
Configuration leaves these client approvals and tool permissions unchanged.
Legacy Claude Code `.config.json` layouts are not supported by this command.

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
