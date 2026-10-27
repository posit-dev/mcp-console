# Command-line reference

Use `kernel` to run a [single-language Jupyter kernel](JUPYTER.md), `serve` to run the MCP server, `warmup` to check the runtimes and warm caches, `sandbox` to run one command under a sandbox, `config init` to create shared settings, `config resolve` to inspect configuration, and `configure-client` to register Console with a client.
Console runs on the local host; it does not create remote connections.

See [Install and connect](CLIENTS.md) for supported hosts and runtime setup.
The [CLI help page](cli-help.qmd) shows tested public-command help.
Run `mcp-console --help` for the version installed on your machine.

## Configure a client

```sh
uvx mcp-console configure-client codex claude
mcp-console configure-client codex
mcp-console configure-client claude --directory . --dry-run
mcp-console configure-client claude --directory /path/to/project
mcp-console configure-client claude --no-auto-load
mcp-console configure-client claude --no-warmup
mcp-console configure-client codex --pin-version current
mcp-console configure-client codex --pin-version v0.0.5
mcp-console configure-client claude --no-uvx
mcp-console configure-client codex --directory . --name console-local -- --writable-root /data
```

`configure-client` registers a stdio MCP server named `console` that launches `uvx mcp-console serve`.
Pass one or more client names; all options apply to every selected client, and repeated client names are configured once.
Use separate invocations for different settings per client.
`--name NAME` changes the server name; arguments after `--` are appended after `serve`.
Each call replaces that entry's managed launch arguments, so include every server option you want to retain and restart the client afterward.
Codex server names must match `^[a-zA-Z0-9_:@/.-]+$`; other names are rejected before installation or configuration changes.
Its built-in `codex_apps` name is also reserved and rejected before changes.
When Claude Code is selected, its reserved names (`workspace`, `claude-in-chrome`, `computer-use`, `Claude Preview`, and `Claude Browser`) are rejected before configuration changes.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) so `uvx` is on the client's `PATH`.
The client itself does not need to be installed when writing its configuration.

