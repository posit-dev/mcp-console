# Development

Run commands from the repository root.
Read [AGENTS.md](../AGENTS.md) for change and publishing rules, [Architecture](ARCHITECTURE.md) for ownership, and [Boundary tests](../tests/boundaries/README.md) for behavioral evidence.

## Setup

Install Python 3.11+, Git, rustup, and platform build tools.
Use a Rust version satisfying `Cargo.toml`; the companion has its own pinned toolchain.
Python 3.14 matches the development-script CI environment.
Python 3.15 is awaiting DuckDB wheels; use 3.14 for the complete test suite.
[Release/build setup](../RELEASE.md#private-sandbox-executable) lists native prerequisites.

```sh
scripts/preflight
scripts/stage-sandbox-runner
scripts/with-checkout cargo build --release --target-dir target
```

`preflight` inventories tools, runtimes, staging, and caches without installing or building.
`--json` gives structured output.
On Windows, the native companion path comes from the staged build manifest and identifies its immutable bundle; older bundles do not count as the current build.
Optional missing capabilities are reported, not evidence of a successful build.

For a source installation:

```sh
scripts/with-checkout uv tool install --reinstall .
```

Packaging stages its companion automatically; direct Cargo/Maturin builds need staging first.
Install the formatting tools used by `scripts/format`, including ruff, yamark, air, rustfmt, and Clippy for checking.

On native Windows use `.cmd` launchers, such as `scripts/check.cmd`, or `python scripts/check`.
Stage the companion before native build/test/check commands.
See [Windows validation](WINDOWS_DEVELOPMENT.md#validation) for MXC host requirements and required runtimes.

### Windows development through WSL2

Use a Linux checkout, build directory, and runtime caches in the distro filesystem, such as `~/src/mcp-console`.
Do not share a Windows Git worktree or build output with Linux.
Separate distro clones test different userspaces but still share the WSL kernel.

Install Linux toolchains and the system libraries required by the selected R packages.
Prepare cold dependency caches serially before concurrent transcript runs.
Use the same development Python as CI.
The isolated `sitecustomize` fixtures are covered by the shared boundary tests.

## Validation ladder

| Command                             | Purpose                                                                       |
| ----------------------------------- | ----------------------------------------------------------------------------- |
| `scripts/test --full --list`        | Discover functional cases without building.                                   |
| `scripts/test --locate SELECTOR`    | Find source and snapshots.                                                    |
| `scripts/test BOUNDARY/SUITE::CASE` | Focused red/green loop.                                                       |
| `scripts/test --update SELECTOR`    | Regenerate an intentional snapshot change; then rerun without `--update`.     |
| `scripts/format`                    | Format all supported sources; inspect every reported result and the diff.     |
| `scripts/check`                     | Ordinary final local gate. `--quick` is an alias, not a narrower gate.        |
| `scripts/check --full`              | Applicable functional, tooling, and installation checks.                      |
| `scripts/test --stress`             | Original allocation-scale workloads, separate from ordinary functional cases. |

Start behavior changes with a failing public regression.
For a refactor, establish the existing public baseline.
Run focused tests after editing, review formatter output and `git diff --check`, then run the appropriate final gate.
The formatter's default reporting can return success despite a formatter failure; do not ignore its output.

On macOS/Linux, the ordinary check includes staging, embedded-source/architecture checks, Rust formatting/Clippy/tests, a release build, and the smoke transcript profile.
On Windows it uses the staged companion, a debug build, and native acceptance.
The smoke profile also compares documented preparation defaults with public configuration output and checks current guides' companion source links against `sandbox-runner.json`.
The link check scans Markdown and Quarto sources recursively and compares both the companion repository and revision.
Dated audit and benchmark records retain their original revisions; rendered website output is excluded.
The full gate adds applicable tooling, shared functional, and installation checks; installation runs last because it can replace staging/build directories.

CI runs the comprehensive profiles, including Unix allocation stress.
CI pins uv's Python catalogue so interpreter-dependent tracebacks and debugger transcripts change only with an intentional tool update.
A focused local pass or capability skip does not validate another platform.
Report exact commands and missing coverage rather than “all tests pass.”

### Selecting and sizing test runs

Selectors retain their scope under every profile.
An unscoped full run audits orphan snapshots; only a successful full update removes them.
Focused updates preserve unselected cases.
Never hand-edit snapshots.

The shared selector syntax works on every platform.
Windows native cases also use `CLASS.CASE`, for example `WindowsConsole.test_python_without_r`.
`--list` and `--locate` do not acquire build ownership.

Use `--jobs N` and `--timeout SECONDS` for shared-case concurrency and deadlines.
The default concurrency is `max(2, 2 * N)` for logical CPU count `N`, with two cases when unavailable.
Set `MCP_CONSOLE_TEST_BINARY` to an absolute installed executable to skip the checkout build; sandbox cases still require its complete companion bundle.

[Boundary tests](../tests/boundaries/README.md) owns fixture, snapshot, capability, execution-mode, and cancellation details.
The [concurrency comparison](benchmarks/transcript-concurrency.md) is dated evidence, not proof of a universally optimal setting.

## Documentation website

Quarto renders the Markdown guides, benchmark pages, and Python API reference.
R pkgdown adds its package site after the main render.
Historical design sketches, task templates, and root/test documentation are not all published as website guides.

Keep Markdown usable in the repository.
The website filter takes page titles from first headings, rewrites links outside `docs/` to GitHub, and maps the R README and published package-reference URLs to local rendered pages.
Keep source filenames descriptive; use source paths in navigation and relative Markdown links.
After Quarto and pkgdown render, `docs/_urls.py` publishes lowercase directory URLs, so `BUILTIN_RUNTIME.md` appears at `builtin_runtime/`.
The generated `index.html` files belong only to the build output; source files do not move.
The R and Python guides use `r-guide/` and `python-guide/`, leaving `r/` and `python/` for the package overviews.
The hook updates page links, asset paths, search, sitemaps, and the Python inventory while preserving symbol fragments.
Links from independently rendered package documentation must already use the published URLs; the hook only rewrites output built as part of the website.
It also highlights the current navbar page or its sidebar index, so Get started and Guides can share a sidebar without both appearing under Get started.
Partial renders update only the affected pages and their search entries.

Install Quarto, uv, R, pkgdown, and the R package dependencies:

```r
pak::pak(c("pkgdown", "local::r"))
```

```sh
scripts/preview-site
scripts/render-site
```

These scripts run `quarto preview docs --render html --no-navigate` and `quarto render docs --to html` with the Python documentation tools supplied by uv.
Run them from the repository root.
Additional arguments are passed to Quarto.
Use `scripts/preview-site --port 3604 --no-browser` to choose a port and keep the browser closed.
On Windows, invoke either script explicitly with `uv run --no-project --isolated --with-requirements docs/requirements.txt python scripts/preview-site` or `scripts/render-site` in its place.

The pre-render script `docs/_python.py` uses the pinned Great Docs generator to write Python reference sources before Quarto discovers pages.
The hook runs through uv, which prepares a separate environment from `docs/requirements.txt`; Great Docs does not need to be installed in the Python environment Quarto selects.
It preserves explicit members, inline methods, static introspection, symbol anchors, and source links.
Generated pages use the main site's Reference sidebar, theme, and search.
Preview starts with a full HTML render so Quarto discovers generated pages on a clean checkout, then rerenders changed pages.
Use `scripts/preview-site` so the URL hook retains the original outputs Quarto needs to track those renders.
The clean URLs use Quarto's reload client, and preview stays on the current page when a source changes.
Full renders remove the original outputs and omit the preview client.
Run `scripts/render-site` after editing Python docstrings or `great-docs.yml`, or to refresh the R package site.
Both commands write to `docs/_site`.

To check the rendered pages:

```sh
uv run --no-project --with-requirements docs/requirements.txt python tests/website.py
```

The website test renders a fresh temporary copy and checks titles, navigation, search, and local links/anchors, including both package references.
The CLI help page reads the normalized cross-platform YAML snapshot from CLI acceptance tests.
It publishes only `mcp-console`, `serve`, `warmup`, `sandbox`, `config`, `config init`, `config resolve`, and `configure-client` help, preserving the exact tested text.
Internal commands remain covered by acceptance tests.
To publish another public command, add it to the explicit allowlist in `docs/_filters/cli-help.py` and the independent expected command list in `tests/website.py`.
When CLI help changes, regenerate snapshots with `scripts/test --update cli/interface/test_help`, inspect the diff, and rerun without `--update`.
Never hand-edit snapshots; website rendering does not build or run Console.
The configuration page also embeds the complete starter file from the `config init` acceptance snapshot through `docs/_filters/config-init.py`.
Changes to the generated starter appear on the website when that snapshot is regenerated.
Quarto renders Python pages into `docs/_site/python/`; the post-render script builds `docs/_site/r/`.
Python reference uses static source analysis, and the R workflow vignette displays its examples without execution, so API credentials and a Console executable are not required for those builds.

Edit Python API docstrings and R roxygen sources for generated reference changes, not generated HTML.
The site configurations are `docs/_quarto.yml`, `great-docs.yml`, and `r/_pkgdown.yml`.
Generated Python sources, inventories, Markdown/LLM downloads, and build directories remain ignored.
Keep Great Docs internal API calls in `docs/_python.py` and update its exact dependency pin when changing the integration.
The Python reference filter resolves symbol links against the generated inventory; the main stylesheet owns reference presentation.

On page load, the guides and package references follow the browser's light or dark preference by default.
A saved choice from a page's theme control takes precedence over the browser preference.

### Documentation ownership

The top navigation links to [Get started](getting-started.qmd), [Guides](README.md), [Build](BUILD.md), and [Reference](REFERENCE.md).
Guides opens a compact reading list followed by the "I want to…" table.
Get started is a walkthrough of connecting an agent, running a first analysis, following up, and reviewing the work.
Both use one shared sidebar, beginning with an expanded Learn Console category: Get started, Sessions and languages, Packages and environments, Sandbox and trust, and Recordings and reports.
The Set up group covers installation and MCP client connections; Other tasks contains Run commands in a sandbox.
Build has its own sidebar for the data assistant walkthrough, R and Python integration, and driving `send` directly.
Its Builder reference links use published routes so the linked sources remain owned by the Reference sidebar.
Reference has one sidebar for configuration files, settings by concept, behavior, interfaces, and the glossary.
Development has its own sidebar and a footer link.
Quarto's previous/next links follow sidebar order so readers can walk through the learning chapters.
Keep the chapter continuations in the same order.
Each concept has one explanatory chapter, task guide, and reference topic where those roles are needed.
Link between those counterparts near the top of each page.
The home page and learning chapters link inline to guides; put exact-setting and behavior references at the end of a section so they do not interrupt the explanation.
The "I want to…" table connects tasks to guides; the Reference index maps setting names to their owning reference sections.

All settings accepted in `config.yaml` belong in the user reference.
Each reference topic has one owner: [Configuration files](CONFIGURATION.md) for discovery, merging, paths, and export; [Languages and startup](RUNTIME_CONFIGURATION.md) for R/Python selection, native startup, and database startup; [Tool configuration](TOOL_CONFIGURATION.md) for language visibility, descriptions, and accepted parameters; [Sandbox permissions](SANDBOX_CONFIGURATION.md) for permission fields and enforcement limits; [Package settings](PACKAGE_CONFIGURATION.md) for startup declarations, preparation policies, sources, caches, and preparation permissions; and [Environment variables](ENVIRONMENT.md) for environment settings and recognized variables.
The [Reference index](REFERENCE.md#configuration) links to these owners instead of repeating their field tables; do not add a second settings index to Configuration files.
[Language behavior](RUNTIME_REFERENCE.md) owns code execution, language helpers, SQL results, and output; [Recording reference](RECORDING_REFERENCE.md) owns storage and retention, while [Recordings and reports](RECORDING.md) explains reading and reusing a session.

[Build a data assistant](BUILD_ON_CONSOLE.md) introduces tool descriptions, language and parameter visibility, and prepared sessions for any connection method.
[MCP client setup](CLIENTS.md) belongs in Guides under Set up.
[R](R.md) and [Python](PYTHON.md) belong in Build, with direct links from Get started and the Guides task table.
Generated package manuals provide exact signatures and package-specific examples, with links back to the main documentation.
Exact MCP calls and requirement operations belong in [MCP send reference](API.md).
[Drive send directly](SEND_OPERATIONS.md) walks client authors through common operations; Python-specific result and cancellation details belong in the generated client API.
Keep compatibility anchors when moving public sections and include every new page in the appropriate sidebar and search index.
Architecture belongs in [Architecture](ARCHITECTURE.md); exact private message schemas belong with their source definitions.
Prefer a cross-reference over another explanation of restart, preparation, or sandbox trust.

Write procedures around a user's task.
Keep implementation algorithms, mutable file inventories, and one-off regression histories in source or dated evidence.
Record unfinished work in [TODO](TODO.md), with a link from the relevant guide; do not present future work as a supported option.

## Find the public test

Start at the outermost process boundary that observes the change.
Use `--full --list`, `--locate`, and scoped source searches.
Keep private-boundary tests specific to their seam instead of duplicating public text.

[Authoring cases](../tests/boundaries/AUTHORING.md) covers embedded programs and causal lifecycle fixtures.
Retain real-library sandbox regressions, including process creation and activation workflows; they test permissions that a trivial replacement program may not exercise.

## Review boundary

Before a cross-cutting change, identify its observable behavior, owning modules, public tests, expected platform/snapshot changes, and intended PR base.
Keep task-specific details in the checkpoint rather than permanent docs.

For a bug fix, record a minimal reproduction, expected and actual behavior, and relevant version/platform details before editing.
Tie the fix to that evidence and the public regression.

Review compatibility at the affected [public interfaces](API.md):

- MCP tool discovery, accepted arguments, result shapes, and error behavior.
- CLI flags and exit status; configuration discovery, precedence, and defaults.
- R and Python client signatures, return values, and connection lifetimes.

Check omission, `null`, empty values, and defaults where the affected interface distinguishes them.
Regenerate affected public discovery/help snapshots and update clients and the owning docs in the same change.
Keep private protocol evolution within the [documented compatibility boundary](API.md#what-is-not-a-public-compatibility-contract).

```sh
scripts/review-diff BASE
git diff --merge-base BASE
```

Use the intended parent for a stacked PR, not necessarily `main`.
Stage intended new files before measuring.
Line counts help review planning; they do not establish semantic scope.

## Native sandbox policy experiments

The hidden `sandbox --runner-config-env NAME` flag passes the named environment variable's JSON payload unchanged to the pinned native runner.
It bypasses application configuration discovery, validation, and generated policy additions, including managed temporary storage and Console's platform extensions.
The runner still validates and consumes its input.
This is an internal development escape hatch: neither the flag nor the runner's schema has a public stability guarantee.
Use the public `sandbox --config-env` interface for configuration in the `config.yaml` format.

The native selector is exclusive with `--config-env`, `--settings-env`, `--exit-with-parent`, configuration selection/defaults/overrides, and writable-root options.
Native lifecycle settings belong in the JSON payload.
The current companion revision is recorded in [sandbox-runner.json](../sandbox-runner.json); consult that revision's protocol for accepted fields.
Stdin remains the workload's input, and native environment/argument size limits apply.

Native filesystem kinds have different enforcement limits:

| Kind               | Meaning without a proxy                                                                                  |
| ------------------ | -------------------------------------------------------------------------------------------------------- |
| `restricted`       | Native filesystem rules, selected networking, and native supervision.                                    |
| `unrestricted`     | Full OS-permitted filesystem access; entries do not narrow it. Native supervision/network policy remain. |
| `external-sandbox` | Delegate filesystem and networking to an outer boundary. The runner does not create or verify it.        |

In external mode without a proxy, `network: restricted` installs no network restriction.
Windows rejects external mode and requires MXC for native policy launches too.
On Linux, external mode supervises the original group/direct child; the outer boundary must retire escaped descendants.
A managed proxy can add native networking while filesystem enforcement remains delegated.
Full filesystem writes can influence unsandboxed processes and shared artifacts; restricted-policy isolation guarantees do not extend to hostile unrestricted workloads.
Cleanup is not proof of isolation.

## Checkout ownership

Build, staging, checking, and packaging share `.dev-workflow/checkout.lock`, outside `target`.
Use `scripts/with-checkout` for direct mutating Cargo/Maturin commands.
Do not delete locks, run concurrent nested mutators under one ownership token, or share application build/staging directories between checkouts.

Separate worktrees can run concurrently.
The pinned companion cache has its own shared staging lock.
Cancellation retains ownership until the workflow's process-cleanup contract completes; Windows phases use Jobs.
After owner crashes or forced termination, establish that surviving mutators stopped before restarting.
A stale lock diagnostic alone proves neither activity nor cleanup.

## Resume from a small checkpoint

Copy [the template](templates/task-checkpoint.md) to the ignored `.dev-workflow/task.md` for a new task.
Record scope, branch/base, revision, working edits, validation evidence, next action, and the requested stopping point.
Do not overwrite an existing checkpoint before checking it.

On resume compare it with Git status, HEAD, base, staged/unstaged diffs, and untracked files.
Validation records under `.dev-workflow/runs/` describe the revision/worktree at admission; an unfinished record does not prove a process is still running.
Establish cleanup with its original owner before rerunning.

Report actual validation and hosted CI/review status separately.
Opening a PR does not imply permission to merge, wait indefinitely for CI, or start a watcher.

## Preparation evidence

Exact cache fallback formulas and generated grants belong in `src/resolver/cache.rs`; do not copy those implementation formulas into other guides.
The [release trust audit](RESOLVER_RELEASE_AUDIT.md) is dated evidence, not approval of an arbitrary later commit.

## Linux platform validation

```sh
scripts/test cli/sandbox/test_procfs
scripts/test cli/sandbox/test_configuration
python3 tests/sandbox_installation.py target/release/mcp-console
```

The procfs matrix compares fresh/inherited views with readable/denied sentinel paths and restricted/enabled networking.
It checks direct policy behavior and attempts through process metadata, descriptors, memory, signals, and namespace entry.
Host restrictions must not substitute for the sandbox property under test.

Namespace-link access and namespace entry are separate assertions, resolving [SP-5](https://github.com/t-kalinowski/mcp-console/issues/603).
The trusted fixture driver first proves that it can enter its disposable network namespace, then supplies a validated handle to the sandboxed target.
The disposable host process drops the driver's capabilities so they cannot conceal a ptrace bypass.
This keeps namespace entry reachable even when procfs denies access to the host process's namespace link, and retains exact errors for both checks.

These probes establish specific contracts, not proof against all kernel attacks.
Fixture process-observation requirements are separate from production requirements.
[Boundary tests](../tests/boundaries/README.md) covers capability skips and safe namespace-PID handling.

Release acceptance additionally checks the installed bundle with an empty PATH and native floor-runtime images.
See [Release](../RELEASE.md#linux-floor-validation); an ABI report, successful normalization, or skipped native probe is not a sandbox acceptance result.
