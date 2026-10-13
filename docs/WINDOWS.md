# Windows local execution

Native Windows x64 support is experimental.
Local sandboxed and `--no-sandbox` sessions support R, Python, SQL, persistent state, input, plots, interruption, restart, and recording.
Either R or Python can run without the other; reticulate bridges them when both are available.

The release workflow does not publish Windows wheels.
Build and install Console from source.
Windows dependency preparation uses host permissions; process cleanup through Jobs is not sandboxing.

## Build and run

Install Python 3.11+, uv, Git, Rust's MSVC toolchain, Visual Studio C++ build tools, the Windows SDK, and CMake.
R is optional for building and Python-only execution.
For R, use an x64 installation selected through public `r` configuration, `R_HOME`, or PATH.

From PowerShell in the repository root:

```powershell
uv tool install --reinstall .
mcp-console serve
```

Source packaging stages the pinned companion.
Direct Cargo builds need `scripts/stage-sandbox-runner.cmd` first.
Use `scripts/with-checkout.cmd` for direct mutating build commands.

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

## Native sandbox

Sandboxed Windows execution requires MXC, including normal sessions, the public `sandbox` command, and complete native policies.
The host must support native process security environments and the requested filesystem policy; read-deny rules need native deny-path support.
The runner probes actual creation and closure, not just exported API names.
Update Windows if that probe fails; there is no elevated, unelevated, AppContainer, or host-execution fallback.

MXC requires no account provisioning, UAC setup, persistent state directory, or legacy helper executables.
The Console frontend selects MXC at the runner boundary.
Removed `windows_backend`, `windows_sandbox_level`, and `windows_state_dir` fields are errors, including explicit nulls and complete native policies.
Windows complete policies also reject `filesystem.kind: external-sandbox`, which could bypass native enforcement.
Explicit `serve --no-sandbox` remains available.

The runner enforces filesystem/network permissions and supplies private `TMPDIR`, `TEMP`, and `TMP`.
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

## Legacy installation cleanup

The pinned runner refuses MXC while either `McpConsoleSandboxOff` or `McpConsoleSandboxOn` exists, even if disabled.
Old provisioning did not register every credential directory, and MXC uses the host identity, which could read those unknown copies.
Launch preserves this guard and never deletes machine resources.
`sandbox-setup` has been removed.

Migration is a one-time administrator operation for the entire machine.
Coordinate it with every Console user; close Console clients, runners, and any old setup process first.
Do not run old Console versions again during cleanup.
Record the two users' and `ConsoleSandboxUsers` group's SIDs **before deleting them**.
Inventory every old state directory, including custom locations and other Windows users' installations; the former default was `%LOCALAPPDATA%\mcp-console`.

1. In elevated PowerShell, inventory and disable the old accounts:

   ```powershell
   $legacyNames = @('McpConsoleSandboxOff', 'McpConsoleSandboxOn')
   $legacyUsers = @(Get-LocalUser | Where-Object Name -In $legacyNames)
   $legacyGroup = @(Get-LocalGroup | Where-Object Name -EQ 'ConsoleSandboxUsers')
   $legacyUsers | Select-Object Name, SID, Enabled
   $legacyGroup | Select-Object Name, SID
   $legacyUsers | Disable-LocalUser
   ```

   Save the displayed SIDs in a migration record outside the old state directories.
   From each state directory also retain the `cap_sid` file: its `workspace`, `readonly`, `workspace_by_cwd`, and `writable_root_by_path` values identify persistent capability ACEs.
   Older files may contain a single SID instead of JSON.
   Do not print or copy `.sandbox-secrets/sandbox_users.json` into that record.

2. Reboot before removing protections.
   Disabling accounts alone does not invalidate existing process or impersonation tokens.
   After reboot, verify the accounts remain disabled and their SIDs match the record.
   Stop if identities changed or an old setup/runner has restarted.

3. Back up ACLs on every affected local filesystem before editing them.
   Include prior workspaces, writable/denied paths, runtime installations, cache directories, and their inherited children.
   Old setup had no complete grant registry: `cap_sid` path maps and setup logs help locate roots but are not a complete inventory.
   Scan the relevant local volumes for **each saved account, group, and capability SID**:

   ```powershell
   # Set these from the reviewed migration record; retain and inspect all output.
   $aclRoot = 'C:\path\to\reviewed-root'
   $savedSid = 'S-1-5-21-REPLACE-WITH-RECORDED-SID'
   icacls $aclRoot /save 'C:\migration\before.acl' /T /C /L
   icacls $aclRoot /findsid "*$savedSid" /T /C /L
   icacls $aclRoot /remove "*$savedSid" /T /C /L
   icacls $aclRoot /findsid "*$savedSid" /T /C /L
   ```

   Use a separate backup per root.
   Remove only the recorded SIDs, including allow and deny ACEs, at their inheritance sources.
   Do not reset whole ACLs, take ownership of an entire volume, remove every unknown SID, or remove Codex/AppContainer principals.
   Any inaccessible objects or remaining matches require administrator investigation; do not report complete ACL cleanup until resolved.
   If the original grant inventory cannot be reconstructed, use a known clean host or restore reviewed ACL backups.

