# Linux host compatibility

Console requires glibc 2.35 or newer, the [runtime libraries](getting-started.qmd#install), and a host that permits the sandbox's namespace setup and requested native/seccomp facilities.
Host and container restrictions can prevent startup even on a supported userspace.

## Host and container setup

Ubuntu AppArmor policy can permit system `/usr/bin/bwrap` while denying a relocated bundled helper.
Check native diagnostics and kernel policy records; a working system helper does not validate the bundle.

Ordinary containers may deny nested namespace setup.
Console adds no privileged flags or implicit policy changes.
Deliberately selecting external mode delegates enforcement to the container and has different cleanup guarantees.

A trusted host helper can take precedence.
When the bundled helper is selected, its digest is verified and it is executed through the verified descriptor.
Test that path in an approved disposable environment; do not weaken a user's host security policy merely to pass a check.

## Policy and backend contract

Default execution uses bubblewrap with user, mount, and PID namespaces, requested filesystem/network rules, and supervised retirement.
It needs procfs, permitted namespace setup, and the requested native/seccomp facilities.

There is no automatic Landlock or unsandboxed fallback.
Standalone `linux_backend: landlock` is rejected.
A namespace failure remains a launch failure.

Fresh namespace-local procfs is optional.
An inherited-procfs alternative keeps the backend and policy but can reveal host PIDs and permitted metadata.
Pidfds are an optional emergency mechanism; without them, a stopped namespace init can exceed cleanup deadlines.
Missing native wait evidence causes failure and retained storage, not inferred cleanup success.

### Filesystem limits

Native classification uses effective permissions, not only a kind label.
Full-write policies do not become restricted because their entries contain ineffective carve-outs.
`unrestricted` ignores narrowing entries; `external-sandbox` without a proxy delegates both filesystem and networking enforcement to an outer boundary.

Mount masking can hide a narrower read beneath a broader denial or make deeper denials fail setup.
Individual-file write roots can be rejected, and missing write roots are skipped until another launch.
Create intended writable directories first.
Console does not create permanent placeholders, grant parents, or switch backends to work around native limits.

[Sandbox configuration](SANDBOX_CONFIGURATION.md#filesystem) owns path precedence and metadata protection.
Restricted-policy security results must not be attributed to unrestricted or externally enforced workloads.

## Reproducing the comparisons

See [Linux platform validation](DEVELOPMENT.md#linux-platform-validation) for regression probes and release acceptance.
