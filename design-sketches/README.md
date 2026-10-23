# Design archive

These files preserve exploratory and historical designs.
They can be incomplete, mutually inconsistent, or superseded.
**They are not documentation of the current product or an implementation checklist.**

Use the [current documentation](../docs/README.md), [public interfaces](../docs/API.md), and [architecture](../docs/ARCHITECTURE.md) for implemented behavior.
Follow the repository-root [AGENTS.md](../AGENTS.md) when changing production code.

## Find a proposal

| Material                                                                              | Subject                                                                     |
| ------------------------------------------------------------------------------------- | --------------------------------------------------------------------------- |
| [Vision](VISION.md)                                                                   | Product goals and possible future experience.                               |
| [Configuration](CONFIGURATION.md)                                                     | Earlier configuration, session, and target designs.                         |
| [MCP interface](docs/MCP_INTERFACE.md), [CLI](docs/CLI.md)                            | Proposed tools and commands, including interfaces that do not exist today.  |
| [Sidecar API](docs/SIDECAR_API.md)                                                    | Viewer, typed inspection, tables, snapshots, and external control.          |
| [Architecture](docs/ARCHITECTURE.md)                                                  | Earlier process/runtime design and implementation plans.                    |
| [Runtime backend](docs/RUNTIME_BACKEND.md), [R DLL REPL](docs/R_REPL_DLL_ITERATOR.md) | Backend comparison and experimental findings.                               |
| [Peer-runtime completion](peer-runtime-completion.md)                                 | Runtime transition history, including now-stale target/startup assumptions. |
| [Tool descriptions](docs/TOOL_DESCRIPTIONS.md), [agent context](AGENTS.md)            | Historical prose and project assumptions.                                   |

The current Console exposes one `send` tool and one session per connection.
Older references here to a separate `session` tool, named sessions, viewers, remote targets, or R hosting Python are not current contracts.

## Using a sketch

Select a concrete goal before treating a proposal as work.
Reconcile it with current source, tests, and trust boundaries.
Record selected unfinished work in [TODO](../docs/TODO.md) or the issue tracker; do not promote the archive wholesale into release scope.

The original expansive index remains in [Git history](https://github.com/t-kalinowski/mcp-console/blob/b9caf7c55e9f9fbb0ce33582e4c61d615813b4eb/design-sketches/README.md).
Historical documents are intentionally kept separate from the website's ordinary reading path.
