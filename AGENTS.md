# Working on MCP Console

Read [Development](docs/DEVELOPMENT.md) for setup and validation, [Architecture](docs/ARCHITECTURE.md) for ownership, and the relevant contract from the [documentation index](docs/README.md).
Do not load every document for every task.
Source and public acceptance tests settle disagreements with prose.
`design-sketches/` contains proposals and history, not current requirements.

## Change discipline

Keep a behavior-changing PR to one observable change.
Aim for fewer than 500 added/deleted implementation lines, but prefer a coherent change to an artificial split; mechanical moves, tests, snapshots, and docs are exempt.

Start behavior changes with a failing public regression.
Establish an existing public baseline for internal refactors; do not test private helpers.
Preserve generation ownership and confirmed-retirement barriers.
Submitted code is shell-class capability; dependency preparation has a separate trust boundary and policy.

Console-owned private protocols evolve in lockstep without independent versions.
Update both endpoints and fixtures together.
Preserve external protocol versions, the separately pinned companion contract, and component/artifact identity checks.

Prefer event-driven waits with explicit cancellation.
Add abstractions for implemented responsibilities, not planned features.
Reassess large files rather than enforcing a line limit; retain one Cargo package until a concrete crate boundary emerges.
Keep production R/Python programs under `src/` compile-time embedded, not loaded from checkout files at runtime.

Update the owning document when a contract changes.
Explain decisions and invariants, not line-by-line mechanics.
Document intentional limitations beside the owning code or contract, with enough detail to distinguish accepted scope from a regression.
Keep user contracts separate from developer internals.
Consolidate unfinished work in [TODO](docs/TODO.md); do not silently promote historical proposals into release requirements.

## Implementation

Keep module visibility narrow and reuse existing owners and helpers.
Prefer named methods or enums when positional booleans or `None` obscure a call.
Prefer exhaustive matches on Console-owned state enums so adding a state requires an explicit decision.

Keep straightforward, single-use code inline unless a helper names a meaningful responsibility or invariant.
Document new traits' purpose and the obligations of their implementations.
When extracting code, keep its invariant comments and module documentation with the implementation.
Follow nearby conventions and keep unrelated style changes out of behavior changes.

## Validation

```sh
scripts/preflight
scripts/test BOUNDARY/SUITE::CASE
scripts/format
scripts/check
```

Run `scripts/format` unchanged before committing and review all output: its default mode can report failures without failing the command.
Use `scripts/check` as the ordinary final gate; use `--full` when requested or appropriate to the change.
Report commands actually run and unavailable coverage.
CI is the comprehensive merge gate.

Use Windows `.cmd` launchers or explicit Python invocation there, and follow [Windows validation](docs/WINDOWS.md#validation).
Stage its companion before direct native checks.
Use `scripts/with-checkout` for direct build commands; never bypass checkout ownership locks or reuse a busy worktree.

## Tests and snapshots

Use temporary workspaces and `MCP_CONSOLE_HOME`, preserving `HOME` and the caller's tool environment.
Declare platform/capability requirements in shared test support.
Retain real-library sandbox regressions where the library's APIs exercise distinct permissions.

Never hand-edit `tests/snapshots/`.
Regenerate through `scripts/test --update` or formatting, inspect the diff, and rerun without `--update`.
Prefer shared cross-platform transcripts.
Make fixtures deterministic and normalize incidental paths/presentation only after assertions; preserve complete errors, tracebacks, status, and meaningful ordering.

Platform variants must test the exact differing contract and explain it in their decorator's `reason`.
Handshake defaults, line endings, or executable suffixes alone do not justify duplicating an otherwise portable test.
Keep initialization and tool discovery near the top, normally with the exact canonical `!same-as` comparison, and generally one Console invocation per transcript.
Different/incomplete exchanges remain in full.
Fix the producer or serializer rather than masking incorrect output.

Use readable multiline programs with `# fmt: r` or `# fmt: python` immediately above `code(` and its opening delimiter; indent the payload and closing delimiter one level deeper.
See [Boundary tests](tests/boundaries/README.md) and [Authoring](tests/boundaries/AUTHORING.md) for detailed rules.

## Local work and publishing

Verify any `.dev-workflow/task.md` against Git status when resuming.
Record the actual revision and evidence, not a generic “dirty” label or an old pass.

Before merging, require passing CI and a verified thumbs-up reaction from the GPT connector reviewer for the **current PR head**.
A completed review without findings is not approval.
Follow [RELEASE.md](RELEASE.md) before pushing a release tag.
Never yank or remove published PyPI files.
Respect the user's requested stopping point; do not infer merge or publication authorization.
