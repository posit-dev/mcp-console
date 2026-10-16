# Windows local execution

Native Windows x64 support is experimental.
Local sandboxed and `--no-sandbox` sessions support R, Python, SQL, persistent state, input, plots, interruption, restart, and recording.
Either R or Python can run without the other; reticulate bridges them when both are available.

PyPI provides a prebuilt Windows x64 wheel.
Windows dependency preparation uses host permissions; process cleanup through Jobs is not sandboxing.

## Install and run

Install Python 3.11+, uv, and the [Microsoft Visual C++ v14 Redistributable for x64](https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist).
R is optional for Python-only execution.
For R, use an x64 installation selected through public `r` configuration, `R_HOME`, or PATH.

From PowerShell:

```powershell
uv tool install mcp-console
mcp-console serve
```

`serve` waits for MCP traffic; it is not an interactive prompt.
Configure your client with command `mcp-console` and arguments `["serve"]`.

To select preinstalled Python:

```yaml
python: C:/project/.venv
```

Use a real interpreter/venv, not a Windows App Execution Alias.
Install any third-party packages your analysis needs; standard-library Python needs no additional packages.
Without an explicit selection, PATH uv prepares managed Python.
Shared [requirements](REQUIREMENTS.md), [configuration](CONFIGURATION.md), and [recording](RECORDING.md) rules apply.

Sandboxed sessions default to Console caches under `%LOCALAPPDATA%/mcp-console/cache/dependencies`, subject to the [documented overrides](RESOLVER.md#cache-locations).
Redirecting caches does not sandbox preparation.
There is no automatic fallback to host execution when MXC is unavailable or rejects the policy.

### Build from source

See [Windows build instructions](WINDOWS_DEVELOPMENT.md#build-from-source).

## Native sandbox

Sandboxed Windows execution requires MXC, including normal sessions, the public `sandbox` command, and complete native policies.
The host must support native process security environments and the requested filesystem policy; read-deny rules need native deny-path support.
The runner probes actual creation and closure, not just exported API names.
Update Windows if that probe fails; unsupported hosts and policies fail before the workload executes.

MXC requires no account provisioning, UAC setup, or persistent state directory.
Workload environment settings apply after native setup, so clearing inheritance does not remove the launcher's Windows platform variables.
Configured temporary-directory values reach the workload unchanged.
Windows complete policies also reject `filesystem.kind: external-sandbox`, which could bypass native enforcement.
Explicit `serve --no-sandbox` remains available.

The runner enforces filesystem/network permissions and supplies private `TMPDIR`, `TEMP`, and `TMP` in the default [managed temporary-storage mode](SANDBOX_CONFIGURATION.md#profiles-and-temporary-storage).
Working directories can contain Windows 8.3 aliases, including aliases in the system temporary path.
Workspace permissions use the common [sandbox schema](SANDBOX_CONFIGURATION.md).

MXC supports managed HTTP/HTTPS and SOCKS5 TCP proxies:

```yaml
sandbox:
  network:
    proxy:
      domains:
        allow: [api.example.com]
```

`allow_local_binding: auto` is the default; omitted and null values also select auto.
For a Windows proxy, auto resolves to true because native host-loopback access is bidirectional.
Explicit false is rejected; it is never silently widened.
Use proxy-free `network: restricted` to prohibit local binding.
UDP and Unix-socket settings remain unsupported.

**Dependency preparation still runs with host permissions.** Explicit `resolver.sandbox` policies remain rejected on Windows.
Resolver environment/cache settings remain usable, but package downloads, installation hooks, and cache writes are outside MXC.
Moving that preparation into MXC is separate work.

## Lifecycle and platform differences

The native runner owns a non-breakaway Job and confirms zero active processes before successful retirement.
Private-storage removal follows that confirmation; cleanup failure retains storage and blocks replacement.
Forced frontend exit is not a cleanup receipt, and runner death does not guarantee storage deletion.

Interruption is cooperative.
R/Python callbacks can handle it without losing state, but an unresponsive native call may need restart.
R-owned DuckDB supports native query interruption; long native Python-owned DuckDB queries can require restart on Windows.

Direct execution reaps its direct worker but does not guarantee descendant cleanup.

## Validation

See [Windows validation](WINDOWS_DEVELOPMENT.md#validation) for contributor checks.

<span id="testing-parity-and-remaining-gaps"></span>

[Testing parity and remaining gaps](WINDOWS_DEVELOPMENT.md#testing-parity-and-remaining-gaps) tracks platform coverage.

## Troubleshooting

An unavailable-MXC error requires a supported Windows host and policy.

Windows process-creation error 4551 indicates host application-control policy.
The host must permit the executable and downloaded interpreter DLLs; this is separate from Console's sandbox settings.
