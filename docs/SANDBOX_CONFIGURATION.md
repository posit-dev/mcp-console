# Sandbox configuration

The public schema applies to both `sandbox` (evaluated code) and `resolver.sandbox` (dependency preparation).
Their permissions are independent.
[Configuration](CONFIGURATION.md) defines discovery and merge rules.

To permit workspace writes and one API host:

```yaml
sandbox:
  filesystem:
    read_write: [.]
  network:
    proxy:
      domains:
        allow: [api.example.com]
```

Configuration is trusted and can widen permissions.
Read [Sandbox and trust](SANDBOX.md) before granting access.

Set either whole permission node to `null` to clear inherited settings and restore that context's built-in defaults.
This does not disable sandboxing; `serve --no-sandbox` disables enforcement after both permission nodes have been omitted or cleared.
Child nodes such as `filesystem` and `network.proxy` still require their documented types.

## Profiles and temporary storage

```yaml
sandbox:
  profile: workspace
  tempdir: managed
```

`profile: workspace` selects the native `:workspace` profile for the captured launch directory.
Omitting `profile` retains the host-read baseline without workspace writes.
Explicit filesystem entries compose with the profile; network selection remains independent.
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

The [pinned runner schema](https://github.com/t-kalinowski/cobox/blob/47221a5dbcac6f793047ce416937f6db2bb2d75f/codex-rs/mcp-console-sandbox/PROTOCOL.md#complete-json-reference) defines native fields; its private protocol is unversioned.
Console still enforces its Windows MXC requirement at this boundary and rejects retired Windows controls.
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
The removed `windows_backend`, `windows_sandbox_level`, and `windows_state_dir` controls are rejected, including explicit nulls.
Unsupported hosts and policies fail before the workload executes; there is no backend or host-execution fallback.
Existing legacy Console accounts block MXC until an administrator completes the [one-time migration](WINDOWS.md#legacy-installation-cleanup).
Explicit `serve --no-sandbox` remains available.
See [Windows](WINDOWS.md#native-sandbox) for host requirements and the separate host-permission dependency-preparation boundary.

## Explicit Linux backend selection

The native default is bubblewrap.
Namespace failure does not trigger a backend switch or unsandboxed retry.
The inherited-procfs alternative keeps the same backend and policy.
Standalone `linux_backend: landlock` has been removed and is rejected.
See [Linux compatibility](LINUX_COMPATIBILITY.md#policy-and-backend-contract).
