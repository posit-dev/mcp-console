# Releasing MCP Console

The release workflow publishes four binary-only PyPI wheels: Apple Silicon and Intel macOS, plus ARM64 and x86-64 Linux.
Linux release validation targets glibc 2.35+ with native Ubuntu 22.04 builds.
No source distribution, Windows wheel, or GitHub release archive is published by this workflow.

`Cargo.toml` owns the version; keep its root package entry in `Cargo.lock` synchronized.
The current workflows and [`scripts/release.py`](scripts/release.py) are authoritative for mechanics.
This page owns the release procedure and required evidence.

## Private sandbox executable

Source builds need Python 3.11+, Git, rustup, and native build tools.
Console's minimum Rust version is in `Cargo.toml`; the pinned companion uses its own toolchain.
Wheel builds need Maturin 1.15+.
R and its packages are not build or Python-only execution prerequisites.

On macOS install Xcode Command Line Tools.
Ubuntu builds need a C toolchain, pkg-config, libcap/OpenSSL development files, libcurl development files for R preparation, and binutils.
Packages can require more system libraries.
Windows source builds need MSVC, the Windows SDK, and CMake; see [Windows](docs/WINDOWS.md).

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
Windows contains its native Console executable and three sandbox executables under `libexec`, with an environment-bound Python launcher exposing the public command.

Move the complete native bundle when relocating it; a symlink to the native executable is supported.
Do not split Cargo's shared target/build layout.
Native launch verifies the selected companion and does not download or extract one at runtime.

Staging/build checks must retain artifact digests, component identity, pin consistency, and required license files.
Linux staging rejects inherited `CODEX_BWRAP_SOURCE_DIR` and `CODEX_SKIP_BWRAP_BUILD`, including empty values.
Dynamically linked libcap is a host dependency; static linkage requires a nonempty applicable `MCP_CONSOLE_LIBCAP_NOTICE`.
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
`--release` additionally enforces the release floor.
Exact loader algorithms and symbol allowlists belong in the release tooling, not a second prose specification.

Use `smoke-wheel WHEEL --installed-only --sandbox-pin PIN_FILE` in a clean runtime without the source checkout/Cargo outputs.
Retain empty-PATH bundled-helper acceptance; a working system bwrap does not validate the relocated bundle.
An ABI report alone is not runtime or sandbox acceptance.

## Linux floor validation

Run on a matching native Docker host with the evidence directory shared with the daemon:

```sh
scripts/check-linux-wheel dist/*.whl \
  --target x86_64-unknown-linux-gnu --evidence linux-compatibility
```

Use `aarch64-unknown-linux-gnu` for ARM64.
Emulation is rejected; `--docker-context` can select an explicit daemon.

The floor fixtures exercise installed wheels without R and with R, with runtime prerequisites separated from build tools.
They refresh R preparation before producing compiler-free runtime images, rather than treating old Docker layers as fresh package resolution.
Language-package build flags do not change distributed wheel binaries.

Native probes require an approved disposable namespace-capable container.
Their elevated container options do not modify host sysctls and must not be applied as a workaround to a user's host.
Distinguish namespace denial from loader/ABI failure; a skipped probe does not pass the gate.

Require startup, language evaluation, bounded shutdown, installed loader resolution, and installed sandbox acceptance on both native architectures.
Both wheel builders and floor-runtime jobs must pass.
Retain wheel hashes, ELF/tag reports, image digests/platforms, exact commands, environment versions, and results separately per architecture.

Earlier controlled-Jammy rehearsals at `d26f9771` passed their measured no-R/R-present contracts.
They are historical evidence, not acceptance of a later source or companion pin.
The [previous detailed record](https://github.com/t-kalinowski/mcp-console/blob/b9caf7c55e9f9fbb0ce33582e4c61d615813b4eb/RELEASE.md#linux-floor-validation) remains available.
Perform the native older-runner rehearsal for the actual release candidate.

## CI caches and rehearsal

Caches are keyed by OS, architecture, toolchain/dependency inputs, and week.
Bump `CI_BUILD_CACHE_VERSION` when changing build flags or native dependencies outside the hashed inputs.
Keep native artifact verification on cache hits; reuse does not prove external SDK/compiler compatibility.

Do not cache accounts, credentials, ACLs, or provisioning state.
Windows setup and full acceptance still run after restoring build/staging caches.
Exact cache-key and timestamp-restoration mechanics belong in the workflow and staging scripts.

Installation tests need the checkout and temporary directory on the same writable filesystem for rename-based checks.
They run after other checks because installation can change staging/build layout.

## One-time PyPI setup

Create the GitHub Actions environment `pypi`.
Configure the existing PyPI project's Trusted Publisher with owner `t-kalinowski`, repository `mcp-console`, workflow `release.yml`, and environment `pypi`.
Do not add a `PYPI_TOKEN` secret; only publication receives an OIDC token.

## Prepare and rehearse

Choose an unpublished `X.Y.Z` after checking public PyPI and remote tags.
Update Cargo's version and lockfile entry.
Regenerate intended CLI/MCP snapshot changes, updating the [canonical handshake](tests/boundaries/README.md#canonical-handshake) first.
Inspect the diff, run `scripts/format`, and pass `scripts/check --full`.

```sh
gh workflow run release.yml --ref release/X.Y.Z
```

Manual dispatch must skip publication.
Require all four wheel builds/installed smoke tests and both Linux floor-runtime jobs to pass.
Exercise mixed-language and R-free installed wheels; `smoke-wheel --without-r` isolates R discovery.
Establish readiness before ordinary cell assertions rather than treating asynchronous tool discovery as runtime readiness.

Revalidate the [resolver trust contract](docs/RESOLVER_RELEASE_AUDIT.md) at the release candidate and its actual companion pin.
[TODO](docs/TODO.md#release-verification) is an index of outstanding verification, not permission to skip gates.

## Publish

Merge only with passing CI and a verified GPT connector reviewer thumbs-up for the current head.
Then require successful push CI on `main` for the exact merged commit.
Compare its tree with the rehearsed tree; rehearse again if different.

From a clean checkout, retain and tag the verified full SHA even if `main` advances:

```sh
release_version=X.Y.Z
release_sha=FULL_VERIFIED_MERGE_SHA
git switch --detach "$release_sha"
git tag -a "v$release_version" "$release_sha"
git push origin "refs/tags/v$release_version"
```

Use `-F MESSAGE_FILE` for a noninteractive annotation.
The tag workflow must verify version, main ancestry, exact-commit push CI, and the four install-tested artifacts before Trusted Publishing.

## Verify the publication

Check PyPI's version JSON: all four wheels must be present, unyanked, and match the tag workflow's artifact SHA-256 digests.
The tag workflow checks these hashes and fresh pinned/unqualified public-index installations on all four release platforms, including installed sandbox execution and Python-only MCP evaluation.
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

Repeat unqualified `uvx mcp-console --version` and `uv tool install mcp-console` with fresh directories and the same isolation/public-index options; confirm they select the new version.
Leave older releases unchanged.

## Recover from a failed release

First check publication logs and PyPI for any uploaded wheel.
Before upload, repair through the same review, CI, and rehearsal process.
Moving a pushed tag requires explicit user authorization, confirmation that its old run cannot publish, and a force-with-lease push limited to that verified tag, never `main`.

After **any** wheel is published, preserve the tag and all uploaded files.
**Never yank or remove a PyPI file.** For partial publication, rerun the failed job from the same workflow run while its original artifacts remain.
Do not rebuild that version or replace an uploaded filename.
Defective published code requires a new version.
