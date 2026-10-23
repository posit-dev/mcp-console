# Command-line interface

**Status:** Draft v0.3 \
**Date:** 2026-07-29 \
**Scope:** User-facing `mcp-console` command surface

## 1. Principles

`mcp-console` is one standalone Rust binary.
CLI operations require explicit subcommands.
The `serve` subcommand runs the MCP stdio server.
The default client registration name is `console`, and the default packaged server command is:

```bash
uvx mcp-console serve
```

With an installed binary, the Codex registration command is:

```bash
codex mcp add console -- mcp-console serve
```

Under Codex's current naming convention, the tools are `mcp__console.send` and `mcp__console.session`.
The binary also exposes diagnostics, installation helpers, and commands that attach to an already-running MCP Console process.
These commands do not start a persistent daemon.
A running MCP client owns the server process and its session workers; attached viewers and CLI clients disappear or disconnect when that server exits.

The CLI follows these rules:

- stdout is reserved for the command's documented result;
- diagnostics and logs go to stderr;
- machine-readable output is explicit with `--json`;
- commands never silently select an ambiguous running server;
- observation never mutates a session;
- arbitrary code sent outside MCP is treated as an attributed primary evaluation and enters the transcript;
- no command auto-starts a detached background service.

## 2. Command summary

```text
mcp-console serve
mcp-console doctor
mcp-console install <client>
mcp-console list
mcp-console session <action>
mcp-console view [SESSION]
mcp-console watch [SESSION]
mcp-console send [SESSION] (--r CODE | --python CODE | --sql CODE | --stdin TEXT)
mcp-console transcript [SESSION]
mcp-console api
mcp-console --version
```

Internal implementation mode:

```text
mcp-console worker ...
```

`worker` is private, unstable, and absent from ordinary help output.

## 3. Target selection

More than one MCP client may launch `mcp-console`, and every process may contain a session named `default`.
Sidecar commands therefore resolve both a server instance and a session.

Common target options:

```text
--instance ID       Select one running MCP Console server instance.
--workspace PATH    Select the unique instance owning this workspace.
--session NAME      Select a named session; defaults to default where accepted.
```

Resolution order:

1. explicit `--instance`;
2. the unique live instance whose configured workspace contains the current directory or `--workspace` path;
3. the only live instance, if exactly one exists;
4. otherwise fail with a compact ambiguity error and show matching instance IDs.

A sidecar command never guesses among several matching processes.
Discovery sees only server processes reachable in the caller's local OS or container namespace.
When the MCP server runs on a remote host, inside an inaccessible container, or under another user, commands fail with a reachability diagnostic; they do not open or request a remote listener automatically.

## 4. `serve`

```bash
mcp-console serve
```

Starts the MCP server over stdio.
Stdout carries MCP frames only.

Initial options:

```text
--workspace PATH       Set the immutable server workspace.
--state-dir PATH       Override persistent session-record storage.
--runtime-dir PATH     Override ephemeral local-API discovery storage.
--local-api auto|off   Enable the process-scoped sidecar API; default auto.
--sandbox auto|on|off  Select worker sandbox policy.
--log-level LEVEL      Set diagnostic verbosity.
--log-file PATH        Write diagnostics to a file instead of stderr.
```

`serve` owns all session workers and the process-scoped local API.
It exits when its MCP transport closes, it receives a termination signal, or it encounters a fatal supervisor error.
Attached viewers do not keep it alive.

## 5. `doctor`

```bash
mcp-console doctor
mcp-console doctor --json
```

Performs read-only installation and runtime checks:

- binary and platform information;
- R discovery, version, architecture, and shared-library support;
- availability of the required R integration packages or bootstrap path;
- Python and reticulate initialization viability;
- DuckDB initialization;
- package cache and state-directory permissions;
- worker sandbox support;
- local API transport support;
- stale runtime discovery records.

`doctor` checks system readiness, not analysis quality or user workflow.

## 6. `install`

```bash
mcp-console install codex
mcp-console install claude-code
mcp-console install cursor
mcp-console install --print
```

Registers the MCP server command with a supported client or prints the configuration without changing files.

Default registered command:

```bash
uvx mcp-console serve
```

Useful options:

```text
--name NAME                Registration name; default console.
--scope user|project       Client configuration scope.
--workspace inherit|PATH   Workspace behavior for the server.
--version VERSION          Register an exact package version.
--dry-run                  Show changes without writing them.
--print                    Print generic JSON/TOML snippets only.
```

Registration adapters are conveniences.
The README must also document manual client configuration because client file formats and commands can change independently of MCP Console.

For stable installations, users may prefer:

```bash
uv tool install mcp-console
mcp-console serve
```

Exact `uvx` pinning is supported:

```bash
uvx mcp-console@0.3.0 serve
```

## 7. `list`

```bash
mcp-console list
mcp-console list --json
```

Lists live server instances and sessions discovered through protected runtime records and verified with a protocol handshake.

Example:

```text
INSTANCE  SESSION    STATE    LANGUAGE  WORKSPACE
7ac91f    default    idle               ~/project-a
7ac91f    model-fit  running  python    ~/project-a
98bd20    default    idle               ~/project-b
```

Stale records are ignored and may be cleaned up.
A process ID alone is never sufficient proof of identity; discovery uses a random instance ID plus a live handshake.

