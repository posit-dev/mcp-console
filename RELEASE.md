# Releasing MCP Console

Each release ships six platform wheels on PyPI and six native archives on GitHub Releases.

| OS                  | x86-64 target              | ARM64 target                | Wheel platform                                    |
| ------------------- | -------------------------- | --------------------------- | ------------------------------------------------- |
| Linux (glibc 2.28+) | `x86_64-unknown-linux-gnu` | `aarch64-unknown-linux-gnu` | `manylinux_2_28_x86_64`, `manylinux_2_28_aarch64` |
| macOS               | `x86_64-apple-darwin`      | `aarch64-apple-darwin`      | `macosx_*_x86_64`, `macosx_*_arm64`               |
| Windows             | `x86_64-pc-windows-msvc`   | `aarch64-pc-windows-msvc`   | `win_amd64`, `win_arm64`                          |

Linux builds use the native manylinux_2_28 images for the public executable, sandbox runner, and bubblewrap.
Windows MSVC builds statically link the runtime.

`Cargo.toml` owns the version; keep its root package entry in `Cargo.lock` synchronized.
Between releases, use the most recently released version plus `+dev`.
Development executables built from Git also report the first 12 characters of `HEAD`, for example `0.0.6+dev.g0123456789ab`; package metadata keeps `0.0.6+dev`.
The current workflows and [`scripts/release.py`](scripts/release.py) are authoritative for mechanics.
This page owns the release procedure and required evidence.

## Consuming a standalone release

Applications such as Posit Assistant can pin a tag, download `mcp-console-<target>.tar.gz` (Unix) or `mcp-console-<target>.zip` (Windows) and verify its SHA-256 against `SHA256SUMS` from the same release.
Extract the archive into its own directory, preserving Unix executable permissions.
For example, after downloading the Linux x86-64 archive and checksum file:

```sh
archive=mcp-console-x86_64-unknown-linux-gnu.tar.gz
awk -v archive="$archive" '$2 == archive' SHA256SUMS | sha256sum --check --strict &&
  mkdir console &&
  tar -xzf "$archive" -C console &&
  console/bin/mcp-console --version &&
  console/bin/mcp-console sandbox -- /usr/bin/true
```

An application can invoke `bin/mcp-console sandbox -- PROGRAM ARG...` directly.
On Windows, use `bin\mcp-console.exe` with the same arguments.
Keep `libexec/` and `share/` alongside `bin/` so Console can find and verify its companion.
The [sandbox launcher](docs/SANDBOX_COMMAND.md) requires no MCP server, `ir`, Python, pip, or uv.
The application supplies the target program and its dependencies.

## Private sandbox executable

Source builds need Python 3.11+, Git, rustup, and native build tools.
Console's minimum Rust version is in `Cargo.toml`; the pinned companion uses its own toolchain.
Wheel builds need Maturin 1.15+.
R and its packages are not build or Python-only execution prerequisites.

