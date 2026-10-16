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
Its packages must already be installed.
Without an explicit selection, PATH uv prepares managed Python.
Shared [requirements](REQUIREMENTS.md), [configuration](CONFIGURATION.md), and [recording](RECORDING.md) rules apply.

Sandboxed sessions default to Console caches under `%LOCALAPPDATA%/mcp-console/cache/dependencies`, subject to the [documented overrides](RESOLVER.md#cache-locations).
Redirecting caches does not sandbox preparation.
There is no automatic fallback to host execution when MXC is unavailable or rejects the policy.

### Build from source

Source builds additionally need Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
From the repository root, run `uv tool install --reinstall .`.
Source packaging stages the pinned companion.
Direct Cargo builds need `scripts/stage-sandbox-runner.cmd` first.
Use `scripts/with-checkout.cmd` for direct mutating build commands.

## Native sandbox

Sandboxed Windows execution requires MXC, including normal sessions, the public `sandbox` command, and complete native policies.
The host must support native process security environments and the requested filesystem policy; read-deny rules need native deny-path support.
The runner probes actual creation and closure, not just exported API names.
Update Windows if that probe fails; unsupported hosts and policies fail before the workload executes.

MXC requires no account provisioning, UAC setup, or persistent state directory.
Native Windows process creation requires `LOCALAPPDATA` to be defined, even if empty; retain it when disabling environment inheritance.
Windows complete policies also reject `filesystem.kind: external-sandbox`, which could bypass native enforcement.
Explicit `serve --no-sandbox` remains available.

The runner enforces filesystem/network permissions and supplies private `TMPDIR`, `TEMP`, and `TMP`.
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

Windows uses native pipes, events, and process handles rather than Unix signals/descriptors.
Those implementation choices do not create different public MCP message shapes.
Direct execution reaps its direct worker but does not guarantee descendant cleanup.

## Validation

Use Python 3.13 to match CI.
Full coverage needs R/Python, PATH uv/ir, package repository access, prepared dependencies, and an MXC-capable host.
CI runs `scripts/with-checkout.cmd python tests/windows_mxc.py -v` after building Console, then the substantive Windows suite on the same host.
Acceptance probes MXC through a real public sandbox launch.
Only the native unavailable-runtime diagnostic permits skipping MXC execution cases; installation, policy, and workload failures remain failures.
Hosts without MXC still run configuration, integrity, explicit host-execution, and public fail-closed startup tests.
As hosted runners gain native MXC support, the existing execution cases run automatically.
The R package's permission tests use the same capability rule; adapter and configuration tests use explicit host execution where enforcement is outside their scope.
Controlled origins verify allowed and denied HTTP, CONNECT, and SOCKS requests through the managed proxy.
Sessions, sandbox commands, and complete policies verify actual unavailable-MXC rejection on unsupported hosts; capable hosts inject that failure into the native probe.

```powershell
$env:LC_ALL = 'C'
scripts/preflight.cmd
scripts/stage-sandbox-runner.cmd
scripts/test.cmd --list
scripts/test.cmd WindowsConsole.test_python_without_r
scripts/test.cmd client_server/python/test_runtime
# Require native coverage on a local MXC-capable host:
$env:MCP_CONSOLE_TEST_REQUIRE_MXC = '1'
scripts/format.cmd
scripts/check.cmd
scripts/check.cmd --full
Remove-Item Env:MCP_CONSOLE_TEST_REQUIRE_MXC
```

Set `R_HOME` to the installed R directory when the complete suite requires it.
`preflight` is read-only; successful inventory is not build or sandbox acceptance.
Packaging stages its companion automatically, while direct checks need explicit staging.

The ordinary gate runs native Windows acceptance.
The full gate adds applicable shared boundary cases, tooling, wheel, and source-install checks.
Shared selectors and snapshot rules are documented in [Development](DEVELOPMENT.md#validation-ladder) and [Boundary tests](../tests/boundaries/README.md).

For installed acceptance, `MCP_CONSOLE_TEST_BINARY` selects the installed public command.
A copied uv launcher also needs `MCP_CONSOLE_TEST_NATIVE_BINARY` for fixtures requiring the actual native executable or bundle relocation.

### Testing parity and remaining gaps

The shared suite runs portable R/Python/SQL behavior in addition to Windows-specific tests.
Unix namespace, signal, PTY, ELF, and procfs tests remain platform-specific.
Some shell/FIFO/layout fixtures still exclude Windows even when the underlying behavior is supported; native counterparts do not replace every skipped scenario.
Port those fixtures before removing capability restrictions.

Use `scripts/test.cmd --full --list` and skip diagnostics to inspect coverage.
A passing macOS equivalent or skipped Windows case is not Windows validation.
[TODO](TODO.md#sandbox-and-platforms) collects this fixture debt.

CI installs the trusted runtimes before acceptance.
MXC runs with the caller's identity, including for installations outside standard roots.

## Troubleshooting

An unavailable-MXC error requires a supported Windows host and policy.

Windows process-creation error 4551 indicates host application-control policy.
The host must permit the executable and downloaded interpreter DLLs; this is separate from Console's sandbox settings.

Keep embedded R sources in LF form as specified by `.gitattributes`.
For Linux workflows under WSL, use an independent Linux checkout and follow [WSL development](DEVELOPMENT.md#windows-development-through-wsl2).
