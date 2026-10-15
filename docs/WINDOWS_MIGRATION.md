# Windows legacy installation cleanup

This administrator procedure is only for installations affected by the legacy-account diagnostic.
New installations should start with [Windows setup](WINDOWS.md).

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

The procedure's identities and storage/ACL inventory are checked against the pinned runner's [Windows transport](https://github.com/t-kalinowski/cobox/blob/47221a5dbcac6f793047ce416937f6db2bb2d75f/codex-rs/mcp-console-sandbox/src/windows_console.rs), [product identities](https://github.com/t-kalinowski/cobox/blob/47221a5dbcac6f793047ce416937f6db2bb2d75f/codex-rs/windows-sandbox-rs/src/product.rs), [capability records](https://github.com/t-kalinowski/cobox/blob/47221a5dbcac6f793047ce416937f6db2bb2d75f/codex-rs/windows-sandbox-rs/src/cap.rs), and [WFP ownership](https://github.com/t-kalinowski/cobox/blob/47221a5dbcac6f793047ce416937f6db2bb2d75f/codex-rs/windows-sandbox-rs/src/wfp.rs).
The startup diagnostic regression injects the legacy-account query result into test children; it does not create host accounts.
