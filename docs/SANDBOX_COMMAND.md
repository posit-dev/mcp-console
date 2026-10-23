# Run commands in a sandbox

Use `mcp-console sandbox` to run a program and its child processes with limits on file and network access.
You can use it directly from your terminal, scripts, or build jobs, independently of the MCP server.

Read [Sandbox and trust](SANDBOX.md) for the permission model, or [Sandbox permissions](SANDBOX_CONFIGURATION.md) for exact settings and platform limits.

## Install

Install Console with [uv](https://docs.astral.sh/uv/getting-started/installation/):

```sh
uv tool install mcp-console
```

The sandbox supports macOS, Linux, and Windows x64.
See [platform requirements](SANDBOX_CONFIGURATION.md#native-capabilities) for host setup, including MXC on Windows.

## Run a program

From your project directory, run an existing Python or R script:

```sh
mcp-console sandbox -- python analysis.py
mcp-console sandbox -- Rscript analysis.R
```

Replace the command after `--` with the program and arguments you want to run.
The executable must be available on the host.
Console does not install the target program or prepare its dependencies.
The program starts in your current directory, receives standard input, and writes output and errors to your terminal or calling process.

With the built-in `read_only` profile, the program can read files your account can read and write to private temporary storage.
Ordinary project writes and direct networking are restricted.
Readable files include secrets: `read_only` protects files from changes, not from being read.
The [configuration files](#save-a-reusable-policy) you load can change these permissions.

## Allow project writes

Use the `workspace` profile when a program needs to edit project files or save reports:

```sh
mcp-console sandbox -c sandbox.profile=workspace -- python analysis.py
```

The workspace is the directory where you start Console.
The profile permits ordinary writes there while keeping its `.git`, `.agents`, `.codex`, `.aws`, and `.claude` paths read-only by default.
Network access is configured separately.
See [Workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile) for the complete rules.

### Write to a selected directory

To start from the built-in permissions and add a writable output directory:

```sh
mkdir output
mcp-console sandbox --no-config --writable-root output -- python analysis.py
```

`--no-config` skips global and project configuration; `--writable-root output` adds permission to write under `output`.
Private temporary storage is still writable.
Create writable directories before launch, and repeat `--writable-root` to grant additional paths.

## Allow network access

To let a program contact selected destinations through Console's proxy:

```sh
mcp-console sandbox -c 'sandbox.network.proxy.domains.allow=[example.com]' -- curl https://example.com
```

The program must use the proxy settings Console supplies; `curl` supports them.
Destination rules use hostnames, not URLs or URL paths.
See [Network access](SANDBOX_CONFIGURATION.md#network-choices) for proxy settings, local services, and platform limits.

To allow host networking while keeping filesystem permissions:

```sh
mcp-console sandbox -c sandbox.network=enabled -- curl https://example.com
```

## Save a reusable policy

Save settings you want to reuse in a YAML file.
For example, `sandbox.yaml` can allow writes to `output` while blocking access to `secrets`:

```yaml
sandbox:
  profile: read_only
  filesystem:
    read_write: [output]
    deny: [secrets]
```

Create `output` before running the command:

```sh
mcp-console sandbox --config-file sandbox.yaml -- python analysis.py
```

`--config-file` selects that file instead of the automatic global and project files.
Relative paths use the directory where you launch Console, including paths written in the YAML file.

For settings you want applied automatically, use Console's [global or project configuration files](CONFIGURATION.md#files-and-precedence).
Command-line `-c` settings override matching file settings.
[Environment variables](ENVIRONMENT.md#where-to-set-variables) covers variables supplied to the program.

### Pass settings through an environment variable

You can store a complete configuration as a one-line YAML or JSON object and pass its variable name through `--config-env`.
For example, in a POSIX shell, after creating `output`:

```sh
export SANDBOX_SETTINGS='{sandbox: {profile: read_only, filesystem: {read_write: [output], deny: [secrets]}, network: restricted}}'
mcp-console sandbox --config-env SANDBOX_SETTINGS -- python analysis.py
```

The variable uses the same structure as a configuration file, including the outer `sandbox:` key.
`--config-env` replaces global and project file discovery while retaining Console's built-in defaults.
Command-line `-c` settings override matching values in the variable.

## Shell commands and redirection

Console passes the command's arguments directly to the executable.
To use shell syntax, launch a shell explicitly.
For example, on macOS or Linux:

```sh
mcp-console sandbox -c sandbox.profile=workspace -- sh -c 'python analysis.py > report.txt'
```

Here the shell creates `report.txt` inside the sandbox.
A redirection written in your outer shell runs outside the sandbox's permissions.
Use the same approach for pipelines when all their commands should run inside the sandbox.

## Output and cleanup

The program produces its own output and files; Console does not create session recordings or capture plots for standalone commands.
Files saved to writable project paths remain after the command finishes.
With the default managed temporary storage, Console waits for sandboxed processes to stop before removing their private temporary files.
Setup and cleanup failures are reported as errors.

Cleanup after a crashed or forcibly stopped runner has [platform-specific limits](SANDBOX_CONFIGURATION.md#supported-hosts-and-lifetime-limits).
[Temporary storage](SANDBOX_CONFIGURATION.md#temporary-storage) describes the other storage options.

For all command options, see [`sandbox` CLI help](cli-help.qmd#all-platforms-mcp-console-sandbox).
Integrations can also [supply configuration through an environment variable](CONFIGURATION.md#configuration-environment).
