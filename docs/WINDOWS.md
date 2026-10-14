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
There is no automatic fallback to host execution when sandbox setup fails.

### Build from source

Source builds additionally need Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
From the repository root, run `uv tool install --reinstall .`.
Source packaging stages the pinned companion and native helpers.
Direct Cargo builds need `scripts/stage-sandbox-runner.cmd` first.
Use `scripts/with-checkout.cmd` for direct mutating build commands.

## Native sandbox

Ordinary `config.yaml` launches use native `auto` selection, preferring MXC when the host and policy support it.
MXC needs no setup.
Otherwise, `auto` selects the elevated backend, which requires explicitly provisioned Console resources.
Existing Console sandbox accounts keep `auto` on elevated: their credential locations are not registered for MXC protection.
Explicit backend selections and launch failures never trigger a retry through another backend.

For elevated execution, `mcp-console sandbox-setup` explicitly provisions or refreshes Console's resources through UAC.
Run it interactively, then use `mcp-console sandbox-setup --status` to inspect readiness.
Elevated launches fail with setup guidance if provisioning is missing or outdated; they do not provision accounts automatically.
Startup also fails during setup if an existing deny ACL prevents granting missing metadata access to the working directory.
An applicable metadata deny still fails setup when an allow is also present.
Existing sandbox-account read access, including a NULL DACL, avoids an extra metadata grant.

Setup creates or reuses:

- `McpConsoleSandboxOff` and `McpConsoleSandboxOn` local accounts, plus the `ConsoleSandboxUsers` group.
- Account-scoped Windows Firewall rules and WFP loopback filters.
- Protected setup, encrypted-credential, and helper storage beneath the state directory.

The default state directory is `%LOCALAPPDATA%\mcp-console`.
Use one stable directory per Windows user; accounts and network policy are machine resources, and ACL entries persist on filesystem objects.
`--status` reports readiness without provisioning; it is not a fresh firewall audit.

The runner enforces the selected networking and filesystem policy and supplies private `TMPDIR`, `TEMP`, and `TMP`.
Working directories can contain Windows 8.3 aliases, including aliases in the system temporary path.
Workspace permissions use the common [sandbox schema](SANDBOX_CONFIGURATION.md).

Complete native policies may set `windows_backend` to `auto`, `mxc`, `elevated`, or `unelevated`, or select another legacy state directory with `windows_state_dir`.
Unelevated execution requires enabled networking and host reads and rejects read-deny policy.
Those controls are not fields in the public YAML schema.

MXC supports managed HTTP/HTTPS and SOCKS5 TCP proxies with explicit local binding permission:

```yaml
sandbox:
  network:
    proxy:
      domains:
        allow: [api.example.com]
    allow_local_binding: true
```

MXC host-loopback access is bidirectional, so it cannot disable local binding while a proxy is enabled.
UDP, Unix sockets, and managed proxies on legacy backends are unsupported and rejected.
Explicit resolver sandbox policies remain unsupported.
Resolver environment/cache settings remain usable with host permissions.

## Lifecycle and platform differences

The native runner owns a non-breakaway Job and confirms zero active processes before successful retirement.
Private-storage removal follows that confirmation; cleanup failure retains storage and blocks replacement.
Forced frontend exit is not a cleanup receipt, and runner/helper death does not guarantee storage deletion.

Interruption is cooperative.
R/Python callbacks can handle it without losing state, but an unresponsive native call may need restart.
R-owned DuckDB supports native query interruption; long native Python-owned DuckDB queries can require restart on Windows.

Windows uses native pipes, events, and process handles rather than Unix signals/descriptors.
Those implementation choices do not create different public MCP message shapes.
Direct execution reaps its direct worker but does not guarantee descendant cleanup.

## Validation

Use Python 3.13 to match CI.
Full coverage needs R/Python, PATH uv/ir, package repository access, prepared dependencies, and an explicitly provisioned sandbox.
Native acceptance does not provision the machine itself.
CI runs `scripts/with-checkout.cmd python tests/windows_mxc.py -v` after building Console and before provisioning legacy sandbox accounts.
This separate acceptance check probes explicit MXC selection through the public sandbox command.
When MXC is available, it requires a successful proxy launch.
When the host cannot create its native security environment, it requires both explicit MXC rejection and automatic proxy rejection before workload execution.
Unexpected launch failures fail the check.
Hosted Windows CI does not establish successful MXC execution when its native environment is unavailable.
The remaining native suite runs after provisioning and retains the public proxy configuration checks.

```powershell
$env:LC_ALL = 'C'
scripts/preflight.cmd
scripts/stage-sandbox-runner.cmd
mcp-console sandbox-setup --status
scripts/test.cmd --list
scripts/test.cmd WindowsConsole.test_python_without_r
scripts/test.cmd client_server/python/test_runtime
scripts/format.cmd
scripts/check.cmd
scripts/check.cmd --full
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

CI prepares the sandbox explicitly before acceptance and may grant sandbox-account read access to installations outside standard roots.
Those are trusted host setup operations, not permission changes performed by tests or ordinary sessions.

## Troubleshooting

A missing/outdated setup error requires explicit `sandbox-setup`, not `--no-sandbox` as an automatic workaround.
Use the default state directory for ordinary application launches.

Windows process-creation error 4551 indicates host application-control policy.
The host must permit the executable and downloaded interpreter DLLs; this is separate from Console's sandbox settings.

Keep embedded R sources in LF form as specified by `.gitattributes`.
For Linux workflows under WSL, use an independent Linux checkout and follow [WSL development](DEVELOPMENT.md#windows-development-through-wsl2).
