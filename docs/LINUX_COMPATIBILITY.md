---
sidebar: guides
---

# Linux host compatibility

Use this page to troubleshoot Linux startup failures, including missing libraries and host restrictions.
For installation and client setup, see [Get started](getting-started.qmd).

## Runtime libraries

Console 0.0.7 has `manylinux_2_34` wheels for x86-64 and ARM64.
See the [published files on PyPI](https://pypi.org/project/mcp-console/0.0.7/#files).
Their bundled bubblewrap requires the system libcap library.
The development release workflow builds wheels and standalone archives for glibc 2.28 or newer, including RHEL 8, and links libcap statically into bubblewrap.
Console and its companion use the system GNU runtime libraries.
Native language packages can have additional dependencies.

| RHEL package | Ubuntu/Debian package | Shared library   | Use                                                                 |
| ------------ | --------------------- | ---------------- | ------------------------------------------------------------------- |
| `libgcc`     | `libgcc-s1`           | `libgcc_s.so.1`  | GNU compiler runtime                                                |
| `libstdc++`  | `libstdc++6`          | `libstdc++.so.6` | C++ runtime for native code                                         |
| `libcap`     | `libcap2`             | `libcap.so.2`    | Bubblewrap in the 0.0.7 wheels and dynamically linked source builds |

A missing library can appear as `error while loading shared libraries` before Console starts, or as an import error when a native Python extension loads.
Use the missing `.so` filename in the error to identify the package in the table above.

## R package preparation on EL8

With R 4.5 on RHEL 8 or a compatible distribution, preparing the default `tidyverse` package can fail because its dependency `textshaping` 1.1.0 needs a newer HarfBuzz than EL8 supplies.
For managed R sessions, set the `CRAN` URL in the selected R installation's `etc/repositories` file to `https://packagemanager.posit.co/cran/2026-11-18/bin/linux/manylinux_2_28-ARCH/4.5`, replacing `ARCH` with `x86_64` or `aarch64`.
This snapshot supplies a compatible `textshaping` 1.0.5 binary on both architectures.

If you already have the R packages you need, including `reticulate` for mixed R/Python sessions, set `r: {resolution: disabled, packages: []}` in your project's `.agents/console/config.yaml` to use those libraries.
The standalone `mcp-console sandbox` command launches its target without R or R package preparation.

## Host and container setup

The host must permit the sandbox's namespace setup and requested native/seccomp facilities.
Host and container restrictions can prevent startup even when the required libraries are installed.

Ubuntu AppArmor policy can permit system `/usr/bin/bwrap` while denying a relocated bundled helper.
Check native diagnostics and kernel policy records; a working system helper does not validate the bundle.

Ordinary containers may deny nested namespace setup.
Console adds no privileged flags or implicit policy changes.
Deliberately selecting external mode delegates enforcement to the container and has different cleanup guarantees.

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

Mount masking can hide a narrower read beneath a broader denial or make deeper denials fail setup.
Individual-file write roots can be rejected, and missing write roots are skipped until another launch.
Create intended writable directories first.
Console does not create permanent placeholders, grant parents, or switch backends to work around native limits.

[Sandbox permissions](SANDBOX_CONFIGURATION.md#filesystem) owns path precedence and metadata protection.

## Validation

See [Linux platform validation](DEVELOPMENT.md#linux-platform-validation) for regression probes and release acceptance.