## 8. `session`

```text
mcp-console session list
mcp-console session status [SESSION]
mcp-console session prepare [SESSION] [--r REQUIREMENT]... [--python REQUIREMENT]...
mcp-console session interrupt [SESSION]
mcp-console session restart [SESSION] [--r REQUIREMENT]... [--python REQUIREMENT]...
mcp-console session close [SESSION]
```

These commands mirror the MCP `session` tool's behavior through the local control API.

Requirements are additive logical-session configuration and persist across runtime restarts.
`restart` loses in-memory R, Python, DuckDB, debugger, import, and loaded-package state while retaining requirements, workspace files, and transcript history.

Requirements supplied to `restart` are resolved before the current runtime is destroyed.
A resolution failure leaves the current runtime intact.

## 9. `view`

```bash
mcp-console view
mcp-console view model-fit
mcp-console view --instance 7ac91f --no-open
```

Launches the bundled human-facing viewer for a live process.
It does not start a daemon.

On Unix, the MCP server normally exposes its local API through a Unix-domain socket.
Since a browser cannot connect directly to that socket, `view` starts a short-lived loopback web process that:

1. connects to the selected local API;
2. serves the bundled static viewer;
3. proxies authenticated requests and event streams;
4. opens the user's browser unless `--no-open` is set;
5. exits when terminated or when the MCP server disappears.

Initial options:

```text
--listen HOST:PORT   Loopback address for the viewer proxy; default 127.0.0.1:0.
--no-open            Print the local URL without opening a browser.
--read-only          Disable primary evaluation and lifecycle controls.
```

The viewer may show:

- running evaluations and bounded live output;
- the Markdown transcript;
- full retained output by explicit request;
- plot and artifact galleries;
- R, Python, and SQL object inventories;
- revisioned live table views and point-in-time snapshots;
- session requirements and runtime metadata.

## 10. `watch`

```bash
mcp-console watch
mcp-console watch model-fit
mcp-console watch --after EVENT_ID
```

Follows the selected session's event stream in the terminal.
It is observational and does not acquire ownership of stdin.

`watch` reconnects with its last event cursor.
If the server reports that the cursor is older than retained replay state, it fetches a fresh session snapshot and resumes.

## 11. `send`

```bash
mcp-console send --r 'summary(df)'
mcp-console send model-fit --python 'print(metrics)'
mcp-console send --sql 'SHOW TABLES'
printf 'n\nc\n' | mcp-console send --stdin -
```

`send` submits code or interactive input through the same session state machine used by MCP.

Exactly one mode is required:

```text
--r CODE
--python CODE
--sql CODE
--stdin TEXT
--r-file PATH
--python-file PATH
--sql-file PATH
```

`-` reads exact bytes from the command's stdin.

An R, Python, or SQL submission is a **primary external evaluation**:

- it may mutate live state;
- it receives an evaluation ID and origin attribution;
- it appears in `transcript.md` as externally submitted work;
- session observers receive its events;
- the MCP-side agent is informed of intervening external evaluations before relying on stale assumptions.

There is intentionally no `--hidden`, `--scratch`, or `--read-only-code` mode.
Arbitrary R and Python cannot be guaranteed non-mutating.

`--stdin` sends exact stream text to the selected session worker whether it is evaluating or idle.
Nonempty input starts the session worker if needed.
No newline is added.

## 12. `transcript`

```bash
mcp-console transcript
mcp-console transcript model-fit
mcp-console transcript --open
```

Prints the selected session's generated `transcript.md` path.
`--open` asks the operating system to open the file.
It does not regenerate or curate the transcript.

## 13. `api`

```bash
mcp-console api
mcp-console api --json
```

Prints the selected running instance's local API metadata for viewer and integration developers:

```text
instance: 7ac91f
protocol: 1
transport: unix
endpoint: /run/user/1000/mcp-console/7ac91f.sock
workspace: /home/user/project-a
```

Secrets are omitted from human output.
Machine output may contain a short-lived local credential only when explicitly requested and when file and terminal safety checks permit it.

## 14. Exit behavior

Suggested exit-code classes:

```text
0   success
2   CLI usage or validation error
3   no matching live instance or session
4   ambiguous target
5   protocol or version incompatibility
6   session busy or wrong state
7   evaluation or preparation failure
8   local transport or supervisor failure
```

Language errors from `send` should return a nonzero evaluation-failure code while preserving the language's formatted diagnostic on stdout or stderr according to the command contract.

## 15. Packaging

The PyPI package named `mcp-console` contains platform wheels with the Rust executable and a very small Python launcher.
The launcher locates the bundled binary and replaces itself with it; the running server is Rust, not a Python implementation.

Release targets should initially include:

```text
macOS arm64
macOS x86_64, while supported
Linux x86_64
Linux arm64
Windows x86_64
```

Also publish native release archives.
Homebrew, WinGet, and other package-manager integrations can follow measured demand.

## 16. Deliberately omitted from v1

- a detached `mcp-console daemon`;
- remote, cross-container, or multi-user listeners and automatic forwarding;
- automatic attachment to an ambiguous process;
- a general terminal or PTY;
- invisible arbitrary sideband code execution;
- a browser listener opened by every MCP server process;
- keeping the MCP server alive solely because a viewer remains attached.
