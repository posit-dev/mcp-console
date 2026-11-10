# Windows development and validation

Use [client setup](CLIENTS.md) and [sandbox permissions](SANDBOX_CONFIGURATION.md#windows-mxc-requirement) for runtime installation.
This page covers contributor builds and native tests.

## Build from source

Install Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
From the repository root:

```powershell
scripts/with-checkout.cmd uv tool install --reinstall .
```

Packaging stages the pinned companion automatically.
Direct Cargo builds require `scripts/stage-sandbox-runner.cmd` first and `scripts/with-checkout.cmd` around mutating build commands.
Console and its `cc`-built native dependencies use the static MSVC runtime selected by the repository's Cargo configuration.

## Validation

Use Python 3.14 to match CI.
Full coverage needs R/Python, PATH uv/ir, repository access, prepared packages, and an MXC-capable host.
Acceptance probes MXC through a real public sandbox launch.
Only the native unavailable-runtime diagnostic permits skipping execution cases; installation, policy, and workload failures remain failures.
Hosts without MXC still test configuration, bundle integrity, explicit host execution, and fail-closed startup.

```powershell
$env:LC_ALL = 'C'
scripts/preflight.cmd
scripts/stage-sandbox-runner.cmd
scripts/test.cmd --list
$env:MCP_CONSOLE_TEST_REQUIRE_MXC = '1'
scripts/test.cmd WindowsConsole.test_python_without_r
scripts/test.cmd WindowsPreparation
scripts/test.cmd client_server/python/test_runtime
scripts/format.cmd
scripts/check.cmd
scripts/check.cmd --full
Remove-Item Env:MCP_CONSOLE_TEST_REQUIRE_MXC
```

Keep `MCP_CONSOLE_TEST_REQUIRE_MXC=1` for required native coverage so an unavailable host fails instead of skipping.
Set `R_HOME` when the suite needs the installed R location.
A successful `preflight` is an inventory, not build or sandbox acceptance.

`WindowsPreparation` exercises real package builds, cold caches, separate resolver policy, activation, failure, and cancellation.
Enforcement cases must observe actual denied filesystem/network effects.
Proxy environment variables alone are not evidence of enforcement.
Fresh install paths and firewall-rule checks prevent previous authorization from masking setup dependencies.

The ordinary gate runs native acceptance; `--full` adds applicable shared cases, tooling, wheel, and source-install checks.
See [Development](DEVELOPMENT.md#validation-ladder) and [Boundary tests](../tests/boundaries/README.md) for selection and snapshot rules.
CI runs `tests/windows_mxc.py` before the substantive suite on the same host.

For installed acceptance, set `MCP_CONSOLE_TEST_BINARY` to the public command.
Copied uv launchers also need `MCP_CONSOLE_TEST_NATIVE_BINARY` when fixtures require the actual executable or relocate its bundle.

### Testing parity and remaining gaps

Shared cases cover portable R/Python/SQL behavior alongside native Windows acceptance.
Unix namespaces, signals, PTYs, ELF, and procfs retain platform-specific tests.
Some shell, FIFO, and layout fixtures still exclude otherwise supported Windows behavior; native counterparts do not replace every skipped scenario.
Their remaining work belongs in [TODO](TODO.md#sandbox-and-platforms).

Use `scripts/test.cmd --full --list` and capability diagnostics to inspect current coverage.
A passing macOS case or skipped Windows case is not Windows validation.
Native probe fixtures can require `rustc` even when testing an installed Console.
MXC uses the caller's identity, including for runtimes outside standard roots.

## Source and lifecycle implementation

Keep embedded R sources in LF form as specified by `.gitattributes`.
For WSL use an independent Linux checkout and follow [WSL setup](DEVELOPMENT.md#windows-development-through-wsl2).

Windows pipes, events, process handles, runtime DLL selection, and interrupt translation belong in the platform source modules.
Public MCP message shapes stay shared; [Architecture](ARCHITECTURE.md) and the private protocol guides describe common ownership.
