# Sandbox and trust

The code an agent runs can read files and launch programs.
The sandbox limits that access; it does not make arbitrary projects, dependencies, or recorded output safe.

## Default permissions

The worker can read host files, write private temporary storage, and run child processes.
Workspace writes and direct networking are restricted.
**Host-file reads include readable secrets.** For ordinary project writes, use the [workspace profile](SANDBOX_CONFIGURATION.md#workspace-profile), which keeps workspace metadata read-only:

```yaml
sandbox:
  profile: workspace
```

Writable workspace paths are persistent user data; Console does not delete them during cleanup.
With the default `sandbox.tempdir: managed`, private temporary storage belongs to the worker lifetime.
The `read_write` and `read_only` modes retain normal host temporary-directory selection; see [temporary storage](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage).

Project configuration is trusted launcher input.
It can choose executables, supply credentials, and widen permissions; global configuration is not a mandatory ceiling.
Use `--no-project-config` when opening an unfamiliar project until its configuration has been authorized.

Dependency preparation uses a separate policy.
On macOS/Linux it normally has host reads, dependency-cache writes, and approved downloads.
Windows preparation and `--no-sandbox` retain host permissions.
Package installation and build hooks remain trusted code.
See [Resolver configuration](RESOLVER.md).

## Application policy and launch

Setup failures do not trigger an automatic retry with weaker enforcement or without a sandbox.
See [Architecture](ARCHITECTURE.md#application-policy-and-launch) for launch and verification ownership.

## Supported hosts and lifetime limits

**macOS:** Seatbelt enforces permissions.
Cleanup covers the owned process group and detached descendants the runner observes.
A descendant that detaches and becomes orphaned before observation may escape discovery.
General shell job suspension/resumption is unsupported.

**Linux:** Bubblewrap provides namespace-based enforcement and retirement.
Namespace setup must be permitted by the host.
Fresh procfs is optional; inherited procfs can expose host PIDs and readable metadata.
A stopped namespace init can exceed cleanup deadlines when emergency termination facilities are unavailable.
See [Linux compatibility](LINUX_COMPATIBILITY.md).

**Windows x64:** Experimental native execution requires MXC for every sandboxed launch.
Unsupported hosts and policies fail before executing the workload.
The runner confirms Job retirement before successful exit and private-storage removal.
See [Windows requirements](WINDOWS.md#native-sandbox).

Caller death triggers cleanup while the runner remains alive.
There is no independent recovery process.
Runner death, a stopped/hung runner, or a forced frontend kill does not guarantee descendant termination, temporary-storage deletion, or terminal restoration.
Surviving sandboxed processes retain their native enforcement.

Cleanup failures are errors.
Unconfirmed retirement retains private storage and blocks replacement rather than being reported as success.
Closing pipes or reaping the direct worker does not prove native descendant cleanup.

`--no-sandbox` removes native enforcement and native descendant cleanup.
It also omits general-purpose managed storage and temporary-environment redirection.
Console guarantees temporary-storage cleanup only with an active sandbox and managed storage.
Other modes use normal runtime cleanup without an added Console guarantee; R performs best-effort session-tempdir cleanup.
Native Python DuckDB retains its normal storage defaults and lifetime, including persistent secrets.
Normal relay shutdown still reaps its direct worker.
Complete native unrestricted/external modes have different guarantees; see [enforcement modes](SANDBOX_CONFIGURATION.md#filesystem-and-enforcement-modes).

## Policy extensions and compatibility

See [Architecture](ARCHITECTURE.md#policy-extensions-and-compatibility) for maintenance and evidence requirements.