On macOS install Xcode Command Line Tools.
Ubuntu builds need a C toolchain, pkg-config, libcap/OpenSSL development files, libcurl development files for R preparation, and binutils.
Packages can require more system libraries.
Windows source builds need MSVC, the Windows SDK, and CMake; see [Windows build instructions](docs/WINDOWS_DEVELOPMENT.md#build-from-source).

```sh
scripts/with-checkout uv tool install --reinstall .
```

Packaging stages the companion automatically, including editable installs.
Direct Cargo/Maturin commands need `scripts/stage-sandbox-runner` first.
`cargo install` alone is insufficient because it does not install the complete bundle.

[`sandbox-runner.json`](sandbox-runner.json) pins the companion repository, release, and commit.
`MCP_CONSOLE_SANDBOX_SOURCE` can select a dedicated clean checkout at that pin.
Otherwise staging uses `${XDG_CACHE_HOME:-$HOME/.cache}/mcp-console/sandbox/<repository>/<commit>/source`; an explicit XDG root must be absolute.

Use `scripts/stage-sandbox-runner --describe` to locate inputs.
Respect [checkout ownership](docs/DEVELOPMENT.md#checkout-ownership).
Changes to SDKs, compilers, linkers, or system libraries may require cleaning both Console and companion build output even when Cargo reports freshness.

### Bundle and verification

Unix installations contain the public executable, the private runner under `libexec`, and licenses under `share/licenses/mcp-console`.
Linux additionally bundles bubblewrap and its notices/source metadata.
The Linux archive keeps bundled libraries at the same paths relative to the installation prefix as the wheel, so the native executables can load them after extraction.
Windows wheels keep the native Console and companion under `libexec` and expose an environment-bound Python launcher.
The standalone archive copies the same native Console executable to `bin/mcp-console.exe`.
Every archive carries Console’s MIT license alongside the companion licenses.

Move the complete native bundle when relocating it; a symlink to the native executable is supported.
Do not split Cargo's shared target/build layout.
Native launch verifies the selected companion and does not download or extract one at runtime.

Staging/build checks must retain artifact digests, component identity, pin consistency, and required license files.
Linux staging rejects inherited `CODEX_BWRAP_SOURCE_DIR` and `CODEX_SKIP_BWRAP_BUILD`, including empty values.
Release builds link the EL8 static libcap archive into bubblewrap and include its license in the bundle.
Source builds that link libcap dynamically require it on the host.
Static builds supply its license through `MCP_CONSOLE_LIBCAP_NOTICE`.
Supplying text is not legal certification of its contents.

When advancing the pin, review the companion's protocol, implementation, executable tests, and toolchain together.
Preserve both environment-policy and descriptor-bootstrap acceptance.
Private protocols are unversioned; the pin is the integration boundary.

### ABI and installed-wheel acceptance

```sh
python3 scripts/release.py inspect-wheel WHEEL --target TARGET --report abi.json
```

Audit every ELF member, including private helpers and extra native libraries, against all advertised wheel tags.
Preserve architecture, loader, dependency/path, symbol-version, and metadata checks; unknown policies fail rather than inheriting the build host's assumptions.
`--release` also checks the glibc 2.28 floor and requires bubblewrap to link libcap statically.
Exact loader algorithms and symbol allowlists belong in the release tooling, not a second prose specification.

On macOS/Linux use `smoke-wheel WHEEL --installed-only --sandbox-pin PIN_FILE` in a clean runtime without the source checkout/Cargo outputs.
Windows builds use the PEP 517 backend through `uv build --wheel` so the installed command retains its native bundle.
Run `python tests/windows_distribution.py WHEEL` to audit normal and delayed PE imports in every bundled native executable or library.
Redistributable VC runtime dependencies are rejected; ordinary Windows system and UCRT libraries are allowed.
On Windows x86-64, install the wheel and run `python tests/windows_install.py --installed-binary PUBLIC_COMMAND` on a clean host.
Installation acceptance repeats the import audit for installed wheels and source builds, including the environment-bound public launcher.
On Windows 11 ARM64, install the wheel and the `reticulate` and `RSQLite` packages for native R.
Run both acceptance checks in PowerShell.

```powershell
$env:MCP_CONSOLE_TEST_REQUIRE_MXC = '1'
python tests/windows_install.py --preinstalled-r --installed-binary (Get-Command mcp-console.exe).Source
python tests/windows_install.py --preinstalled-r --archive archives/mcp-console-aarch64-pc-windows-msvc.zip
```

This matches the release job's MXC requirement and exercises the preinstalled R packages without `ir`.
The hosted x86-64 Windows Server runner probes for MXC and skips native execution cases when it is unavailable.
Run those cases on an MXC-capable x86-64 Windows 11 host before release.
This checks version/help, empty-PATH bundled sandbox execution, filesystem/exit-status enforcement, Python-only MCP, and the R/Python bridge without rebuilding from source.
Retain empty-PATH bundled-helper acceptance; a working system bwrap does not validate the relocated bundle.
An ABI report alone is not runtime or sandbox acceptance.

## Linux floor validation

Run on a matching native Docker host with the evidence directory shared with the daemon:

```sh
scripts/check-linux-wheel dist/*.whl --archive archives/*.tar.gz \
  --target x86_64-unknown-linux-gnu --evidence linux-compatibility
```

Use `aarch64-unknown-linux-gnu` for ARM64.
Before starting Docker, the driver compares the installed paths, Unix permissions, and SHA-256 digests of the archive's executables, libraries, and licenses with the wheel.
Emulation is rejected; `--docker-context` can select an explicit daemon.

The Python-only AlmaLinux 8 runtime audits the wheel's ABI and companion pin before either artifact runs.
A separate AlmaLinux 8 minimal image launches the relocated archive with its bundled sandbox helpers and no Python or uv.
The wheel runs in compiler-free images with Python alone and with R.
The R runtime includes R's recommended packages, which the isolated resolver expects the R installation to provide.
The R preparation stage refreshes package resolution for each floor run before producing the final R image.
Language-package build flags do not change distributed wheel binaries.

Native probes require an approved disposable namespace-capable container.
Their elevated container options do not modify host sysctls and must not be applied as a workaround to a user's host.
Distinguish namespace denial from loader/ABI failure; a skipped probe does not pass the gate.

Require startup, language evaluation, bounded shutdown, installed loader resolution, and installed sandbox acceptance on both native architectures.
Both wheel builders and floor-runtime jobs must pass.
Retain wheel hashes, ELF/tag reports, image digests/platforms, exact commands, environment versions, and results separately per architecture.

Run this rehearsal for the actual release candidate and its companion pin.

## CI caches and rehearsal

Caches are keyed by OS, architecture, toolchain/dependency inputs, and week.
Bump `CI_BUILD_CACHE_VERSION` when changing build flags or native dependencies outside the hashed inputs.
Keep native artifact verification on cache hits; reuse does not prove external SDK/compiler compatibility.

Do not cache accounts, credentials, ACLs, or provisioning state.
MXC capability probing and full Windows acceptance still run on a clean host after restoring build/staging caches; unavailable native execution cases are skipped as documented in [Windows validation](docs/WINDOWS_DEVELOPMENT.md#validation).
Exact cache-key and timestamp-restoration mechanics belong in the workflow and staging scripts.

CI uses CRAN binaries for macOS and Windows x64 and PPM's portable Linux binaries for both Linux architectures.
Windows ARM64 tests both startup orders with native R and Python, preinstalled `reticulate`, and `r.resolution` set to `disabled`.
The R-first test uses `RSQLite` to open the connection required by its configured startup block.
The managed R resolver, `r-lib-ir`, currently lacks a Windows ARM64 wheel.
Configure both R's repository data file and site profile so profile-free preparation and ordinary R sessions select the same repository.
Packages without compatible binaries still build from source.

Installation tests need the checkout and temporary directory on the same writable filesystem for rename-based checks.
They run after other checks because installation can change staging/build layout.

## One-time PyPI setup

Create the GitHub Actions environment `pypi`.
Configure the existing PyPI project's Trusted Publisher with owner `posit-dev`, repository `mcp-console`, workflow `release.yml`, and environment `pypi`.
Do not add a `PYPI_TOKEN` secret; only publication receives an OIDC token.

## Prepare and rehearse

Choose an unpublished `X.Y.Z` after checking public PyPI and remote tags.
Replace the development version with `X.Y.Z` in Cargo's version and lockfile entry.
Release executables report that exact version without a Git suffix.
Version values are normalized in acceptance snapshots, so a version bump alone does not require snapshot updates.
Inspect the diff, run `scripts/format`, and pass `scripts/check --full`.

```sh
gh workflow run release.yml --ref release/X.Y.Z
```

Manual dispatch must skip publication.
Require the six native archive and installed-wheel jobs, both Linux floor-runtime jobs, and the aggregate artifact check to pass.
The `github-release` workflow artifact contains the archives and their checksum manifest for inspection.
Exercise mixed-language and R-free installed wheels; `smoke-wheel --without-r` isolates R discovery.
Establish readiness before ordinary cell assertions rather than treating asynchronous tool discovery as runtime readiness.

Revalidate the [resolver trust boundary](docs/PACKAGE_CONFIGURATION.md#trust-boundary) at the release candidate and its actual companion pin.
Require the applicable trust, cancellation, result-handoff, and cleanup cases on each supported native platform.
Record the exact Console and companion revisions with the CI evidence.

## Publish

Merge only with passing CI and a verified GPT connector reviewer thumbs-up for the current head.
Then require successful push CI on `main` for the exact merged commit.
Compare its tree with the rehearsed tree; rehearse again if different.
Later merges can continue: they do not cancel this commit's CI or change the chosen release revision.

From a clean checkout, retain and tag the verified full SHA even if `main` advances:

```sh
release_version=X.Y.Z
release_sha=FULL_VERIFIED_MERGE_SHA
git switch --detach "$release_sha"
git tag -a "v$release_version" "$release_sha"
git push origin "refs/tags/v$release_version"
```

Use `-F MESSAGE_FILE` for a noninteractive annotation.
The tag workflow must verify version, main ancestry, exact-commit push CI, and the install-tested artifacts before Trusted Publishing.
After the PyPI upload, a separate job verifies that `SHA256SUMS` lists all six archives exactly once with their measured digests.
It then uploads the archives and manifest to a draft GitHub release, compares the remote asset digests, and publishes the release.

## Verify the publication

Check PyPI's version JSON: all six wheels must be present, unyanked, and match the tag workflow's artifact SHA-256 digests.
The tag workflow checks these hashes and fresh pinned/unqualified public-index installations on all six release platforms, including installed sandbox execution and Python-only MCP evaluation.
It waits up to two minutes for both the version JSON and package index to expose all expected files, including retries for temporary request failures; a visible hash mismatch, yanked file, or HTTP permission error fails immediately.
Verify fresh public-index installation on every supported release platform:

```sh
export UV_CACHE_DIR="$(mktemp -d)"
export UV_TOOL_DIR="$(mktemp -d)"
export UV_TOOL_BIN_DIR="$(mktemp -d)"
export UV_NO_CONFIG=1
uvx --isolated --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console@$release_version" --version
uv tool install --no-cache --no-sources --default-index https://pypi.org/simple \
  "mcp-console==$release_version"
"$UV_TOOL_BIN_DIR/mcp-console" --help
"$UV_TOOL_BIN_DIR/mcp-console" sandbox -- /usr/bin/true
```

Through an MCP client, establish readiness for that exact version and verify Python `6 * 7` returns `42`; check R when installed.
A server waiting for protocol input is normal.
On Windows use PowerShell environment variables and `mcp-console.exe`; replace `/usr/bin/true` with the absolute `System32/cmd.exe` command and `/d /c "exit 0"`.
Use an MXC-capable Windows host to verify native sandbox execution and the R/Python bridge.

Repeat unqualified `uvx mcp-console --version` and `uv tool install mcp-console` with fresh directories and the same isolation/public-index options; confirm they select the new version.
Leave older releases unchanged.

## Return to development

After publication checks pass, set the version in `Cargo.toml` and its root package entry in `Cargo.lock` to `X.Y.Z+dev`.
Run `scripts/format` and `scripts/check`, then commit the two version files as `Mark post-release builds as development versions` through the usual review process.
Keep the release tag on the verified release commit.

## Recover from a failed release

First check publication logs and PyPI for any uploaded wheel.
Before upload, repair through the same review, CI, and rehearsal process.
Moving a pushed tag requires explicit user authorization, confirmation that its old run cannot publish, and a force-with-lease push limited to that verified tag, never `main`.

After **any** wheel is published, preserve the tag and all uploaded files.
**Never yank or remove a PyPI file.** For partial publication, rerun the failed job from the same workflow run while its original artifacts remain.
The pre-build version check rejects versions already visible on PyPI.
If the GitHub upload stopped midway, rerun the failed job with the original `github-release` artifact.
It checks existing assets by digest and uploads the missing files to the draft.
If GitHub retained an empty asset in the `starter` state after an interrupted upload, remove that placeholder from the draft before retrying.
A digest mismatch on an uploaded asset requires investigation.
Do not rebuild that version or replace an uploaded filename.
Defective published code requires a new version.
