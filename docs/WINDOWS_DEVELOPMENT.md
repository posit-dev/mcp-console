# Windows development and validation

Contributor build and test procedures.
Use the shared [installation instructions](getting-started.qmd#install) and [sandbox permissions](SANDBOX_CONFIGURATION.md#windows-mxc-requirement) for runtime setup.

## Build from source

Source builds additionally need Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
From the repository root, run `uv tool install --reinstall .`.
Source packaging stages the pinned companion.
Console's Cargo configuration enables static MSVC runtime linkage for native builds, wheels, and source installs.
Native C/C++ dependencies built through `cc` inherit the same runtime selection.
Direct Cargo builds need `scripts/stage-sandbox-runner.cmd` first.
Use `scripts/with-checkout.cmd` for direct mutating build commands.

## Validation

Use Python 3.14 to match CI.
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
# Require native coverage on a local MXC-capable host:
$env:MCP_CONSOLE_TEST_REQUIRE_MXC = '1'
scripts/test.cmd WindowsConsole.test_python_without_r
scripts/test.cmd WindowsPreparation
scripts/test.cmd client_server/python/test_runtime
scripts/format.cmd
scripts/check.cmd
scripts/check.cmd --full
Remove-Item Env:MCP_CONSOLE_TEST_REQUIRE_MXC
```

Set `R_HOME` to the installed R directory when the complete suite requires it.
`preflight` is read-only; successful inventory is not build or sandbox acceptance.
Packaging stages its companion automatically, while direct checks need explicit staging.

`WindowsPreparation` exercises cold resolver caches, real package builds and descendants, independent resolver policies, late additions and failures, and cancellation under MXC.
Its enforcement probes require actual denied filesystem and non-loopback network effects; proxy environment variables alone are insufficient.
Preparation and proxy regressions use a fresh installation path and inspect firewall application rules before and after execution, so authorization from earlier runs cannot mask a prompt or an administrative setup dependency.
Native sessions use temporary Console caches; worker-permission cases share a test-owned cache and explicitly select their required providers rather than depending on the caller's package state.
Keep `MCP_CONSOLE_TEST_REQUIRE_MXC=1` set for those native runs so unavailable MXC fails instead of skipping execution.

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

## Source and lifecycle implementation

Keep embedded R sources in LF form as specified by `.gitattributes`.
For Linux workflows under WSL, use an independent Linux checkout and follow [WSL development](DEVELOPMENT.md#windows-development-through-wsl2).

Windows uses native pipes, events, and process handles rather than Unix signals/descriptors.
Workers dispatch interpreter interrupts through the system UCRT's signal table, independently of Console's static CRT, so blocking Python calls and temporary native SQL handlers still wake.
The selected Python DLL's directory is searched first for its dependencies, including any runtime DLLs supplied by Python itself.
Those implementation choices do not create different public MCP message shapes.
