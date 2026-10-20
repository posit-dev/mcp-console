# Resolver release trust audit

**Historical evidence, not current release approval.** The original audit evaluated Console base `28b9ee4aa23037416a1f565c8e7d53fbcbb1a6ac` with companion `85d407d8a4544ed0916aff3c7a273461e739f215`, followed by the repair revisions below.
It did not evaluate every later source or companion pin.

The [complete original report](https://github.com/t-kalinowski/mcp-console/blob/b9caf7c55e9f9fbb0ce33582e4c61d615813b4eb/docs/RESOLVER_RELEASE_AUDIT.md) retains fixture details, local record IDs, and the full run history.
Current behavior belongs in [Package sources and caches](RESOLVER.md), [Requirements](REQUIREMENTS.md), and [Sandbox and trust](SANDBOX.md).

## Boundary evaluated

The audit asked whether mutable trusted resolver inputs could exceed the resolver's documented authority.
The server captures configuration and accepts declarations; workers can request named dependencies, not arbitrary resolver programs.
Materialization, package builds, Python inspection, and extension installation use a separate native sandbox on macOS/Linux.

Host reads and granted cache writes remain allowed.
Selected tools, configuration, sources, installations, and caches are not immutable.
Permitting a worker to replace them can influence later preparation; that must not grant additional controller authority.

## Evidence map

| Probe                                                   | Property examined                                                              |
| ------------------------------------------------------- | ------------------------------------------------------------------------------ |
| Replaced uv/ir wrappers                                 | Real preparation still rejects forbidden writes and unapproved downloads.      |
| Mutable uv configuration, local wheel, and source build | Build/startup code executes within resolver permissions.                       |
| Explicitly writable poisoned Python cache               | Cached hooks cannot gain writes through an escape symlink.                     |
| Worker-owned R profile                                  | Profile-free preparation does not source the project's profile.                |
| Native constructor and aliases                          | Restrictions apply before target `main`, including alias-based access.         |
| Result handoff, cancellation, and failures              | Failed or unretired preparation does not commit a candidate.                   |
| Default/custom caches and extensions                    | Captured paths, cache boundaries, and extension loading retain their contract. |

The tests required actual denial, successful permitted operations, and resolver-side receipts.
Environment markers alone were not containment evidence.
The poisoned-cache case explicitly granted worker writes; it did not describe the default policy.

These probes did not validate arbitrary packages, protect readable secrets, freeze files against concurrent replacement, make shared artifacts safe for unrelated host consumers, or establish cleanup after runner loss.
Windows preparation and `--no-sandbox` were outside the native resolver-isolation claim.
A demonstrated violation of the supported boundary still requires a regression and repair; an “unsupported” label cannot hide it.

## Recorded validation

Nine new cases passed locally on macOS with that companion.
Existing permission, handoff, cache, rollback, and retirement cases and ordinary checks supplied additional bounded evidence.

[Initial PR CI](https://github.com/t-kalinowski/mcp-console/actions/runs/37717478807) at `1bde04ee` recorded all nine new cases passing on Linux/macOS but failed overall in other cases.
[The subsequent main run](https://github.com/t-kalinowski/mcp-console/actions/runs/37718879271) passed Unix jobs and reproduced Windows interruption failures.

[Revised PR CI](https://github.com/t-kalinowski/mcp-console/actions/runs/37725473130) at `b9d702e4` passed Linux/macOS and Windows core/native acceptance, then failed existing Windows shared fixtures.
Later fixture repairs passed their recorded macOS checks but still required Windows verification in the original report.

The companion's [recorded CI run](https://github.com/t-kalinowski/cobox/actions/runs/37522396814) passed relevant native/executable contracts but failed a later lint step.
That was not an observed containment failure, and the job did not pass overall.

## Release decision and next verification

The original decision was **NO-GO pending passing applicable CI for the revised PR and final release candidate**.
It identified no demonstrated permission-boundary breach requiring restoration of the historical companion dependency.

That is a decision about the recorded revisions, not a claim that today's CI is failing and not approval of today's candidate.
Re-evaluate using the actual `sandbox-runner.json` pin, applicable trust cases on supported native platforms, final-head CI, and the full [release gates](../RELEASE.md).
Track that verification in [TODO](TODO.md#release-verification).
