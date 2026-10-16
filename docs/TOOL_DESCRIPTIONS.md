# MCP tool descriptions

Descriptions recur in agent context.
Include information that changes tool selection, call construction, or result interpretation.
Keep tutorials in user guides and implementation explanations in developer docs.

## Source and stability

[`src/server/presentation.rs`](../src/server/presentation.rs) composes [named prose sections](../src/server/presentation/sections.rs); [`src/server/arguments.rs`](../src/server/arguments.rs) owns the input schema.
The [canonical handshake](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the advertised definition.

Presentation comes from captured configuration, not completed background discovery.
It stays stable when startup succeeds or fails.
Configured languages control visible code fields and relevant examples; actual runtime availability is checked during execution.
Python dependency guidance follows the captured existing/managed selection and managed resolution policy, including launch-time `RETICULATE_PYTHON` when no Python selection is configured.
Existing environments use installed packages; managed environments describe explicit declarations, automatic import preparation, or fixed startup requirements as selected.
Under explicit resolution, hiding `requirements.python` or its parent switches generated Python guidance to installed packages.
Configuration exports leave generated Python parameter descriptions as `null` so later launches regenerate them after selection; explicit description overrides are preserved.
Requirements can remain visible for a hidden SQL provider.

Launcher [tool configuration](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters) applies after generated presentation.
It can replace tool prose, initialization instructions, and nested parameter descriptions verbatim, or hide parameters while preserving their omission defaults.
The editorial guidance below governs generated text; explicit replacements are owned by the launcher.

Do not equate visibility with initialization, installation, or a security boundary.
Bridge examples require both source languages to be visible; Python-owned SQL registration must not imply that an R-owned connection is a DB-API object.

## Deferred tool discovery

MCP initialization instructions introduce the server; the `send` definition explains the tool.
Start both with a short task-oriented summary so deferred-discovery clients can choose Console before loading its full schema.

These are separate protocol fields: [`initialize`](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization) returns server `instructions`; [`tools/list`](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#listing-tools) returns each tool's `description` and `inputSchema`, including argument descriptions.
Initialization makes instructions available to the client before it lists tools.
The client decides which text to put in the model's context and when.
Fetching a definition from Console does not necessarily expose that definition to the model immediately.

With deferred loading, a server summary can help the model decide where to search before it receives individual tool schemas.
[Claude Code's server-author guidance](https://code.claude.com/docs/en/mcp#tool-search-for-mcp-server-authors) describes using initialization instructions for that decision.
Other integrations can use a separately supplied server description, as in the [Responses API tool-search workflow](https://developers.openai.com/api/docs/guides/tools-tool-search#use-namespaces-where-possible).
Instructions are therefore relevant beyond the connection handshake, but their visibility is client-dependent.
The R `console_tool()` adapter uses the tool description and schema to construct its ellmer tool; it does not automatically add initialization instructions to the chat prompt.

The implementation review recorded on October 9, 2026 found a 250-character first-line preview in the examined Codex deferred-discovery implementation and a documented default 2,048-character cap in Claude Code.
These are dated client behaviors, not MCP limits.
See the [pinned Codex preview implementation](https://github.com/openai/codex/blob/36ae1561b9324c93d5638b45eb19fe2cc070a581/codex-rs/core/src/context/world_state/tools.rs) and [Claude Code author guidance](https://code.claude.com/docs/en/mcp#for-mcp-server-authors).

Use those sizes as practical editorial targets, not guarantees that every client exposes all of the text.
Recheck client behavior when revising discovery guidance.
Detailed budget allocation and client feature flags do not belong in Console's public contract.

## Editorial rules

Lead with tasks: calculations, data analysis, simulations, debugging, queries, plots, and comparing approaches with retained state.
Follow with complete-cell and automatic-display guidance.

Give each fact one primary home:

| Location        | Information                                                                                            |
| --------------- | ------------------------------------------------------------------------------------------------------ |
| Tool summary    | When to choose Console, sequential cells, polling, state persistence, and the selected trust boundary. |
| Language fields | Display rules, plots, exact bridge/connection helper names.                                            |
| `timeout_ms`    | Observation only; recommend long polls for unfinished work.                                            |
| `stdin`         | Exact queued bytes, no added newline.                                                                  |
| `control`       | Interruption and restart effects, including state loss.                                                |
| `requirements`  | Preparation/ordering restrictions; action and package syntax in nested fields.                         |

Preserve defaults in prose where missing schema-default rendering would be misleading.
Keep the empty-send polling rule and discourage repeated one-second polls.
Preserve warnings that errors do not roll back effects and restart discards live state.

Ordinary use should not require declaration inspection, preparation, language switching, or restart.
Do not turn the description into a package list, protocol guide, or help API.
Response notices own current state, missing prerequisites, and omitted-output locations.

Do not remove a capability's only usable explanation in favor of a repository link: the agent may not have the checkout.
Exact helper names and essential restrictions must remain available in the tool definition.

## Capability and security claims

Describe configured capabilities without claiming that dependencies are already installed.
An advertised language can fail because the runtime is unavailable; advertising it does not authorize installation.
Existing Python, bare runtimes, and custom workers have different preparation support.

Policy text must reflect selected native permissions.
Name the configured temporary and workspace write grants and concrete writable paths, subject to narrower read/deny rules.
Metadata protection is a default, not a mandatory ceiling.
A read entry grants reads as well as narrowing writes.

Do not imply that resolver settings and worker settings are the same boundary, or that a declaration is an installed-package inventory.
Keep supported interrupt-plus-cell partial effects explicit.

## Comparing the advertised definition

Capture the canonical handshake and affected configuration/platform companions before editing.
Regenerate through:

```sh
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
```

Then run the focused case without updating.
Compare the top-level description, all nested descriptions, and the complete tool object.
Remove `description` fields recursively and require unchanged remaining schema/metadata for a prose-only edit.
Argument order may change without changing schema values.

Use the same compact serialization and tokenizer for comparisons; report UTF-8 bytes alongside token counts.
Smaller text alone does not demonstrate better agent behavior.
[TODO](TODO.md#documentation-and-agent-usability) records the evaluation work; do not claim a wording improvement is measured without an evaluation.
