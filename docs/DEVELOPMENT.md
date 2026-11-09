# Development

Run commands from the repository root.
[AGENTS.md](../AGENTS.md) owns change and publishing rules; [Architecture](ARCHITECTURE.md) maps implementation ownership; [Boundary tests](../tests/boundaries/README.md) explains behavioral evidence.

## Setup

Install Python 3.11+, Git, rustup, and platform build tools.
Use Python 3.14 for the complete test suite, matching CI and the available DuckDB fixture wheels.
Console uses the Rust version required by `Cargo.toml`; its companion has a separate pinned toolchain.
See [native build prerequisites](../RELEASE.md#private-sandbox-executable).

```sh
scripts/preflight
scripts/stage-sandbox-runner
scripts/with-checkout cargo build --release --target-dir target
```

`preflight` inventories tools, runtimes, staging, and caches without installing or building; `--json` returns structured output.
Missing capabilities are reported, not validated.
Packaging stages the companion automatically; direct Cargo/Maturin builds need staging first.

For a source installation:

```sh
scripts/with-checkout uv tool install --reinstall .
```

Install the tools used by `scripts/format`: ruff, yamark, air, rustfmt, and Clippy for checking.
Relay/worker tests also need reticulate in the R library inherited by the test process, because their proxy bypasses host runtime preparation.

Development versions end in `+dev`.
Git builds append a short commit identity to runtime version reports; this identifies the base commit, not uncommitted edits.
Package metadata keeps the version without that suffix.
[build.rs](../build.rs) owns this distinction; ordinary development does not require version-file edits.

On native Windows use `.cmd` launchers, such as `scripts/check.cmd`, or invoke the scripts with Python.
Follow [Windows validation](WINDOWS_DEVELOPMENT.md#validation) for companion staging, native prerequisites, and MXC coverage.

### Windows development through WSL2

Use an independent Linux checkout, build directory, and caches in the distro filesystem, such as `~/src/mcp-console`.
Windows and Linux must not share a Git worktree or build output.
Separate WSL distributions still share the host kernel.
Install Linux toolchains and required R system libraries, and prepare cold package caches serially before concurrent tests.

## Validation ladder

| Command                             | Purpose                                                         |
| ----------------------------------- | --------------------------------------------------------------- |
| `scripts/test --full --list`        | Discover functional cases without building.                     |
| `scripts/test --locate SELECTOR`    | Find source and snapshots.                                      |
| `scripts/test BOUNDARY/SUITE::CASE` | Run a focused regression.                                       |
| `scripts/test --update SELECTOR`    | Regenerate snapshots; inspect and rerun without `--update`.     |
| `scripts/format`                    | Run all formatters; inspect every reported result and the diff. |
| `scripts/check`                     | Ordinary final local gate; `--quick` is an alias.               |
| `scripts/check --full`              | Add applicable functional, tooling, and installation checks.    |
| `scripts/test --stress`             | Run allocation and repeated concurrency workloads.              |

Start behavior changes with a failing public regression and refactors with an existing public baseline.
After editing, run focused cases, formatting, `git diff --check`, and the appropriate final gate.
The formatter's default mode can report failures without failing the command, so read its output.

On macOS/Linux, the ordinary gate includes staging, embedded-source and architecture checks, Rust formatting/Clippy/tests, a release build, and smoke transcripts.
On Windows it uses the staged companion, a debug build, and native acceptance.
Smoke tests compare documented preparation defaults with public configuration and check companion source links against `sandbox-runner.json`.
The full gate adds applicable tooling, shared functional, and installation tests; installation runs last because it can replace build/staging directories.

CI is the comprehensive platform gate.
A focused pass or capability skip does not validate another platform.
Report commands and missing coverage precisely.

### Selecting and sizing test runs

Selectors retain their scope under every profile.
`--list` and `--locate` do not acquire build ownership.
Native Windows cases also accept `CLASS.CASE`, such as `WindowsConsole.test_python_without_r`.

Use `--jobs N` for concurrency and `--timeout SECONDS` for case deadlines.
Default concurrency is `max(2, 2 * N)` for logical CPU count `N`, or two when unavailable.
`MCP_CONSOLE_TEST_BINARY` selects an absolute installed executable and skips the checkout build; sandbox cases still need its complete companion bundle.

Only a successful full, unscoped update removes orphan snapshots.
Focused updates preserve unselected cases.
See [snapshot rules](../tests/boundaries/README.md#snapshots).
Version-sensitive SQL fixtures use [shared R support](../tests/support/r.py), which owns their DuckDB pin and update criteria.
The related cancellation limitation is tracked in [TODO](TODO.md#runtime-and-resource-limits).

## Documentation website

Quarto renders the guides and Python reference; pkgdown adds the R package site.
Install Quarto, uv, R, pkgdown, and the R package dependencies:

```r
pak::pak(c("pkgdown", "local::r"))
```

```sh
scripts/preview-site
scripts/render-site
uv run --no-project --with-requirements docs/requirements.txt python tests/website.py
```

The first two commands prepare Python documentation tools through uv and write to `docs/_site`.
Extra arguments go to Quarto; for example, `scripts/preview-site --port 3604 --no-browser`.
On Windows invoke either script with `uv run --no-project --isolated --with-requirements docs/requirements.txt python`.
The website test renders a fresh temporary copy and checks navigation, titles, search, and local links/anchors, including both package references.

Edit Python docstrings and R roxygen sources for generated API changes.
Site configuration lives in `docs/_quarto.yml`, `great-docs.yml`, and `r/_pkgdown.yml`.
Generated sources, inventories, downloads, and HTML remain ignored.

| Site component                                                 | Owner                                                                               |
| -------------------------------------------------------------- | ----------------------------------------------------------------------------------- |
| Python reference generation and pinned Great Docs integration  | [_python.py](_python.py)                                                            |
| R package reference                                            | [_pkgdown.R](_pkgdown.R), [r/_pkgdown.yml](../r/_pkgdown.yml)                       |
| Repository links, page titles, and symbol links                | [Website filters](https://github.com/posit-dev/mcp-console/tree/main/docs/_filters) |
| Published directory URLs, assets, search, and preview behavior | [_urls.py](_urls.py)                                                                |
| Rendered navigation and link checks                            | [tests/website.py](../tests/website.py)                                             |

Use the preview script so Quarto retains the outputs needed for incremental renders.
Run a full render after changing API docstrings, reference configuration, or the R package site.
Static Python analysis and unevaluated R workflow examples do not need API credentials or a Console executable.

CLI help and the configuration starter come from acceptance snapshots.
Regenerate them through the owning CLI tests, never by editing snapshots.
To publish another CLI command, update the explicit allowlist in [_filters/cli-help.py](_filters/cli-help.py) and its independent expectation in `tests/website.py`.

### Documentation ownership

Give each fact one primary home, with links where another audience needs it:

| Information                                                             | Home                                                                       |
| ----------------------------------------------------------------------- | -------------------------------------------------------------------------- |
| First use and task walkthroughs                                         | [Get started](getting-started.qmd), [Guides](README.md), [Build](BUILD.md) |
| Accepted configuration fields, values, defaults, discovery, and merging | [Configuration](CONFIGURATION.md)                                          |
| Setting behavior and worked examples                                    | Concept references linked from [Reference](REFERENCE.md)                   |
| Public MCP operations and results                                       | [API](API.md)                                                              |
| R/Python signatures and package-specific examples                       | R roxygen and Python docstrings                                            |
| Process ownership and cross-module invariants                           | [Architecture](ARCHITECTURE.md)                                            |
| Algorithms, private schemas, and local constraints                      | Owning source comments and types                                           |
| Unfinished work                                                         | [TODO](TODO.md)                                                            |

Keep Markdown readable in the checkout.
Use relative source links; site filters map external repository paths and package references to their rendered destinations.
The URL hook publishes lowercase directory routes, with `r-guide/` and `python-guide/` reserved for the integration guides.
Independently rendered package documentation must use published URLs.

Update navigation and internal links when consolidating pages.
Keep current behavior in the docs, remove completed tasks, and use Git history for superseded designs and one-off audits.
Future options belong in TODO, not supported usage.

## Find the public test

Start at the outermost process boundary that observes the change.
Use `--full --list`, `--locate`, and scoped searches.
[Authoring](../tests/boundaries/AUTHORING.md) covers fixtures, embedded programs, and causal lifecycle assertions.
Preserve real-library sandbox cases when they exercise distinct permissions or activation paths.

## Review boundary

For a cross-cutting change, identify observable behavior, owning modules, public tests, platform effects, and intended PR base.
For a bug, record a minimal reproduction with expected/actual behavior and platform/version details.
Keep task-specific evidence in the checkpoint.

Review the affected public MCP, CLI, configuration, and client contracts, including omission, `null`, empty values, and defaults.
Update clients, owning docs, and regenerated discovery/help snapshots together.
Private protocols evolve in lockstep within the [compatibility boundary](API.md#what-is-not-a-public-compatibility-contract).

```sh
scripts/review-diff BASE
git diff --merge-base BASE
```

Use the intended parent for a stacked PR.
Stage intended new files before measuring; line counts help review planning but do not establish semantic scope.

## Native sandbox policy experiments

The hidden `sandbox --runner-config-env NAME` flag passes an environment variable's JSON directly to the pinned runner.
It bypasses Console configuration discovery, validation, and generated additions, including managed temporary storage and platform extensions.
The runner still validates its own input.
Use public `sandbox --config-env` for the `config.yaml` format.

The native selector is exclusive with Console's other policy/configuration selectors and `--exit-with-parent`.
Put native lifecycle settings in the payload; stdin remains the workload's input.
Consult [sandbox-runner.json](../sandbox-runner.json) for the exact companion revision and its protocol.
This internal flag and schema have no public stability promise.

`unrestricted` filesystem policies do not narrow access through entries; `external-sandbox` delegates enforcement to an outer boundary without verifying it.
Without a proxy, `network: restricted` in external mode adds no restriction.
Windows rejects external mode.
On Linux its supervision covers the original group and direct child; the outer boundary must retire escaped descendants.
Native cleanup does not establish isolation for hostile unrestricted workloads.

## Checkout ownership

Build, staging, checks, and packaging share `.dev-workflow/checkout.lock`, outside `target`.
Use `scripts/with-checkout` for direct mutating Cargo/Maturin commands.
Do not delete locks, run nested mutators concurrently under one token, or share application build/staging directories between checkouts.

Separate worktrees can run concurrently; companion staging has its own shared cache lock.
Cancellation retains ownership through process cleanup.
After a crash or forced termination, establish that mutators stopped before restarting.
A stale lock diagnostic proves neither activity nor cleanup.

## Resume from a small checkpoint

Copy [the template](templates/task-checkpoint.md) to the ignored `.dev-workflow/task.md`.
Record scope, branch/base, revision, edits, validation, next action, and the requested stopping point.
Check an existing checkpoint before overwriting it.

On resume compare it with HEAD, Git status, base, and all staged/unstaged/untracked changes.
Records under `.dev-workflow/runs/` describe the worktree at admission; an unfinished record does not prove that a process is running.
Establish cleanup with its original owner before rerunning.
Report local validation and hosted CI/review status separately, and respect the requested publishing boundary.

## Linux platform validation

```sh
scripts/test cli/sandbox/test_procfs
scripts/test cli/sandbox/test_configuration
python3 tests/sandbox_installation.py target/release/mcp-console
```

These probes check specific native contracts, including procfs exposure, denied paths, networking, and namespace entry.
Fixture capabilities must establish that the host can perform the attempted operation; host restrictions must not substitute for sandbox enforcement.
See [fixture rules](../tests/boundaries/README.md#fixtures) for safe PID handling.

Release acceptance also tests installed bundles with an empty PATH and native floor-runtime images.
Follow [Release](../RELEASE.md#linux-floor-validation); an ABI report or skipped native probe is not sandbox acceptance.
