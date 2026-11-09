# Sandbox permissions

Use this reference to choose which files code can read or change, which network connections it can make, and where it can write temporary files.
For an introduction to the trust model, read [Sandbox and trust](SANDBOX.md).
For command-line examples, see [Run commands in a sandbox](SANDBOX_COMMAND.md).
[Configuration files](CONFIGURATION.md) explains where to put these settings and how files and command-line overrides combine.

## Which permissions to configure

`sandbox` controls evaluated code and standalone commands; `resolver.sandbox` independently controls runtime inspection and package preparation.
Examples below use `sandbox`; the same fields apply to `resolver.sandbox`, except `inherit_codex`.
Worker defaults allow host reads, including readable secrets, and private temporary writes, with workspace writes and direct networking restricted.
Preparation also permits cache writes and approved downloads; see [Package settings](PACKAGE_CONFIGURATION.md).

Configuration is trusted launcher input and can widen permissions.
Clearing a whole policy with `null` restores its defaults while retaining enforcement; [merge rules](CONFIGURATION.md#merge-rules) define valid child values.
Use [host execution](#run-without-the-sandbox) to disable enforcement.
[`warmup`](CLI.md#check-your-installation) prepares dependencies with host permissions, then checks runtimes under the worker sandbox.

## Choose a base profile {#profiles-and-temporary-storage}

The [permission field reference](CONFIGURATION.md#sandbox-settings) lists profiles and defaults for workers and preparation.

For evaluated code, `read_only` keeps private writable temporary storage and restricted networking without a proxy by default.
It does not deny host reads.
Use `workspace` for ordinary project writes, or keep `read_only` and grant writes to [selected paths](#filesystem).
[Inherit permissions from Codex](#inherit-permissions-from-codex) describes the optional client-selected base and its fallback.

### Workspace profile

The `workspace` profile permits ordinary project writes while keeping workspace metadata read-only:

```yaml
sandbox:
  profile: workspace
```

The workspace is Console's launch directory, captured before evaluated code runs.
Changing the working directory from R or Python does not move the permission boundary.

If that directory is a filesystem root, the user's home directory, or a parent of home, Console warns and uses the `read_only` base.
Home follows the same [launch-environment selection](CONFIGURATION.md#paths) as `~` paths, including explicit `HOME` precedence on Windows.
The home directory need not exist; Console still protects its existing ancestors.
If home cannot be determined, automatic workspace writes fall back to `read_only` and relative write grants are ignored with a warning.
This also applies when `workspace` is the fallback for unavailable client metadata.
The same root check applies to `resolver.sandbox.profile: workspace`.
Explicit absolute write grants, network settings, and temporary-storage settings still apply.

| Path                                                | Workspace access                                                                                                             |
| --------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------- |
| Host files                                          | Read access allowed by the host's own permissions, including readable secrets.                                               |
| Ordinary workspace files                            | Create, edit, and delete files; changes persist after the session ends.                                                      |
| Root `.git`, `.agents`, `.codex`, `.aws`, `.claude` | Read-only, including protection against creating missing paths. Referenced Git worktree/submodule metadata is protected too. |

Private temporary storage and networking keep their defaults unless configured separately.
Explicit filesystem rules compose with the profile using [path precedence](#filesystem).
An explicit write grant at or beneath protected metadata can permit changes there; the profile is not an unchangeable permission ceiling.
Package preparation retains its independent policy.

### Allow workspace writes across projects

To select workspace writes for future launches across projects, update the global client registration:

```sh
uvx mcp-console configure-client claude -- -c sandbox.profile=workspace
```

The profile uses each future launch directory as the workspace.
The `-c` setting overrides configuration files and client inheritance.
An existing project client entry may take precedence over the global entry; see [client configuration](CLI.md#configure-a-client).

## Grant or restrict file access {#filesystem}

To save results in `output` while blocking access to `secrets`:

```yaml
sandbox:
  profile: read_only
  filesystem:
    read_write: [./output]
    deny: [./secrets]
```

Create `./output` before launch.
The [field reference](CONFIGURATION.md#sandbox-settings) defines the `read_only`, `read_write`, and `deny` lists.
Paths are concrete paths, not globs.
They resolve against the captured launch directory, not the configuration file.
A leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
Paths do not expand environment-variable references, create directories, grant parent directories, or erase symlink components.

Worker entries modify the host-read/private-write baseline.
When the resolved configuration contains neither a `profile` nor `read_write`, it grants no workspace writes.
An explicit resolver filesystem instead **replaces** its default entries, including host reads and cache writes; see [resolver grants](PACKAGE_CONFIGURATION.md#filesystem-grants).

Relative `read_write` paths and `--writable-root` arguments that resolve to a filesystem root, the user's home directory, or a parent of home are ignored with a warning.
This applies to both worker and resolver filesystem settings.
This includes `.` when Console starts from one of those directories, parent paths such as `..`, and symlinks to those roots.
To grant writes there explicitly, supply an absolute path.
The check includes absent paths and is repeated at native launch so a changed symlink cannot widen a relative grant.
Absolute paths retain their explicit authority and undergo native validation.

Narrower native rules take precedence.
At equal paths, deny wins over write, then read.
List order does not decide access.

A writable root grants writes throughout its subtree, including metadata directories such as `.git`.
It does not add read-only exceptions or create metadata directories.
Use `read_only` or `deny` for exclusions, or select the workspace profile for its explicit metadata rules.
These rules compose using the same specificity and equal-path precedence as other entries.

Platform behavior matters.
macOS and Linux can grant individual files.
macOS can grant creation at absent paths; Linux skips absent write roots until a later launch.
Linux cannot reliably reopen a narrower read beneath a broader denied mount.
Create intended writable directories first.
Console does not grant extra paths or switch backends to make an unsupported policy work.

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
It adds a filesystem grant without creating directories or selecting the workspace profile; more specific rules still apply.

## Configure network access {#network-choices}

The value of `sandbox.network` or `resolver.sandbox.network` selects the network policy.
The [permission field reference](CONFIGURATION.md#sandbox-settings) lists the boolean, string, sequence, and mapping forms.

Worker networking defaults to restricted without a proxy.
Resolver networking defaults to its [download proxy](PACKAGE_CONFIGURATION.md#expanded-public-default).
The `enabled` and `restricted` modes, including their boolean aliases, replace that generated resolver proxy.

A managed mapping requires a non-null `proxy` mapping.
`enabled` and `restricted` are reserved mode names; use a list such as `[enabled]` for a literal destination.
For the equivalent R value use `list("enabled")`, since a one-element character vector is also a scalar string.

Destination lists and strings normalize to `proxy.domains.allow` before layers merge.
They replace that list while retaining other proxy and socket settings; `[]` allows no destinations.
`network: null` preserves the earlier layer.
See [merge rules](CONFIGURATION.md#merge-rules) for clearing inherited settings.

### Proxy settings {#proxy-modes-and-destinations}

```yaml
sandbox:
  network:
    proxy:
      mode: full
      socks5: tcp
      allow_upstream_proxy: false
      domains:
        allow: [api.example.com, "*.example.org"]
        deny: [blocked.example.org]
```

Full mode supports HTTP/HTTPS proxying and the selected SOCKS mode.
`socks5: disabled` disables SOCKS; `tcp` enables SOCKS5 TCP.
The schema recognizes `tcp_udp`, but Console currently rejects it on every platform.

Limited mode permits plain HTTP GET, HEAD, and OPTIONS.
It rejects POST, HTTPS CONNECT, and SOCKS; every explicitly supplied `socks5` field is invalid in limited mode, even `disabled`.
It is not read-only HTTPS, does not intercept TLS, and does not guarantee side-effect-free requests.

### Allow or block destinations {#destination-rules}

Domain rules use hostnames, not URLs, paths, or host-and-port restrictions.
Exact names match that host only; `*.example.org` matches subdomains but not the apex, and `**.example.org` matches both.
Patterns support `*` and `?` wildcards; character classes such as `[0-9]` are rejected.
Matching deny rules win.
Native IP-literal forms are also accepted.

An explicit `domains` mapping replaces the generated destination default; omitted allow/deny lists are empty.
Worker defaults allow no destinations; resolver defaults allow its package sources.
This does not override ordinary deep merging between configuration layers.
To remove an explicitly inherited field, clear and rebuild its containing mapping.

### Use an upstream proxy

When `allow_upstream_proxy: true`, onward routing uses the trusted launch environment, not workload overrides; destination and method restrictions still apply.
The public format has no per-port rules, URL rules, credential injection, listener configuration, or per-request approval API.

### Allow local services {#unix-sockets-and-local-binding}

```yaml
sandbox:
  network:
    proxy: {mode: full}
    sockets:
      unix_sockets: [/tmp/service.sock]
    allow_local_binding: true
```

`network.sockets.unix_sockets` controls direct Unix-socket access:

| Value                                                      | Direct Unix-socket access                                                                      |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `[]` (default)                                             | No Unix-socket grants.                                                                         |
| List of absolute path strings, after leading-`~` expansion | Grant the named sockets. Supported on macOS; nonempty lists are rejected on Linux and Windows. |
| `dangerously_allow_all`                                    | Permit all Unix sockets. Supported on macOS and Linux; rejected on Windows.                    |

There are no deny lists or globs.
Native macOS direct-socket grants use subpath matching, so this is not an exact-path firewall across every socket route.
Filesystem permissions still apply.

`network.allow_local_binding` controls local binding:

| Value                      | Behavior                                                                                                 |
| -------------------------- | -------------------------------------------------------------------------------------------------------- |
| `auto` or `null` (default) | On Unix, false for workers and true for resolvers. On Windows, true for a managed proxy.                 |
| `true`                     | Permit local binding and related native local-socket access.                                             |
| `false`                    | Prohibit local binding. Rejected for Windows managed proxies, which require bidirectional host loopback. |

Omission and explicit `null` select `auto`; a later `null` replaces an earlier boolean.
Console resolves it after normal file and CLI layering and shows the effective boolean in `config resolve --full`.
Unix resolvers enable it by default because installers need coordination and DNS.
Console rejects explicit `false` when the selected policy cannot honor it.
To keep local binding disabled on Windows, select proxy-free `network: restricted` instead.
It affects private-address checks and native local socket access, including macOS loopback traffic and DNS.
It does not expose a Linux network namespace to a host browser.

Direct connections allowed by socket/local permissions do not pass through the proxy's HTTP-method filter.
Limited mode therefore does not restrict every granted channel to GET/HEAD/OPTIONS.

## Choose temporary storage {#temporary-storage}

```yaml
sandbox:
  profile: workspace
  tempdir: managed
```

`sandbox.tempdir` and `resolver.sandbox.tempdir` select automatic temporary-storage grants.
The [permission field reference](CONFIGURATION.md#sandbox-settings) compares `managed`, `read_write`, and `read_only` storage.

### Host temporary directories

`read_write` also grants temporary writes under the base read-only policy.
On Unix its platform default grant is `/tmp`; configured/inherited `TMPDIR`, `TEMP`, and `TMP` add their named locations.
On Windows, if all three child variables are unset or empty, Console exports the granted host default as `TMPDIR`, `TEMP`, and `TMP` so runtimes can find it.
This does not create managed storage or add cleanup guarantees.
The directories must already exist; Console does not provision caller-owned storage.
Relative temp paths receive write grants relative to the launch working directory; their environment values remain relative.
Changing directory, including in a subprocess, can make those values refer to an ungranted location.
Use absolute temp paths when working directories can change.
`read_only` omits automatic grants rather than imposing an overriding denial: explicit filesystem permissions, including a workspace root containing a temp path, still apply.
R needs a writable temporary directory and can fail to initialize in that mode.

### Cleanup and runtime storage

`serve` and ordinary `sandbox` use the same policy and defaults.
`--no-sandbox` uses normal runtime temporary-directory selection without general-purpose managed storage or temporary-environment redirection.
Console guarantees temporary-storage cleanup only with an active sandbox and `tempdir: managed`; the native runner retires that storage after the workload exits.
In other modes, Console adds no temporary-directory cleanup guarantees.
R runs its normal best-effort shutdown, including its session `Rtmp` cleanup.
On Unix that cleanup uses `rm` from the workload's `PATH` and can leave files when that command is unavailable or a subtree is not writable.
Native Python DuckDB retains its own `temp_directory` and `secret_directory` defaults outside managed mode, including the normal persistence of stored secrets.
Set those connection settings explicitly when their default locations need different filesystem permissions.
Caller-owned parent directories and unrelated files remain the caller's responsibility.

## Inherit permissions from Codex

The Codex registration created by [`configure-client`](CLI.md#configure-a-client) already selects this profile as a launch default.
Configuration files can override that choice.
To select it explicitly in a configuration file, use:

```yaml
sandbox:
  profile: inherit_codex
```

The `inherit_codex` profile inherits usable sandbox metadata from the first `send` call.
If inheritance is unavailable, it uses the built-in `read_only` profile: host reads, private temporary writes, and restricted networking before explicit local settings are applied.
Missing metadata selects this fallback silently.
Malformed or unsupported metadata emits a warning on the server's standard error stream and selects the fallback.
Select this profile only when the MCP client is trusted to configure Console's sandbox, including disabling it.
`inherit_codex` is not accepted under `resolver.sandbox.profile`, with or without a fallback.
When the resolved profile is `read_only` or `workspace`, client sandbox metadata has no effect.

### Choose a fallback

For one configuration shared by Codex and Claude, or another client without this metadata, configure the default permissions for that client with `fallback`:

```yaml
sandbox:
  profile:
    inherit_codex:
      fallback: workspace
  filesystem:
    read_write: [./output]
```

Each connection selects one base profile on its first `send`.
Usable Codex metadata selects `inherit_codex`; otherwise the configured `workspace` fallback permits ordinary project writes with metadata protections.
The configured fallback applies to both missing and malformed metadata; malformed metadata warns which profile was selected.
The fallback's workspace grants are not added when `inherit_codex` is selected.
Explicit filesystem entries compose with the selected base using the ordinary specificity and conflict rules.
Explicit network and temporary-storage settings apply to the selected base; without a network override, the Codex network mode applies.
Create writable directories such as `./output` before launch.

The [field reference](CONFIGURATION.md#sandbox-settings) defines fallback values and validation.
Omitting `fallback` selects `read_only` when inheritance is unavailable.
A failure to prepare runtimes or start the selected sandbox is an error; it does not select the fallback.
The standalone `sandbox` command has no client metadata, so it silently uses the configured fallback, defaulting to `read_only`.
`config resolve` preserves the selection policy and automatic cache choice without selecting a runtime profile or preparing interpreters.
Broad-root checks and their warnings apply at launch, so resolving configuration from a broad root does not replace the configured workspace permissions.

### Metadata and permission translation

Console reads the first `send` request's sandbox metadata before runtime discovery, preparation, or startup.
Initialization, discovery, and pings remain available while it waits.
The capability names, accepted metadata shape, and path validation live in the [Codex permission adapter](../src/server/sandbox.rs).

Console translates the current `permissionProfile` and uses `sandboxCwd` to resolve symbolic project roots:

| Client permissions                 | Console behavior                                                                                                                                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Managed, restricted filesystem     | Use recognized read, write, and deny entries as the base, including additional writable roots.                                                  |
| Managed, unrestricted filesystem   | Permit host filesystem access, composing explicit local filesystem rules, while retaining Console's native supervision and selected networking. |
| External filesystem enforcement    | Use unrestricted filesystem access within Console's runner; Console does not verify the external boundary.                                      |
| Disabled enforcement (full access) | Disable Console's worker and preparation sandboxes, as with `--no-sandbox`.                                                                     |
| Restricted or enabled network      | Apply that worker network mode unless `sandbox.network` is explicitly configured.                                                               |

Symbolic project roots use the client's sandbox working directory, which may differ from Console's launch directory; inheritance does not change the worker's working directory.
Local grants and denials retain their normal precedence.
Console's host-read baseline and temporary-storage settings still apply to restricted profiles, so inheritance does not reproduce every Codex restriction.

Client metadata does not supply proxy endpoints, destination rules, credentials, or proxy approvals.
Console keeps explicit proxy configuration and normally inherited proxy environment variables; metadata alone cannot reconstruct the client's proxy.
Dependency preparation retains its separate resolver policy unless inherited full access disables sandboxing.
Full access defaults to host caches; managed caches, including custom cache directories, require sandboxing.
Inherited full access disables enforcement of local filesystem and network restrictions too.

Malformed or unsupported permission entries make the whole inherited profile unavailable: dropping one denial could widen access.
Unknown additional fields and client backend/launcher selections are ignored.
Console uses its own pinned runner and validates the translated policy; metadata cannot choose an executable or backend.

### Policy lifetime

The first received `send` captures the translated policy or fallback for the connection, even if that call is cancelled before execution.
Workers and explicit restarts retain that policy.
Later calls without metadata keep it; later calls with different permissions fail before submitting code, with a request to reconnect.
When a usable client policy was captured, later malformed metadata rejects the call and warns that reconnecting is needed to apply the fallback.
If the fallback is already active, later malformed metadata warns and the call continues under that same fallback.
Reconnect after changing the client's permissions so Console can prepare its runtimes under the new policy.

## Run without the sandbox

`serve --no-sandbox` selects host permissions for both package preparation and evaluated code.
Explicit `sandbox` and `resolver.sandbox` mappings conflict with this flag; omit them or clear both whole nodes to retain other configuration settings:

```sh
mcp-console serve --no-sandbox -c sandbox=null -c resolver.sandbox=null
```

This mode defaults to host caches and rejects managed caches, including custom cache directories.
It removes native enforcement and native descendant cleanup, and omits managed temporary storage and temporary-environment redirection.
Normal relay shutdown still reaps its direct worker, but Console does not add a temporary-storage cleanup guarantee.
See [Temporary storage](#temporary-storage) for runtime-specific behavior.

`--no-sandbox` also conflicts with `--writable-root`.
For an exported configuration, pass `--no-sandbox` to both `config resolve` and `serve`; launch flags are not stored in YAML.
See [Cache selection](PACKAGE_CONFIGURATION.md#cache-locations) for clearing inherited permission settings deliberately.

## Platform support {#native-capabilities}

| Platform | Important limits                                                                                                                                                             |
| -------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| macOS    | Seatbelt enforcement and managed proxy supported. Console rejects `tcp_udp`.                                                                                                 |
| Linux    | Bubblewrap enforcement; managed bridge is TCP-only. Nonempty Unix-socket path lists and `tcp_udp` are rejected. Socket allow-all is supported, but is not path-specific.     |
| Windows  | Windows x64 requires MXC for workers and preparation. TCP proxies resolve local binding to true under `auto`; explicit false is rejected. UDP and Unix sockets are rejected. |

A common schema does not imply equal native capabilities.
Console rejects unsupported explicit requests rather than silently ignoring them.
Setup failures do not trigger an automatic retry with weaker enforcement or without a sandbox.

### Windows MXC requirement

Normal sessions and standalone commands require MXC.
Unsupported hosts and policies fail before the workload executes; there is no backend or host-execution fallback.
Explicit `serve --no-sandbox` remains available.

The host must support native process security environments and the requested filesystem policy; read-deny rules need native deny-path support.
Console probes actual creation and closure.
Update Windows if that probe fails.
MXC needs no account provisioning, UAC setup, or persistent state directory.

Only clean hosts are supported: remove any legacy Console sandbox accounts, stored credentials, and firewall rules independently before use.
MXC runs as the host identity and can decrypt readable DPAPI-protected credentials; the runner neither detects nor cleans up legacy state.
Environment setup and Windows path handling are documented beside [runner launch](../src/sandbox/runner.rs).

Windows process-creation error 4551 indicates host application-control policy.
The host must permit the executable and downloaded interpreter DLLs; this is separate from Console's sandbox settings.

### Linux backend {#explicit-linux-backend-selection}

The native default is bubblewrap.
Namespace failure does not trigger a backend switch or unsandboxed retry.
The inherited-procfs alternative keeps the same backend and policy.
Standalone `linux_backend: landlock` is rejected.
See [Linux compatibility](LINUX_COMPATIBILITY.md#policy-and-backend-contract).

## Process cleanup {#supported-hosts-and-lifetime-limits}

Console waits for sandboxed processes to stop before removing managed temporary storage.
Cleanup failures are errors: unconfirmed process retirement retains private storage and blocks worker replacement.
Closing pipes or reaping the direct worker does not prove that its native descendants have stopped.

On **macOS**, cleanup covers the owned process group and detached descendants the runner observes.
A descendant that detaches and becomes orphaned before observation may escape discovery.
General shell job suspension and resumption are unsupported.

On **Linux**, Bubblewrap provides namespace-based enforcement and retirement, and the host must permit namespace setup.
Fresh procfs is optional; inherited procfs can expose host PIDs and readable metadata.
A stopped namespace init can exceed cleanup deadlines when emergency termination facilities are unavailable.
See [Linux compatibility](LINUX_COMPATIBILITY.md).

On **Windows**, the native runner owns a non-breakaway Job and confirms zero active processes before successful retirement and private-storage removal.
Cleanup failure retains storage and blocks replacement.

Caller death triggers cleanup while the runner remains alive.
There is no independent recovery process.
Runner death, a stopped or hung runner, or a forced frontend kill does not guarantee descendant termination, temporary-storage deletion, or terminal restoration.
Surviving sandboxed processes retain their native enforcement.

These guarantees require an active sandbox and managed storage; [other temporary-storage modes](#temporary-storage) use normal runtime cleanup.

## Supply configuration through an environment variable {#explicit-complete-policy}

Standalone commands accept the same public configuration through `sandbox --config-env NAME`.
See [Configuration through an environment variable](CONFIGURATION.md#configuration-environment) for precedence, defaults, and examples.
