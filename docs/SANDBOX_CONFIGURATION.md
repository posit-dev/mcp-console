# Sandbox permissions

Use this reference to choose which files code can read or change, which network connections it can make, and where it can write temporary files.
For an introduction to the trust model, read [Sandbox and trust](SANDBOX.md).
For command-line examples, see [Run commands in a sandbox](SANDBOX_COMMAND.md).
[Configuration files](CONFIGURATION.md) explains where to put these settings and how files and command-line overrides combine.

## Which permissions to configure

| Node               | Accepted values   | Applies to                                                                 |
| ------------------ | ----------------- | -------------------------------------------------------------------------- |
| `sandbox`          | Mapping or `null` | Code evaluated in Console, standalone commands, and their child processes. |
| `resolver.sandbox` | Mapping or `null` | Runtime inspection and package preparation.                                |

The two policies are independent.
Examples below configure `sandbox`; the same fields are accepted under `resolver.sandbox`, except for the `inherit_codex` profile.
Defaults below apply after launch defaults, configuration files, and command-line overrides have been combined.
Worker defaults permit host reads, including readable secrets, and private temporary writes, with ordinary workspace writes and direct networking restricted.
Preparation defaults also permit dependency-cache writes and approved downloads; see [Package sources and caches](RESOLVER.md).