After registration, setup starts one background `warmup` and prints its PID and log path.
It returns without waiting for runtime checks or package preparation.
Use `--no-warmup` to skip this step; `--dry-run` previews it without starting a job.
Logs remain in `logs/warmup-*.log` under [Console home](ENVIRONMENT.md#server-paths-and-executable-discovery), normally `~/.agents/console` or the directory selected by `MCP_CONSOLE_HOME`.

The configuring executable runs the warmup checks against the first selected client's `serve` command, preserving its version and launch arguments.
Pinned releases can predate the `warmup` subcommand.
It runs in `--directory PATH` when supplied, otherwise the current directory, and uses the [standalone warmup permissions](#check-your-installation).
Later client sessions may start in other directories or inherit different permissions.
Warmup errors appear in the log and do not undo registration; a failure to launch prints a warning.

Codex launches include `--default-config sandbox.profile=inherit_codex` to [inherit the client's sandbox permissions](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex) when configuration files do not select a profile.
This applies to uvx and `--no-uvx` launches.
Global and project configuration take precedence over this client default.
Arguments after `--` customize the launch; use `-c` to override configuration files too, for example:

```sh
mcp-console configure-client codex --directory . -- -c sandbox.profile=workspace
```

To pass `--no-sandbox`, also clear the generated policy with `-c sandbox=null`.

The default uvx command has no version suffix.
`--pin-version VERSION` appends a Console version suffix after `@`, for example `mcp-console@v0.0.5` or `mcp-console@latest`.
`--pin-version current` saves the concrete version running setup, so later launches stay on that version.
Exact versions use PEP 440 syntax and are validated before configuration changes; `latest` and `current` are also accepted, while version ranges and invalid suffixes are rejected.
Package availability is checked by uv when warmup or the client launches Console.
For Codex, pinned versions must support `--default-config` and `sandbox.profile=inherit_codex`.
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

Global scope is the default, including inside a Git repository.
Use `--directory PATH` to write project configuration in an existing directory, or `--directory .` for the current directory.
Relative paths start at the current directory; setup does not search Git parents or set the client's launch directory.

| Client      | Project configuration | Global configuration                                        |
| ----------- | --------------------- | ----------------------------------------------------------- |
| Codex       | `.codex/config.toml`  | `$CODEX_HOME/config.toml`, default `~/.codex/config.toml`   |
| Claude Code | `.mcp.json`           | `$CLAUDE_CONFIG_DIR/.claude.json`, default `~/.claude.json` |

Claude Code tool permissions are written separately to `.claude/settings.json` in the selected project, or `$CLAUDE_CONFIG_DIR/settings.json` globally (default `~/.claude/settings.json`).

[Codex](https://learn.chatgpt.com/docs/config-file/config-basic#configuration-precedence) and [Claude Code](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence) give loaded project entries precedence over same-named global entries.
Global registration leaves existing project entries unchanged and warns when it finds one that may take precedence, including during `--dry-run`.
The warning identifies the nearest matching project file.
To update an existing project entry, rerun setup with `--directory PATH` for that project directory.
To use global settings instead, remove only that server's entry from the project configuration listed above.

The check searches from the current directory through Codex's project root, using `project_root_markers` from its user configuration or `.git` by default; without a matching marker it checks only the current directory.
For Claude Code, it checks ancestor `.mcp.json` files, including above Git roots.
It does not evaluate client trust, approvals, profiles, managed settings, or Claude Code's private local entries.
An unreadable or malformed project file encountered by this check causes an error before installation or writes.

The command prints the selected scope, file path, and fields it sets.
Use `--dry-run` to preview settings, any persistent installation, and background warmup without writing files, installing, or starting a job.
You can also apply the printed settings manually.
Existing settings outside the printed fields are preserved, including other servers, environment variables, and client preferences.
TOML key comments and comments around replaced values are preserved.
Comments inside the managed `args` and `omit_tools_from` arrays are discarded when those arrays are replaced.
JSON is formatted on write.
Malformed configuration and an existing remote server with the selected name cause an error without overwriting the file.
Every selected client's configuration, including Claude Code's permission settings, is parsed before installation or writes begin.
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

Configuration also allows Console's `send` tool to run without a per-call approval prompt:

- Codex: [`approval_mode = "approve"`](https://learn.chatgpt.com/docs/config-file/config-reference) in `[mcp_servers.console.tools.send]`.
- Claude Code: [`"mcp__console__send"`](https://code.claude.com/docs/en/permissions#mcp) in `permissions.allow` in its settings file.

`--name` selects the server for both settings; Claude Code's permission rule uses the server's normalized tool-name spelling.
Existing Claude Code allow rules are retained without adding duplicate entries.
`--no-auto-load` affects discovery only; it still configures approval for `send`.
Console's sandbox permissions are unchanged by the tool allowlist.
Explicit Claude Code `ask` or `deny` rules and managed client restrictions can still require approval or block a call.

Restart the client after configuration.
Codex requires project trust before loading project configuration, and Claude Code may ask you to approve a project server.
Configuration leaves those project trust decisions unchanged.
Legacy Claude Code `.config.json` layouts are not supported by this command.

For other MCP clients, use the [stdio configuration example](CLIENTS.md#other-mcp-clients).

## Check your installation

```sh
uvx mcp-console warmup
mcp-console warmup --config-file ./session.yaml
mcp-console warmup -c 'languages=[python,sql]'
```

`warmup` starts a server, initializes an MCP connection, discovers its tools, and evaluates a small check in each enabled language before shutting down.
It prepares the configured startup packages and reuses the same dependency caches as `serve`, so a later session with the same settings can reuse them.
The first run may download Python, packages, and DuckDB extensions.

Dependency discovery, downloads, installation, and builds run with host permissions during warmup, including the background warmup started by `configure-client`.
The resolver keeps the environment and cache locations selected for a normal session, while its filesystem and network sandbox restrictions are not enforced.
This trusted installation step lets package tools complete setup that the resolver sandbox might otherwise deny.
Run warmup only with trusted configuration and dependencies.
The runtime checks still use the configured worker sandbox.

Run it from the directory where your client launches Console, with the same configuration and launch options.
Configuration selection, ordered `-c` overrides, `--no-sandbox`, and `--writable-root` work as they do for `serve`.
`--no-sandbox` also removes the worker sandbox and selects host caches by default.
When using the `inherit_codex` profile, this standalone connection has no client permissions to inherit and uses the configured fallback.
The check runs with your shell's environment; a client launched with a different environment may select different runtimes.

Each language reports `OK`, `FAIL`, or `SKIP`.
If the server closes its connection before initialization completes, warmup reports the closed connection and exit status while preserving the server's diagnostics on stderr.
Disabled languages report `SKIP (disabled)`; R reports `SKIP (not installed)` when runtime discovery confirms it is absent, while Python and SQL are still checked.
An explicitly selected or discovered but broken R installation remains a failure, as do package preparation and runtime errors.
Warmup prints diagnostics, continues after completed language failures, and exits with status 1 if a check or server shutdown fails.
`--timeout SECONDS` limits initialization and each language check to 600 seconds by default.
A timeout or interactive input request stops further checks and shuts down the server.

## MCP server

```sh
mcp-console serve
```

Your MCP client normally launches this command.
It communicates over standard input and output and waits for the client; it is not an interactive terminal.
See [MCP client setup](CLIENTS.md).

`serve --control-socket PATH` also exposes the optional [integrator control channel](INTEGRATOR_CONTROL.md) on Unix.
The socket requires an existing private parent directory owned by the current user.

For project-write setup, follow [Give a session the access it needs](SANDBOX.md#give-a-session-the-access-it-needs).
The launch flags below also apply to `warmup`:

| Option                 | Effect                                                                                                                                |
| ---------------------- | ------------------------------------------------------------------------------------------------------------------------------------- |
| `--writable-root PATH` | Add write access to an existing directory; may be repeated. See [filesystem rules](SANDBOX_CONFIGURATION.md#filesystem).              |
| `--no-sandbox`         | Run with host permissions. See [host execution](SANDBOX_CONFIGURATION.md#run-without-the-sandbox) for conflicts, caches, and cleanup. |

## Standalone sandbox

```sh
mcp-console sandbox -- python analysis.py
```

Run an installed program with Console's filesystem and network permissions.
[Run commands in a sandbox](SANDBOX_COMMAND.md) covers command arguments, workspace writes, networking, saved configuration, and cleanup.
`--config-env NAME` reads standalone configuration from the named environment variable; see [configuration inputs and precedence](CONFIGURATION.md#configuration-environment).

## Configuration options

These options work before or after `serve`, `warmup`, and ordinary `sandbox`, and with [`config resolve`](#inspect-configuration):

| Option                               | Effect                                                      |
| ------------------------------------ | ----------------------------------------------------------- |
| `--no-project-config`                | Skip the automatic project file.                            |
| `--no-global-config`                 | Skip the automatic global file.                             |
| `--no-config`                        | Skip both automatic files.                                  |
| `--config-file PATH`                 | Read one required YAML or JSON file instead of those files. |
| `--default-config KEY=VALUE`         | Set a launch default that configuration files can override. |
| `-c KEY=VALUE`, `--config KEY=VALUE` | Override settings from configuration files.                 |

`--config-file` may occur once and cannot be combined with discovery-exclusion flags.
Both assignment options accept dotted keys and YAML values; repeat them to apply several in order.

```sh
mcp-console serve --config-file ./session.yaml
mcp-console serve --no-project-config
mcp-console serve --default-config sandbox.profile=inherit_codex
mcp-console serve -c 'languages=[python,sql]' -c 'python=.venv'
```

Examples use POSIX shell quoting.
In PowerShell, quote the complete assignment when it contains spaces or structured values.

Project configuration is trusted input and can widen permissions.
Use `--no-project-config` for an unfamiliar project until you have reviewed its settings.
See [Configuration files](CONFIGURATION.md#files-and-precedence) for file discovery and precedence, and [Command-line overrides](CONFIGURATION.md#command-line-overrides) for assignment syntax and merge examples.

## Initialize configuration

```sh
mcp-console config init
mcp-console config init --global
mcp-console config init --directory /path/to/project
mcp-console config init -c r.resolution=disabled -c 'r.packages=[]' --dry-run
```

By default, `config init` creates `.agents/console/config.yaml` in the current directory.
`--directory PATH` selects an existing project directory and interprets relative settings there.
`--global` creates `$MCP_CONSOLE_HOME/config.yaml`, or `~/.agents/console/config.yaml` when the variable is unset.
The two options are mutually exclusive.
These files are shared by clients that launch Console with the same configuration locations.
Client registration remains a separate operation; see [MCP client setup](CLIENTS.md).

The starter selects `sandbox.profile: workspace`: ordinary workspace writes are allowed, while the project's `.git`, `.agents`, `.codex`, `.aws`, and `.claude` paths stay read-only.
Other readable host files remain readable.
It includes commented examples for permissions, packages, interpreters, environment variables, and caches.
Lines beginning with `##` are explanations; lines beginning with `#` are YAML examples to uncomment.

Use repeated `-c` or `--config` assignments to override the starter settings, with the same ordering and merge rules as launch overrides.
Assignments may appear before `config` or after `init`.
Supplied sections show active YAML with their guidance; unconfigured sections retain commented examples.
To start with read-only permissions instead, replace the starter profile:

```sh
mcp-console config init -c sandbox.profile=read_only
```

Later profile overrides replace the starter profile without retaining explicit workspace write grants.
Filesystem settings add exceptions to the selected profile; to restrict writes to selected directories, choose `read_only` and supply those directories in `sandbox.filesystem.read_write`.

Only the starter permissions and supplied settings are saved: initialization does not copy discovered global or project configuration or expand built-in defaults.
Explicit values are retained even when they equal built-in defaults, so a new project layer can override different global values.
Configuration-source flags such as `--config-file` and `--no-config` are not accepted.
`--default-config` is a launch setting and is not accepted by initialization; use `-c` for settings to save in the new file.

The command validates settings without starting interpreters or preparing packages, creates the configuration directory if needed, and reports the file path and contents.
An existing destination causes an error and is left untouched, including with `--dry-run` or `-c`.
`--dry-run` prints the proposed path and contents without creating files or directories.

See [Configuration files](CONFIGURATION.md) for how these files are found and applied.

## Inspect configuration

```sh
mcp-console config resolve > session.yaml
mcp-console config resolve --full --no-config --format json
mcp-console serve --config-file session.yaml
```

`config resolve` prints settings without starting runtimes or preparing packages.
The default output is YAML with built-in defaults omitted.
Use `--full` to include defaults or `--format json` for JSON; `--full` and `--prune` are mutually exclusive.
The [configuration options](#configuration-options) apply before `config` or after `resolve`.

See [Export a configuration](CONFIGURATION.md#export-a-configuration) for pruning rules, relative paths, editable fields, and what an export retains.

<span id="developer-only-commands"></span>

Contributor-only command contracts are documented in [Architecture](ARCHITECTURE.md) and the internal protocol references.
