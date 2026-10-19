# Command-line reference

Use `serve` to run the MCP server, `warmup` to check the runtimes and warm caches, `sandbox` to run one command under a sandbox, `config init` to create shared settings, `config resolve` to inspect configuration, and `configure-client` to register Console with a client.
Console runs on the local host; it does not create remote connections.

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
If an earlier registration created a project entry, rerun setup with `--directory PATH` for that project directory.
Earlier automatic registrations used the Git root.
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

### Other MCP clients

Configure a stdio server with command `uvx` and arguments `["mcp-console", "serve"]`.
Clients using an `mcpServers` mapping can use:

```json
{
  "mcpServers": {
    "console": {
      "command": "uvx",
      "args": ["mcp-console", "serve"]
    }
  }
}
```

Console runs on the same host as the client, usually in the agent's workspace.
For remote work, run both on the remote host; Console does not manage remote connections or containers.

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
See [Connect your client](getting-started.qmd#connect-your-client).

For ordinary project writes, create `.agents/console/config.yaml` in the launch directory:

```yaml
sandbox:
  profile: workspace
```

The [workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile) allows project writes while keeping workspace metadata read-only.
It retains the default private temporary storage and restricted networking.

Each `configure-client` call replaces that entry's managed launch arguments.
Include every server option you want to retain when changing its permissions, and restart the client afterward.

### Allow workspace writes across projects

To select workspace writes for future launches across projects, update the global client registration:

```sh
uvx mcp-console configure-client claude -- -c sandbox.profile=workspace
```

The profile uses each future launch directory as the workspace; running setup does not bind it to the setup directory.
The `-c` setting overrides profiles selected in configuration files or inherited from the client.
Use this option when you want that write policy across projects.
An existing project client entry may take precedence over the global entry; see [client configuration](#configure-a-client).

### Grant writes to one directory

To add a writable path through a project client entry, create the directory before registering the grant:

```sh
mkdir output
uvx mcp-console configure-client claude --directory . -- -c sandbox.profile=read_only --writable-root output
```

Use `codex` in place of `claude` for Codex.
The client chooses Console's launch directory, and Console resolves `output` there.
The `read_only` base restricts ordinary workspace writes; `--writable-root` adds the explicit grant.
Other configured filesystem permissions still apply.
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

These options work before or after `serve`, `warmup`, and ordinary `sandbox`:

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

## Initialize configuration

```sh
mcp-console config init
mcp-console config init --global
mcp-console config init --directory /path/to/project
mcp-console config init -c r.resolution=disabled -c 'r.packages=[]' --dry-run
```

`config init` creates `.agents/console/config.yaml` in the current directory, without searching Git parents or falling back to global configuration outside Git.
Use `--directory PATH` for another existing project directory, or `--global` for Console's global configuration location; these options are mutually exclusive.
The annotated starter selects `sandbox.profile: workspace`, with comments explaining workspace writes and read-only project metadata.
It includes commented examples for common settings, using `##` for explanations and `#` for YAML to uncomment.
Ordered `-c`/`--config` assignments apply after the starter permissions.
Other defaults remain implicit; initialization does not read discovered files or materialize defaults.
Existing destinations cause an error and remain unchanged.
The command prints the path and file contents; `--dry-run` previews them without creating files or directories.

`-c` may appear before `config` or after `init`.
Configuration-source flags and `--default-config` are not accepted by initialization.
See [Create a configuration file](CONFIGURATION.md#create-a-configuration-file) for paths and behavior.

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
See [Windows host requirements](SANDBOX_CONFIGURATION.md#windows-mxc-requirement).

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
