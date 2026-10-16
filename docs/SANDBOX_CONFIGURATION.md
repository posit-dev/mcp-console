# Sandbox configuration

To allow ordinary project writes while protecting workspace metadata:

```yaml
sandbox:
  profile: workspace
```

[Configuration reference](CONFIGURATION_REFERENCE.md#sandbox) lists every sandbox field and its accepted values.

For a narrower grant, use `sandbox.filesystem.read_write: [./output]` and create the directory first.
To permit a specific API through the managed proxy, configure networking separately:

```yaml
sandbox:
  network:
    proxy:
      domains:
        allow: [api.example.com]
```

A proxy allowlist does not permit every direct network channel.
Package downloads use a separate policy.
The public schema applies to `sandbox` for evaluated code and `resolver.sandbox` for dependency preparation; their permissions are independent.
`inherit_codex` is a session profile and is not accepted under `resolver.sandbox.profile`.
[Configuration](CONFIGURATION.md) defines discovery and merge rules.

Configuration is trusted and can widen permissions.
Read [Sandbox and trust](SANDBOX.md) before granting access.

Set either whole permission node to `null` to clear inherited settings and restore that context's built-in defaults.
This does not disable sandboxing; `serve --no-sandbox` disables enforcement after both permission nodes have been omitted or cleared.
Child nodes such as `filesystem` and `network.proxy` still require their documented types.

## Inherit permissions from Codex

```yaml
sandbox:
  profile: inherit_codex
```

The `inherit_codex` profile inherits usable sandbox metadata from the first `send` call.
If inheritance is unavailable, it uses the built-in `read_only` profile: host reads, private temporary writes, and restricted networking before explicit local settings are applied.
Missing metadata selects this fallback silently.
Malformed or unsupported metadata emits a warning on the server's standard error stream and selects the fallback.
Select this profile only when the MCP client is trusted to configure Console's sandbox, including disabling it.
Omitting `profile` or selecting `workspace` keeps existing behavior: client sandbox metadata has no effect.

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

The `inherit_codex` mapping accepts `fallback: read_only` or `fallback: workspace`.
Omitting `fallback`, including with `profile: inherit_codex` or `profile: {inherit_codex: {}}`, selects `read_only` when inheritance is unavailable.
Unknown profiles, unknown options, and nested fallbacks are errors.
A failure to prepare runtimes or start the selected sandbox is an error; it does not select the fallback.
The standalone `sandbox` command has no client metadata, so it silently uses the configured fallback, defaulting to `read_only`.
`config resolve` preserves the selection policy and automatic cache choice without selecting a runtime profile or preparing interpreters.

In R, use `SandboxPolicy(profile = "inherit_codex")` or `SandboxPolicy(profile = InheritCodexSandboxProfile(fallback = "workspace"))`.

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
Full access defaults to host caches; explicit `cache: console` remains invalid without sandboxing.
Inherited full access disables enforcement of local filesystem and network restrictions too.

Missing metadata, an unsupported client profile, an invalid profile shape, or a malformed or unsupported filesystem entry makes the whole `inherit_codex` profile unavailable.
Console does not drop permission entries individually: omitting a denial could widen access.
Unknown additional fields and client backend or launcher selections are ignored.
Console always uses its own pinned runner; metadata cannot select an executable or change its backend.
Supported translated permissions still undergo ordinary native validation.

The first received `send` captures the translated policy or fallback for the connection, even if that call is cancelled before execution.
Workers and explicit restarts retain that policy.
Later calls without metadata keep it; later calls with different permissions fail before submitting code, with a request to reconnect.
When a usable client policy was captured, later malformed metadata rejects the call and warns that reconnecting is needed to apply the fallback.
If the fallback is already active, later malformed metadata warns and the call continues under that same fallback.
Reconnect after changing the client's permissions so Console can prepare its runtimes under the new policy.

## Profiles and temporary storage

### Workspace profile

The `workspace` profile permits ordinary project writes while keeping workspace metadata read-only:

```yaml
sandbox:
  profile: workspace
```

The workspace is Console's launch directory, captured before evaluated code runs.
Changing the working directory from R or Python does not move the permission boundary.

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

Omitting both `profile` and explicit write grants retains the host-read baseline without workspace writes.
Explicit filesystem entries compose with the profile: narrower rules override ancestors, and at the same path deny wins over write, then read.
An explicit write at a protected metadata path or beneath it can therefore permit changes there; these defaults are not an unchangeable ceiling.
Network and temporary-storage selections remain independent.

### Temporary storage

```yaml
sandbox:
  profile: workspace
  tempdir: managed
```

The native profile's temporary-directory options are internal: `tempdir` is the only public selector for automatic temporary-storage grants.

| `tempdir`           | Behavior                                                                                                                                                                               |
| ------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `managed` (default) | Create private storage, grant writes there, redirect temporary environment variables, and remove the storage after retirement.                                                         |
| `read_write`        | Preserve configured/inherited temporary environment variables and grant writes to their host locations and the platform default temp location. Console does not own those directories. |
| `read_only`         | Preserve temporary environment variables, create no managed storage, and add no temporary write grants.                                                                                |

All three values are strings.
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

`serve` and ordinary `sandbox` use the same policy and defaults.
`--no-sandbox` uses normal runtime temporary-directory selection without general-purpose managed storage or temporary-environment redirection.
Console guarantees temporary-storage cleanup only with an active sandbox and `tempdir: managed`; the native runner retires that storage after the workload exits.
In other modes, Console adds no temporary-directory cleanup guarantees.
R runs its normal best-effort shutdown, including its session `Rtmp` cleanup.
On Unix that cleanup uses `rm` from the workload's `PATH` and can leave files when that command is unavailable or a subtree is not writable.
Native Python DuckDB retains its own `temp_directory` and `secret_directory` defaults outside managed mode, including the normal persistence of stored secrets.
Set those connection settings explicitly when their default locations need different filesystem permissions.
Caller-owned parent directories and unrelated files remain the caller's responsibility.

## Filesystem

```yaml
sandbox:
  filesystem:
    read_only: [./data]
    read_write: [.]
    deny: [./secrets]
```

Paths are concrete paths, not globs.
They resolve against the captured launch directory, not the configuration file.
They do not expand `~` or variables, create directories, grant parent directories, or erase symlink components.

Worker entries modify the host-read/private-write baseline.
Omitting both `profile` and `read_write` grants no workspace writes.
An explicit resolver filesystem instead **replaces** its default entries, including host reads and cache writes; see [resolver grants](RESOLVER.md#filesystem-grants).

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

## Network choices

| Value                   | Effect                                                 |
| ----------------------- | ------------------------------------------------------ |
| `network: restricted`   | Restricted native networking, without a managed proxy. |
| `network: enabled`      | Host networking; filesystem enforcement remains.       |
| `network: {proxy: {…}}` | Restricted native networking with a managed proxy.     |

Worker networking defaults to restricted without a proxy.
Resolver networking on macOS/Linux defaults to its [download proxy](RESOLVER.md#expanded-public-default).
Explicit scalar values also remove that generated resolver proxy.

A managed mapping requires a non-null `proxy` mapping.
There is no `network: full` shortcut or public proxy-enabled boolean.

## Proxy modes and destinations

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

Full mode is the default and supports HTTP/HTTPS proxying and the selected SOCKS mode.
`socks5` accepts `disabled`, `tcp` (default), or `tcp_udp` in the schema; **Console currently rejects `tcp_udp` on every platform**, as described below.

Limited mode permits plain HTTP GET, HEAD, and OPTIONS.
It rejects POST, HTTPS CONNECT, and SOCKS; every explicitly supplied `socks5` field is invalid in limited mode, even `disabled`.
It is not read-only HTTPS, does not intercept TLS, and does not guarantee side-effect-free requests.

Domain rules use hostnames, not URLs, paths, or host-and-port restrictions.
Exact names match that host only; `*.example.org` matches subdomains but not the apex, and `**.example.org` matches both.
Matching deny rules win.
Native IP-literal forms are also accepted.

An explicit `domains` mapping replaces the generated destination default; omitted allow/deny lists are empty.
Worker defaults allow no destinations; resolver defaults allow its package sources.
This does not override ordinary deep merging between configuration layers.
To remove an explicitly inherited field, clear and rebuild its containing mapping.

`allow_upstream_proxy` defaults to false.
When enabled, onward routing uses the trusted launch environment, not workload overrides; destination and method restrictions still apply.
The public format has no per-port rules, URL rules, credential injection, listener configuration, or per-request approval API.

## Unix sockets and local binding

```yaml
sandbox:
  network:
    proxy: {mode: full}
    sockets:
      unix_sockets: [/tmp/service.sock]
    allow_local_binding: true
```

`unix_sockets` accepts an absolute-path list or `dangerously_allow_all`.
An empty list is the default.
There are no deny lists or globs.
Native macOS direct-socket grants use subpath matching, so this is not an exact-path firewall across every socket route.
Filesystem permissions still apply.

`allow_local_binding` accepts `auto` (the default), `null`, `true`, or `false`.
Omission and explicit `null` select `auto`; a later `null` replaces an earlier boolean.
Console resolves it after normal file and CLI layering and shows the effective boolean in `config resolve --full`.
On Unix, `auto` preserves false for workers and true for resolvers, whose installers need coordination and DNS.
On Windows, `auto` enables local binding for a managed proxy because MXC host-loopback access is bidirectional.
Explicit `true` permits local binding; explicit `false` prohibits it and is rejected when the selected policy cannot honor it, including Windows managed proxies.
To keep local binding disabled on Windows, select proxy-free `network: restricted` instead.
It affects private-address checks and native local socket access, including macOS loopback traffic and DNS.
It does not expose a Linux network namespace to a host browser.

Direct connections allowed by socket/local permissions do not pass through the proxy's HTTP-method filter.
Limited mode therefore does not restrict every granted channel to GET/HEAD/OPTIONS.

## Native capabilities

| Platform | Important limits                                                                                                                                                               |
| -------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ |
| macOS    | Managed proxy supported. Console rejects `tcp_udp`; historical evidence records a native reply-routing issue.                                                                  |
| Linux    | Managed bridge is TCP-only. Nonempty Unix-socket path lists and `tcp_udp` are rejected. Socket allow-all is supported, but is not path-specific.                               |
| Windows  | MXC is required. TCP proxies resolve local binding to true under `auto`; explicit false is rejected. UDP, Unix sockets, and explicit `resolver.sandbox` settings are rejected. |

A common schema does not imply equal native capabilities.
Console rejects unsupported explicit requests rather than silently ignoring them.
[TODO](TODO.md#sandbox-and-platforms) records related upstream and platform work without promising parity.

## Explicit complete policy

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

The [pinned runner schema](https://github.com/t-kalinowski/cobox/blob/c32634c488a856c400e4118a2fc19821d9c0223a/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) defines native fields; its private protocol is unversioned.
The native runner enforces the Windows MXC requirement at this boundary.
Complete policies use the native boolean `proxy.allowLocalBinding` field; the public `auto` selector belongs to the shared YAML schema above.
Runnable examples are provided in [shell](../examples/sandbox-config.sh), [Python](../examples/sandbox-config.py), and [R](../examples/sandbox-config.R).

Complete policies can request OS-specific options unavailable in `config.yaml`.
Environment/argument size limits still apply: a nominally valid JSON payload can exceed native process-launch limits.
The internal bootstrap-descriptor transport belongs to the pinned protocol, not this public YAML reference.

## Filesystem and enforcement modes

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

## Windows MXC requirement

Normal sessions, standalone commands, and complete native policies require MXC.
Unsupported hosts and policies fail before the workload executes; there is no backend or host-execution fallback.
Explicit `serve --no-sandbox` remains available.
See [Windows](WINDOWS.md#native-sandbox) for host requirements and the separate host-permission dependency-preparation boundary.

## Explicit Linux backend selection

The native default is bubblewrap.
Namespace failure does not trigger a backend switch or unsandboxed retry.
The inherited-procfs alternative keeps the same backend and policy.
Standalone `linux_backend: landlock` has been removed and is rejected.
See [Linux compatibility](LINUX_COMPATIBILITY.md#policy-and-backend-contract).
