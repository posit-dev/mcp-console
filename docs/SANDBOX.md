# Sandbox and trust

Console evaluates code with shell-class capability.
Native sandboxing narrows that capability; it does not make arbitrary projects, dependencies, or recorded output safe.

## Default permissions

The worker can read host files, write private temporary storage, and run child processes.
Workspace writes and direct networking are restricted.
**Host-file reads include readable secrets.** Add permissions deliberately through [sandbox configuration](SANDBOX_CONFIGURATION.md).

```yaml
sandbox:
  filesystem:
    read_write: [.]
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

Console selects policy and verifies its installed, pinned native runner.
The runner owns OS enforcement, private storage, signals/terminals, and descendant retirement.
The relay owns only its direct worker, not a second process-tree supervisor.

```text
MCP server → sandbox frontend → native runner → relay → worker
                                              or standalone command
```

There is no runtime runner download or automatic retry with weaker enforcement.
[Architecture](ARCHITECTURE.md) describes component ownership; [Release](../RELEASE.md#private-sandbox-executable) describes bundle verification.

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
Existing legacy Console accounts require the documented one-time migration; launch does not remove them or select another backend.
The runner confirms Job retirement before successful exit and private-storage removal.
See [Windows requirements and migration](WINDOWS.md#native-sandbox).

Caller death triggers cleanup while the runner remains alive.
There is no independent recovery process.
Runner death, a stopped/hung runner, or a forced frontend kill does not guarantee descendant termination, temporary-storage deletion, or terminal restoration.
Surviving sandboxed processes retain their native enforcement.

Cleanup failures are errors.
Unconfirmed retirement retains private storage and blocks replacement rather than being reported as success.
Closing pipes or reaping the direct worker does not prove native descendant cleanup.

`--no-sandbox` removes native enforcement and native descendant cleanup.
It also omits general-purpose managed storage and temporary-environment redirection.
R retains its session-tempdir cleanup on graceful shutdown; native Python SQL scratch is removed after worker retirement.
Normal relay shutdown still reaps its direct worker.
Complete native unrestricted/external modes have different guarantees; see [enforcement modes](SANDBOX_CONFIGURATION.md#filesystem-and-enforcement-modes).

## Policy extensions and compatibility

The macOS extension in [`src/sandbox/policy_extensions.sbpl`](../src/sandbox/policy_extensions.sbpl) contains Console-specific allowances and their evidence.
Host terminal reads and mutating terminal ioctls remain denied, while sandbox-created PTYs support interactive children.

For maintenance, distinguish a rule duplicated by the pinned runner from one genuinely unnecessary for the workload.
Removing an allowance needs evidence from the actual sandboxed workflow and relevant platform/runtime versions.
Passing host-resolver tests is not worker-sandbox evidence.
Historical observations and uncertain callers belong beside the rule in source, not as asserted public guarantees.

The remaining exception-evidence review is listed in [TODO](TODO.md#sandbox-and-platforms).
