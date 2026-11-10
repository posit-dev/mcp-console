# MCP tool descriptions

Tool descriptions recur in agent context.
Include what changes tool selection, call construction, or result interpretation.
Tutorials belong in guides; implementation explanations belong beside the code.

## Source and stability

[presentation.rs](../src/server/presentation.rs) composes [named prose sections](../src/server/presentation/sections.rs); [arguments.rs](../src/server/arguments.rs) owns the input schema.
The [canonical handshake](../tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml) records the advertised definition.
Edit those sources rather than copying their full text into documentation.

Presentation follows captured configuration and stays stable through startup success or failure.
Language visibility selects fields and examples, independently of runtime availability and permissions.
Dependency guidance follows existing or managed Python selection and its resolution policy.
Hidden declaration fields must not leave instructions telling the agent to use them.
Requirements may still be needed by a hidden SQL provider.

The definition also carries a static BaseMetadata `title`: a single-language interface names its language ("Run R code"); an interface with several languages advertises "Run code", since one shared title cannot name a single language.
Clients that aggregate several calls into one row or header (such as chat tool groups) need a stable shared label; a static title keeps that header from adopting one call's dynamic result title, and per-call titles stay distinct inside the group.
The title is server-owned: launcher configuration has no title override.

[Tool configuration](TOOL_CONFIGURATION.md#tool-descriptions-and-parameters) applies launcher overrides after generation.
It can replace prose verbatim or hide fields while preserving omission defaults.
Exports retain explicit overrides and leave generated Python parameter descriptions unset for regeneration at launch.

## Deferred tool discovery

Start server instructions and the `send` description with short, task-oriented summaries.
A client may use one before exposing the full tool definition.

MCP [initialization](https://modelcontextprotocol.io/specification/2025-11-25/basic/lifecycle#initialization) returns server `instructions`; [tool discovery](https://modelcontextprotocol.io/specification/2025-11-25/server/tools#listing-tools) returns each tool's `title`, description, and schema.
The client decides which text reaches the model and when.
Fetching a definition does not prove that the model has seen it.
The R `console_tools()` adapter uses advertised tool definitions to construct ellmer tools; it does not add initialization instructions to the chat prompt.

Do not rely on a particular client's preview length or discovery strategy.
Keep the opening useful by itself, and recheck affected integrations when changing client-specific guidance.

## Editorial rules

Lead with useful tasks and retained state, then explain complete cells and automatic display.
Encourage choosing the easiest language for each step when multiple languages are visible.
Offer bridge examples only when both R and Python are exposed, and describe custom-worker capabilities without promising built-in bridges.

Give each fact one primary home:

| Location        | Information                                                                            |
| --------------- | -------------------------------------------------------------------------------------- |
| Tool summary    | When to choose Console, sequential cells, polling, state, and selected trust boundary. |
| Language fields | Display rules, plots, and exact bridge/connection/result helper names.                 |
| `session`       | Named-session creation, concurrency, aggregate polling, and deferred restart.          |
| `timeout_ms`    | Observation deadline and long polling for unfinished work.                             |
| `stdin`         | Exact queued bytes with no added newline.                                              |
| `control`       | Interrupt/restart effects, including state loss and partial effects.                   |
| `requirements`  | Preparation and ordering; action/package syntax in nested fields.                      |

Preserve omission defaults where clients may not render schema defaults.
Keep empty-send polling, error partial effects, and restart state loss explicit.
Response notices own current state, missing prerequisites, and retained-output locations.
Ordinary calls should not require package inspection or a restart.

Keep exact helper names and essential restrictions in the tool definition: the agent may not have the checkout.
Avoid duplicating the generated prose in guides or turning it into a package list or protocol manual.

## Capability and security claims

Advertised languages are configured capabilities, not proof of installed runtimes or permission to install packages.
Dependency declarations are not installed-package inventories.
Preparation and worker sandbox settings govern separate boundaries.

Describe selected native permissions and concrete temporary/workspace write grants, including narrower read/deny rules.
A read entry grants reads as well as narrowing writes; metadata protection is a default rather than a mandatory ceiling.
Language visibility is not a security boundary.

## Comparing the advertised definition

Capture the canonical handshake and affected variants, then regenerate through:

```sh
scripts/test --update client_server/server/test_tools::initializes_and_lists_tools
scripts/test client_server/server/test_tools::initializes_and_lists_tools
```

Inspect every changed description and the complete tool object.
For a prose-only change, remove `description` fields recursively and verify the remaining schema and metadata are unchanged.
Use the same serialization/tokenizer for size comparisons and report UTF-8 bytes alongside tokens.
Agent evaluations, tracked in [TODO](TODO.md#documentation-and-agent-usability), are needed to establish behavioral improvement; a smaller definition alone does not do that.