4. Remove the disabled accounts, their profiles, and the group using the saved identities.
   Remove only `Win32_UserProfile` instances whose SID matches one of the two saved account SIDs, using `Remove-CimInstance`; verify they are not loaded first.
   Use `Remove-LocalUser -Name` for the two fixed names and `Remove-LocalGroup -Name ConsoleSandboxUsers` after checking the current SIDs again.
   This invalidates copied account credentials, including copies whose locations were never registered.
   Remove only their two values from `HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList`, if present.

5. Remove Console's five named Windows Firewall rules and persistent WFP state:

   ```powershell
   $firewall = New-Object -ComObject HNetCfg.FwPolicy2
   $rules = @(
     'console_sandbox_offline_block_outbound',
     'console_sandbox_offline_block_inbound',
     'console_sandbox_offline_block_loopback_tcp',
     'console_sandbox_offline_block_loopback_udp',
     'console_sandbox_offline_allow_loopback_proxy'
   )
   foreach ($name in $rules) {
     if (@($firewall.Rules | Where-Object Name -EQ $name).Count) {
       $firewall.Rules.Remove($name)
     }
   }
   # From a reviewed Console source checkout:
   .\scripts\remove-legacy-console-wfp.ps1 -WhatIf
   .\scripts\remove-legacy-console-wfp.ps1
   ```

   The migration-only script removes filters belonging to Console's exact WFP provider, then its sublayer/provider, in one transaction.
   It covers both fixed and dynamically generated loopback filters, verifies no filters remain, and is safe to repeat.
   It does not remove Codex's distinct WFP namespace.
   It is never invoked during launch or installation.

6. In each inventoried state directory, delete only `.sandbox`, `.sandbox-secrets`, `.sandbox-bin`, and `cap_sid`.
   Resolve and inspect these exact paths before recursive removal, including any reparse points.
   Retain configuration, recordings, dependency caches, and user work.
   Account/group deletion does not remove these files or persistent ACLs by itself.

Verify that both accounts and the group are absent, the five firewall rules are absent, the WFP script completes on a second run, all recorded SID scans are clean, and the inventoried credential/helper directories are gone.
Keep the migration/ACL records until this verification is complete.
Then, as the ordinary user, run:

```powershell
mcp-console sandbox --no-config -- cmd.exe /d /c "echo MXC ready"
```

Require exit status zero and `MXC ready`; investigate any MXC support/policy error.
Do not bypass the guard, recreate the legacy accounts, or use host execution as an automatic workaround.

The procedure's identities and storage/ACL inventory are checked against the pinned runner's [Windows transport](https://github.com/t-kalinowski/cobox/blob/8bcbf394a3a6171d9bb3a566976d456f52bd2aec/codex-rs/mcp-console-sandbox/src/windows_console.rs), [product identities](https://github.com/t-kalinowski/cobox/blob/8bcbf394a3a6171d9bb3a566976d456f52bd2aec/codex-rs/windows-sandbox-rs/src/product.rs), [capability records](https://github.com/t-kalinowski/cobox/blob/8bcbf394a3a6171d9bb3a566976d456f52bd2aec/codex-rs/windows-sandbox-rs/src/cap.rs), and [WFP ownership](https://github.com/t-kalinowski/cobox/blob/8bcbf394a3a6171d9bb3a566976d456f52bd2aec/codex-rs/windows-sandbox-rs/src/wfp.rs).
The startup diagnostic regression injects the legacy-account query result into test children; it does not create host accounts.

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
Full coverage needs R/Python, PATH uv/ir, package repository access, prepared dependencies, and an MXC-capable clean host.
CI runs `scripts/with-checkout.cmd python tests/windows_mxc.py -v` after building Console, then the substantive Windows suite on the same clean host.
Acceptance probes MXC through a real public sandbox launch.
Only the native unavailable-runtime diagnostic permits skipping MXC execution cases; installation, policy, and legacy-account failures remain failures.
Hosts without MXC still run configuration, integrity, migration-diagnostic, explicit host-execution, and public fail-closed startup tests.
As hosted runners gain native MXC support, the existing execution cases run automatically.
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

## Troubleshooting

An unavailable-MXC error requires a supported Windows host and policy.
A legacy-account diagnostic requires the [one-time cleanup](#legacy-installation-cleanup); launch performs no migration.

Windows process-creation error 4551 indicates host application-control policy.
The host must permit the executable and downloaded interpreter DLLs; this is separate from Console's sandbox settings.

Keep embedded R sources in LF form as specified by `.gitattributes`.
For Linux workflows under WSL, use an independent Linux checkout and follow [WSL development](DEVELOPMENT.md#windows-development-through-wsl2).
