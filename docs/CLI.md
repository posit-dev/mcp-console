# Command-line reference

Use `serve` to run the MCP server, `warmup` to check the runtimes and warm caches, `sandbox` to run one command under a sandbox, `config init` to create shared settings, `config resolve` to inspect configuration, and `configure-client` to register Console with a client.
Console runs on the local host; it does not create remote connections.

See [Install and connect](CLIENTS.md) for supported hosts and runtime setup.
The [CLI help page](cli-help.qmd) shows tested public-command help.
Run `mcp-console --help` for the version installed on your machine.

## Configure a client

```sh
uvx mcp-console configure-client codex claude
mcp-console configure-client claude --directory . --dry-run
mcp-console configure-client codex --pin-version current
mcp-console configure-client codex --name console-local -- --writable-root /data
```

`configure-client` registers a stdio MCP server named `console` that launches `uvx mcp-console serve`.
Install [uv](https://docs.astral.sh/uv/getting-started/installation/) so `uvx` is on the client's `PATH`; the clients need not be installed to write their settings.
One invocation applies the same options to every selected client and ignores repeated client names.
Use separate invocations for different client settings.

| Option                  | Effect                                                                                     |
| ----------------------- | ------------------------------------------------------------------------------------------ |
| `--name NAME`           | Select the server entry to create or update.                                               |
| `--directory PATH`      | Register in an existing project directory; omission selects global scope, even inside Git. |
| `--pin-version VERSION` | Use an exact PEP 440 version, `current`, or `latest` in the uvx launch.                    |
| `--no-uvx`              | Save an absolute executable path; conflicts with `--pin-version`.                          |
| `--no-auto-load`        | Let the client defer tool discovery. Tool approvals are still configured.                  |
| `--no-warmup`           | Skip background installation checks.                                                       |
| `--dry-run`             | Preview settings, any persistent installation, and warmup without making changes.          |
| `-- SERVER_OPTIONS`     | Append launch arguments after `serve`.                                                     |

Updating an entry replaces its managed launch arguments; include every server option you want to retain.
Codex names must match `^[a-zA-Z0-9_:@/.-]+$` and cannot be `codex_apps`.
Claude Code reserves `workspace`, `claude-in-chrome`, `computer-use`, `Claude Preview`, and `Claude Browser`.
Invalid names fail before installation or configuration changes.

### Launch selection

Codex registration supplies `--default-config sandbox.profile=inherit_codex` for both uvx and direct launches.
Configuration files can override this default; `-c` overrides files as well:

```sh
mcp-console configure-client codex --directory . -- -c sandbox.profile=workspace
```

To pass `--no-sandbox`, also clear the generated policy with `-c sandbox=null`.
See [Codex inheritance](SANDBOX_CONFIGURATION.md#inherit-permissions-from-codex) for policy capture and fallback behavior.

The default uvx command has no version suffix and may reuse an installed or cached release.
`--pin-version current` records the running version; `latest` requests the latest release at launch.
Exact versions are validated before changes, while package availability is checked when uv launches Console.
Version ranges are rejected.
A pinned Codex release must support `--default-config` and `sandbox.profile=inherit_codex`.

`--no-uvx` selects the executable running setup, for example `/opt/mcp-console/bin/mcp-console configure-client claude --no-uvx`.
For a disposable uv environment, setup first installs the recorded source persistently with `uv tool install` and saves that launcher.
This requires uv 0.11.29 or newer during setup; later client launches use the executable directly.
Wheel, archive, and Git sources preserve recorded hashes or resolved commits.
Directory sources are rebuilt from the recorded path, so keep that checkout unchanged during setup.
Persistent uv tools, Cargo builds, and other environments retain their existing executable paths.

Temporary index or `--find-links` installations lack a complete source record and are rejected before changes, as is missing source metadata.
Install the intended source persistently with the same resolver options, then invoke that installation's `configure-client --no-uvx`.
Setup reports the installation command and launcher, redacts source URL queries from reports, and leaves existing-tool conflicts to uv without forcing replacement.
Source detection and installation details live in [client launch selection](../src/configure_client/launch.rs).

### Scope and files

`--directory` selects where registration is written, not the client's later launch directory.
Relative paths use the current directory; setup does not discover a project by searching Git parents.

| Client      | Project configuration | Global configuration                                        |
| ----------- | --------------------- | ----------------------------------------------------------- |
| Codex       | `.codex/config.toml`  | `$CODEX_HOME/config.toml`, default `~/.codex/config.toml`   |
| Claude Code | `.mcp.json`           | `$CLAUDE_CONFIG_DIR/.claude.json`, default `~/.claude.json` |

Claude Code permissions go in the project's `.claude/settings.json`, or globally in `$CLAUDE_CONFIG_DIR/settings.json` (default `~/.claude/settings.json`).
Legacy Claude Code `.config.json` layouts are unsupported.

[Codex](https://learn.chatgpt.com/docs/config-file/config-basic#configuration-precedence) and [Claude Code](https://code.claude.com/docs/en/mcp#scope-hierarchy-and-precedence) prefer loaded project entries over same-named global entries.
Global setup warns about the nearest matching project entry, including during dry run.
Update it with `--directory PATH`, or remove that server's project entry to use the global registration.
The check follows Codex's configured project-root markers and Claude Code's ancestor `.mcp.json` files; it does not evaluate client trust, profiles, managed settings, or private local entries.

Setup prints every selected path and field, and preserves settings outside those fields.
It parses all selected client and permission files before installation or writes; malformed files, unreadable project files, and conflicting remote-server entries fail without overwriting them.
TOML comments are retained except inside replaced managed arrays; JSON is reformatted.
Existing files are updated in place to preserve identity, symlinks, ownership, and ACLs.
An interrupted write can leave an incomplete file, and a later failure can leave earlier files changed; completed actions remain in stdout.
New files are created atomically.
Avoid concurrent client or setup writers.

### Tool visibility and approval

Setup writes these settings for the selected server:

| Client      | Visible without tool search                        | Tool approval                                                             |
| ----------- | -------------------------------------------------- | ------------------------------------------------------------------------- |
| Codex       | `enabled = true`, `omit_tools_from = ["deferred"]` | `default_tools_approval_mode = "approve"`                                 |
| Claude Code | `alwaysLoad: true`                                 | `"mcp__console__*"` in `permissions.allow`, using the normalized `--name` |

`--no-auto-load` writes `omit_tools_from = []` or `alwaysLoad: false`.
Client startup limits and Codex's Strict Code Mode Only still apply.
Codex also receives `tool_input_schema_max_bytes = 12000` to retain Console's argument descriptions, including with deferred discovery.
Use current client versions that support these settings.

Existing Claude Code allow rules and Codex per-tool settings are preserved.
Per-tool overrides, explicit Claude Code `ask`/`deny` rules, and managed client restrictions can still require approval or block calls.
Tool approvals do not change Console's sandbox permissions.
Restart the client after setup; its project-trust and project-server approval decisions remain in effect.
For other clients, use the [stdio configuration example](CLIENTS.md#other-mcp-clients).

### Background warmup

After registration, setup starts one background [`warmup`](#check-your-installation) against the first selected client's launch command and prints its PID and log path.
It preserves the selected version and arguments and runs in `--directory`, or the current directory when omitted.
Logs remain under [Console home](ENVIRONMENT.md#server-paths-and-executable-discovery) in `logs/warmup-*.log`.
Setup returns immediately; warmup failures appear in the log without undoing registration, and launch failure prints a warning.
If the selected release has no `send` tool, only discovery and shutdown are checked.
Later client sessions can use different launch directories, environments, or permissions.

## Check your installation

```sh
uvx mcp-console warmup
mcp-console warmup --config-file ./session.yaml
mcp-console warmup -c 'languages=[python,sql]'
```

`warmup` starts a server, initializes an MCP connection, discovers its tools, and evaluates a small check in each enabled language before shutting down.
It checks the default session, selecting it explicitly when the server advertises named sessions.
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

| Option                               | Effect                                                            |
| ------------------------------------ | ----------------------------------------------------------------- |
| `--no-project-config`                | Skip the automatic project file.                                  |
| `--no-global-config`                 | Skip the automatic global file.                                   |
| `--no-config`                        | Skip both automatic files.                                        |
| `--config-file PATH`                 | Read selected YAML or JSON files in order instead of those files. |
| `--optional-config-file PATH`        | Allow a selected configuration file to be absent.                 |
| `--default-config KEY=VALUE`         | Set a launch default that configuration files can override.       |
| `-c KEY=VALUE`, `--config KEY=VALUE` | Override settings from configuration files.                       |

Repeat file and assignment options for ordered layers.
See [file selection](CONFIGURATION.md#choose-which-files-to-load) for required/optional files and incompatible options, and [command-line overrides](CONFIGURATION.md#command-line-overrides) for YAML syntax and precedence.
Examples use POSIX shell quoting; PowerShell also needs the complete assignment quoted when it contains spaces or structured values.

Project files are trusted input and can widen permissions.
Use `--no-project-config` until you have reviewed an unfamiliar project's settings.

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

The starter selects the [workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile), allowing ordinary project writes while protecting its metadata directories.
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
Add `--editable` to preserve authored tool settings while omitting generated descriptions and schema, including when using `--full`.
The [configuration options](#configuration-options) apply before `config` or after `resolve`.

See [Export a configuration](CONFIGURATION.md#export-a-configuration) for pruning rules, relative paths, editable fields, and what an export retains.

<span id="developer-only-commands"></span>

Contributor-only command contracts are documented in [Architecture](ARCHITECTURE.md) and the internal protocol references.