Configuration is trusted launcher input and can widen these permissions.
Setting either whole permission node to `null` clears inherited settings and restores that context's built-in defaults; enforcement remains enabled.
Child nodes such as `filesystem` and `network.proxy` require their documented types and cannot remain `null` in the final configuration.
To disable enforcement, see [Run without the sandbox](#run-without-the-sandbox).

[`warmup`](CLI.md#check-your-installation) prepares dependencies with host permissions and the configured caches; its runtime checks retain the worker sandbox.

## Choose a base profile {#profiles-and-temporary-storage}

| Node                       | Accepted values                                                                  | Meaning and default                                                                            |
| -------------------------- | -------------------------------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `sandbox.profile`          | `read_only`, `workspace`, `inherit_codex`, an `inherit_codex` mapping, or `null` | Select the base profile. Omitted, `null`, or `read_only`: host reads without workspace writes. |
| `resolver.sandbox.profile` | `read_only`, `workspace`, or `null`                                              | Select a preparation profile. Omitted, `null`, or `read_only`: no workspace profile.           |

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

| Access             | With `profile: workspace`                                                                                                                                                                                 |
| ------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Host files         | Read files allowed by the host's own permissions, including readable secrets.                                                                                                                             |
| Workspace files    | Create, edit, and delete ordinary files throughout the workspace. These files persist after the session ends.                                                                                             |
| Workspace metadata | Read but do not write `.git`, `.agents`, `.codex`, or `.aws` at the workspace root. Missing metadata paths are protected from creation too. Referenced Git worktree/submodule metadata is also protected. |
| Temporary storage  | Keep the default private writable storage with `tempdir: managed`; shared host temporary directories receive no automatic write grant.                                                                    |
| Network            | Keep restricted direct networking unless configured separately.                                                                                                                                           |

For example, an agent can read `data.csv`, update `analysis.py`, and save `plot.png` in the workspace.
It cannot edit `.agents/console/config.yaml`, delete `.git`, or write ordinary files outside the workspace and private temporary storage without another grant.
Package preparation retains its [independent policy](RESOLVER.md).

To construct the same permissions without selecting a profile, use:

```yaml
sandbox:
  filesystem:
    read_write: [.]
    read_only: [.git, .agents, .codex, .aws]
```

The worker's default host reads, managed temporary storage, and restricted networking supply the rest.
The explicit read-only entries protect those fixed workspace paths, including absent paths and when a broader write root is added.
Both forms retain native Git pointer protections for the writable root.
The profile does not protect `.claude`; add it to `filesystem.read_only` if needed.

When the resolved configuration contains neither a `profile` nor explicit write grants, it retains the host-read baseline without workspace writes.
Explicit filesystem entries compose with the profile: narrower rules override ancestors, and at the same path deny wins over write, then read.
An explicit write at a protected metadata path or beneath it can therefore permit changes there; these defaults are not an unchangeable ceiling.
Network and temporary-storage selections remain independent.

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
`sandbox.filesystem` and `resolver.sandbox.filesystem` each require a mapping.
Fields within that mapping are:

| Field        | Accepted values      | Access to each path and its descendants                             |
| ------------ | -------------------- | ------------------------------------------------------------------- |
| `read_only`  | List of path strings | Permit reads, with no writes at that path.                          |
| `read_write` | List of path strings | Permit reads and writes, including creation, editing, and deletion. |
| `deny`       | List of path strings | Deny reads and writes.                                              |

Each list defaults to `[]` within a supplied mapping.
Paths are concrete paths, not globs.
They resolve against the captured launch directory, not the configuration file.
A leading `~` follows the shared [path rules](CONFIGURATION.md#paths).
Paths do not expand environment-variable references, create directories, grant parent directories, or erase symlink components.

Worker entries modify the host-read/private-write baseline.
When the resolved configuration contains neither a `profile` nor `read_write`, it grants no workspace writes.
An explicit resolver filesystem instead **replaces** its default entries, including host reads and cache writes; see [resolver grants](RESOLVER.md#filesystem-grants).

Relative `read_write` paths and `--writable-root` arguments that resolve to a filesystem root, the user's home directory, or a parent of home are ignored with a warning.
This applies to both worker and resolver filesystem settings.
This includes `.` when Console starts from one of those directories, parent paths such as `..`, and symlinks to those roots.
To grant writes there explicitly, supply an absolute path.
The check includes absent paths that would grant writes to home or its ancestors.
Console checks relative grants again before every native launch, including after worker replacement, so a changed symlink cannot turn one into a broad grant.
Absolute grants retain their explicit authority and the native runner's path validation.

Narrower native rules take precedence.
At equal paths, deny wins over write, then read.
List order does not decide access.

The native runner protects writes to `.git`, `.agents`, `.codex`, and `.aws` beneath writable roots by default.
These are not unchangeable denial ceilings: explicit equal-path or narrower writes can override them, and broader ancestor grants can subsume narrower roots and their metadata defaults.
Add fixed metadata paths to `read_only` when needed.
`.claude` is not in this default set; use `read_only: [.claude]` to protect it explicitly.

Platform behavior matters.
macOS can grant individual files and creation at absent granted paths.
Linux can reject file write roots, skips absent write roots until a later launch, and cannot reliably reopen a narrower read beneath a broader denied mount.
Create intended writable directories first.
Console does not grant extra paths or switch backends to make an unsupported policy work.

## Configure network access {#network-choices}

The value of `sandbox.network` or `resolver.sandbox.network` selects the network policy:

| Value                   | Effect                                                 |
| ----------------------- | ------------------------------------------------------ |
| `network: restricted`   | Restricted native networking, without a managed proxy. |
| `network: enabled`      | Host networking; filesystem enforcement remains.       |
| `network: {proxy: {…}}` | Restricted native networking with a managed proxy.     |

Worker networking defaults to restricted without a proxy.
Resolver networking defaults to its [download proxy](RESOLVER.md#expanded-public-default).
Explicit scalar values also remove that generated resolver proxy.

A managed mapping requires a non-null `proxy` mapping.
There is no `network: full` shortcut or public proxy-enabled boolean.

Fields within a network mapping are:

| Field                 | Accepted values                 | Meaning and default                                                                                                             |
| --------------------- | ------------------------------- | ------------------------------------------------------------------------------------------------------------------------------- |
| `proxy`               | Required mapping                | Managed HTTP/HTTPS and optional SOCKS proxy. See [Proxy settings](#proxy-modes-and-destinations).                               |
| `sockets`             | Mapping                         | Direct Unix-socket permissions. Omission grants no Unix sockets.                                                                |
| `allow_local_binding` | `auto`, `null`, `true`, `false` | Local binding and related native socket access. Omission selects `auto`; see [Local services](#unix-sockets-and-local-binding). |

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

Fields under `sandbox.network.proxy` or `resolver.sandbox.network.proxy`:

| Field                  | Accepted values                                    | Meaning and default                                                                                                        |
| ---------------------- | -------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------- |
| `mode`                 | `full`, `limited`                                  | Proxy methods and protocols. Default: `full`.                                                                              |
| `socks5`               | `disabled`, `tcp`; `tcp_udp` is currently rejected | SOCKS support in full mode. Default: `tcp`. Omit this field in limited mode.                                               |
| `allow_upstream_proxy` | `true`, `false`                                    | Permit onward routing through the trusted launch environment's proxy. Default: `false`.                                    |
| `domains`              | Mapping                                            | Destination allow/deny rules. Omission permits no worker destinations or uses the resolver's generated download allowlist. |

Full mode supports HTTP/HTTPS proxying and the selected SOCKS mode.
`socks5: disabled` disables SOCKS; `tcp` enables SOCKS5 TCP.
The schema recognizes `tcp_udp`, but Console currently rejects it on every platform.

Limited mode permits plain HTTP GET, HEAD, and OPTIONS.
It rejects POST, HTTPS CONNECT, and SOCKS; every explicitly supplied `socks5` field is invalid in limited mode, even `disabled`.
It is not read-only HTTPS, does not intercept TLS, and does not guarantee side-effect-free requests.

### Allow or block destinations {#destination-rules}

Fields under `sandbox.network.proxy.domains` or `resolver.sandbox.network.proxy.domains`:

| Field   | Accepted values                          | Meaning and default                                               |
| ------- | ---------------------------------------- | ----------------------------------------------------------------- |
| `allow` | List of hostname patterns or IP literals | Permitted destinations. Default within an explicit mapping: `[]`. |
| `deny`  | List of hostname patterns or IP literals | Blocked destinations; matching deny rules win. Default: `[]`.     |

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

The `sandbox.network.sockets` and `resolver.sandbox.network.sockets` mappings each have one field, `unix_sockets`:

| Value                                                      | Direct Unix-socket access                                                                      |
| ---------------------------------------------------------- | ---------------------------------------------------------------------------------------------- |
| `[]` (default)                                             | No Unix-socket grants.                                                                         |
| List of absolute path strings, after leading-`~` expansion | Grant the named sockets. Supported on macOS; nonempty lists are rejected on Linux and Windows. |
| `dangerously_allow_all`                                    | Permit all Unix sockets. Supported on macOS and Linux; rejected on Windows.                    |

There are no deny lists or globs.
Native macOS direct-socket grants use subpath matching, so this is not an exact-path firewall across every socket route.
Filesystem permissions still apply.

`sandbox.network.allow_local_binding` and `resolver.sandbox.network.allow_local_binding` accept:

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
Each accepts one of the following strings:

| `tempdir`           | Behavior                                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `managed` (default) | Create private storage, grant writes there, redirect temporary environment variables, and remove the storage after retirement.                                                         |
| `read_write`        | Preserve configured/inherited temporary environment variables and grant writes to their host locations and the platform default temp location. Console does not own those directories. |
| `read_only`         | Preserve temporary environment variables, create no managed storage, and add no temporary write grants.                                                                                |

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

The `sandbox.profile.inherit_codex` mapping has one option:

| Field      | Accepted values          | Default                                   |
| ---------- | ------------------------ | ----------------------------------------- |
| `fallback` | `read_only`, `workspace` | `read_only` when metadata is unavailable. |

Omitting `fallback`, including with `profile: inherit_codex` or `profile: {inherit_codex: {}}`, selects `read_only` when inheritance is unavailable.
Unknown profiles, unknown options, and nested fallbacks are errors.
A failure to prepare runtimes or start the selected sandbox is an error; it does not select the fallback.
The standalone `sandbox` command has no client metadata, so it silently uses the configured fallback, defaulting to `read_only`.
`config resolve` preserves the selection policy and automatic cache choice without selecting a runtime profile or preparing interpreters.
Broad-root checks and their warnings apply at launch, so resolving configuration from a broad root does not replace the configured workspace permissions.

In R, use `SandboxPolicy(profile = "inherit_codex")` or `SandboxPolicy(profile = InheritCodexSandboxProfile(fallback = "workspace"))`.

### Metadata and permission translation

When the `inherit_codex` profile is configured, Console advertises the experimental `codex/sandbox-state` capability and its older `codex/sandbox-state-meta` alias.
It reads that key from the first `send` call's `_meta`, before runtime discovery, dependency preparation, or worker startup.
Initialization, tool discovery, and pings remain available while waiting for that call.
Metadata belongs to the MCP request, outside the tool's `arguments`.

Console translates the current `permissionProfile` and uses `sandboxCwd` to resolve symbolic project roots:

| Client permissions                 | Console behavior                                                                                                                                |
| ---------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------- |
| Managed, restricted filesystem     | Use recognized read, write, and deny entries as the base, including additional writable roots.                                                  |
| Managed, unrestricted filesystem   | Permit host filesystem access, composing explicit local filesystem rules, while retaining Console's native supervision and selected networking. |
| External filesystem enforcement    | Use unrestricted filesystem access within Console's runner; Console does not verify the external boundary.                                      |
| Disabled enforcement (full access) | Disable Console's worker and preparation sandboxes, as with `--no-sandbox`.                                                                     |
| Restricted or enabled network      | Apply that worker network mode unless `sandbox.network` is explicitly configured.                                                               |

Concrete paths accept absolute native paths or local `file:` URIs.
Symbolic project roots use the client's sandbox working directory, which can differ from Console's launch directory; inheritance does not change the worker's working directory.
Symbolic subpaths must be relative and cannot contain parent-directory components, drive prefixes, or NUL characters.
Root, minimal, and temporary-path entries use Console's corresponding native permissions.
Existing local grants and denials retain their normal specificity and conflict precedence.
Console's host-read baseline and temporary-storage setting remain in effect for restricted profiles.
This translates permissions rather than reproducing every Codex restriction.

The current Codex metadata does not contain proxy endpoints, destination rules, credentials, or proxy approval settings.
Console retains an explicitly configured managed proxy, including its destination rules and `allow_upstream_proxy` choice.
Proxy environment variables passed to the server follow the ordinary environment-inheritance rules; metadata alone cannot reconstruct the client's proxy.
Dependency preparation retains its separate resolver policy unless inherited full access disables sandboxing.
Full access defaults to host caches; managed caches, including custom cache directories, require sandboxing.
Inherited full access disables enforcement of local filesystem and network restrictions too.

Missing metadata, an unsupported client profile, an invalid profile shape, or a malformed or unsupported filesystem entry makes the whole `inherit_codex` profile unavailable.
Console does not drop permission entries individually: omitting a denial could widen access.
Unknown additional fields and client backend or launcher selections are ignored.
Console always uses its own pinned runner; metadata cannot select an executable or change its backend.
Supported translated permissions still undergo ordinary native validation.

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

Normal sessions, standalone commands, and complete native policies require MXC.
Unsupported hosts and policies fail before the workload executes; there is no backend or host-execution fallback.
Explicit `serve --no-sandbox` remains available.

The host must support native process security environments and the requested filesystem policy; read-deny rules need native deny-path support.
The runner probes actual creation and closure, not just exported API names.
Update Windows if that probe fails.

MXC requires no account provisioning, UAC setup, or persistent state directory.
Only fresh hosts are supported.
Previously provisioned hosts require independent removal of legacy Console sandbox accounts, stored account credentials, and persistent firewall rules before use.
MXC runs as the host identity, so a workload can decrypt readable DPAPI-protected legacy passwords.
The runner does not detect or clean up legacy state.

Workload environment settings apply after native setup, so clearing inheritance does not remove the launcher's Windows platform variables.
Configured temporary-directory values reach the workload unchanged.
Inherited Windows shell metadata whose names start with `=`, such as Command Prompt's `=ExitCode`, is omitted from the workload environment.
Explicitly configured names containing `=` remain invalid.
Working directories can contain Windows 8.3 aliases, including aliases in the system temporary path.

Dependency preparation uses a separate MXC policy with host reads, dependency-cache and private temporary writes, and a managed download proxy by default.
`resolver.sandbox` configures those permissions independently of evaluated code; see [Package preparation](RESOLVER.md).
Native setup or launch failure stops preparation without falling back to host execution.
Package downloads, installation hooks, and cache writes use these resolver permissions.

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

Console guarantees temporary-storage cleanup only with an active sandbox and managed storage.
Other modes use normal runtime cleanup without an added Console guarantee; see [Temporary storage](#temporary-storage) and [Run without the sandbox](#run-without-the-sandbox).
Complete native unrestricted and external modes also have different guarantees, described below.

## Supply a complete native policy {#explicit-complete-policy}

Advanced standalone callers can use `sandbox --config-env NAME` with a **complete native JSON policy**.
This bypasses automatic configuration and all generated Console policy defaults, including private temporary-storage and application-extension additions.

```sh
SANDBOX_POLICY='{"filesystem":{"kind":"restricted","entries":[{"path":{"type":"special","value":{"kind":"root"}},"access":"read"}]},"network":"restricted"}' \
  mcp-console sandbox --config-env SANDBOX_POLICY -- /bin/echo 'literal argument'
```

The variable holds JSON, not a filename.
The runner consumes it; stdin belongs to the target.
This mode rejects `-c`, discovery options, and writable-root composition.
Use a child-specific environment rather than mutating a multithreaded parent's global environment.

The [pinned runner schema](https://github.com/t-kalinowski/cobox/blob/bad761ee97eb54caf302d6d4261ef494f686bf12/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) defines native fields; its private protocol is unversioned.
The native runner enforces the Windows MXC requirement at this boundary.
Complete policies use the native boolean `proxy.allowLocalBinding` field; the public `auto` selector belongs to the shared YAML schema above.
Runnable examples are provided in [shell](../examples/sandbox-config.sh), [Python](../examples/sandbox-config.py), and [R](../examples/sandbox-config.R).

Complete policies can request OS-specific options unavailable in `config.yaml`.
Environment/argument size limits still apply: a nominally valid JSON payload can exceed native process-launch limits.
The internal bootstrap-descriptor transport belongs to the pinned protocol, not this public YAML reference.

### Filesystem and enforcement modes

These **complete-native-policy** kinds are not `config.yaml` permission groups:

| Kind               | Meaning without a proxy                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------------------------- |
| `restricted`       | Native filesystem rules, selected networking, and native supervision.                                    |
| `unrestricted`     | Full OS-permitted filesystem access; entries do not narrow it. Native supervision/network policy remain. |
| `external-sandbox` | Delegate filesystem and networking to an outer boundary. The runner does not create or verify it.        |

In external mode without a proxy, `network: restricted` installs no network restriction.
Console rejects external mode on Windows, where every sandboxed launch must use MXC.
Linux then supervises the original group/direct child; the outer boundary must retire escaped descendants.
A managed proxy can add native networking while filesystem enforcement remains delegated.

Full filesystem writes can influence unsandboxed processes and shared artifacts.
Restricted-policy isolation guarantees do not extend to hostile unrestricted workloads.
Cleanup is not proof of isolation.
