# Windows development and validation

Contributor build and test procedures.
Installation and supported runtime behavior are in [Windows setup](WINDOWS.md).

## Build from source

Source builds additionally need Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
From the repository root, run `uv tool install --reinstall .`.
Source packaging stages the pinned companion.
Direct Cargo builds need `scripts/stage-sandbox-runner.cmd` first.
Use `scripts/with-checkout.cmd` for direct mutating build commands.

## Validation

Use Python 3.13 to match CI.
Full coverage needs R/Python, PATH uv/ir, package repository access, prepared dependencies, and an MXC-capable clean host.
CI runs `scripts/with-checkout.cmd python tests/windows_mxc.py -v` after building Console, then the substantive Windows suite on the same clean host.
Acceptance probes MXC through a real public sandbox launch.
Only the native unavailable-runtime diagnostic permits skipping MXC execution cases; installation, policy, and legacy-account failures remain failures.
Hosts without MXC still run configuration, integrity, migration-diagnostic, explicit host-execution, and public fail-closed startup tests.
As hosted runners gain native MXC support, the existing execution cases run automatically.
The R package's permission tests use the same capability rule; adapter and configuration tests use explicit host execution where enforcement is outside their scope.
CI never provisions legacy accounts or selects a different sandbox backend.
Controlled origins verify allowed and denied HTTP, CONNECT, and SOCKS requests through the managed proxy.
Process-local fault injection checks legacy-account diagnostics through sessions, sandbox commands, and complete policies without changing the host's resources.
The same public routes verify actual unavailable-MXC rejection on unsupported hosts; capable hosts inject that failure.
CI separately runs the administrator-only `tests/fixtures/windows_migration.ps1` acceptance fixture against the migration script.
It refuses existing Console WFP state, creates inert persistent filters, and verifies dry-run behavior, removal, idempotence, and preservation of unrelated provider state.

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
MXC runs with the caller's identity and needs no sandbox-account ACL grants, including for installations outside standard roots.

## Source and lifecycle implementation

Keep embedded R sources in LF form as specified by `.gitattributes`.
For Linux workflows under WSL, use an independent Linux checkout and follow [WSL development](DEVELOPMENT.md#windows-development-through-wsl2).

Windows uses native pipes, events, and process handles rather than Unix signals/descriptors.
Those implementation choices do not create different public MCP message shapes.
