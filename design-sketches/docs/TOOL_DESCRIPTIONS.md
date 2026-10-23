# Registered MCP Tool Descriptions

**Status:** Draft v0.2 \
**Date:** 2026-07-27

This document contains the exact descriptions intended to be registered with the MCP server.
Keep these synchronized with [`MCP_INTERFACE.md`](MCP_INTERFACE.md) and the implementation.
These strings are part of the agent-facing interface and should change only when the added context materially improves tool selection or correct use.

## `send`

```text
Persistent R, Python, and DuckDB SQL console. Use it whenever exact computation or direct inspection would improve accuracy—from arithmetic, string counting, parsing, and file or binary-data inspection to data wrangling, exploratory analysis, visualization, statistics, simulation, and model training or tuning. State persists across calls; R and Python exchange objects, and SQL queries live or registered tabular data. Language-native help, introspection, interactive input, and debuggers work. Send exactly one complete `r`, `python`, or `sql` cell, optionally with `stdin`; send `stdin` on its own to queue exact text to the session worker; send neither to wait/poll. Large values are previewed; oversized stdout/stderr, plots, artifacts, the Markdown transcript, and the source-only Quarto document are saved in the workspace.
```

Property descriptions:

- `r`: `Complete multiline R cell in persistent state. Python objects are available through py; R help, browser(), and recover() work.`
- `python`: `Complete multiline Python cell in persistent state. R objects are available through r; help(), breakpoint(), and pdb work.`
- `sql`: `Complete DuckDB SQL cell in the persistent catalog. Query live or registered tabular data; use SHOW TABLES, DESCRIBE, SUMMARIZE, and EXPLAIN for discovery. CLI dot commands are not supported.`
- `stdin`: `Raw text queued to the session worker's standard input, whether it is evaluating or idle. A single value may satisfy multiple reads; newlines are significant and are not added automatically. Queuing does not acknowledge consumption, and unread text may satisfy later reads.`
- `session`: `Persistent named session; defaults to default. Use another name for independent or concurrent state. A missing session is created by a code cell or nonempty stdin.`
- `label`: `Optional short heading for this cell in the Markdown transcript; it has no effect on execution.`
- `wait_ms`: `Maximum time this call waits for output or a state change. It never limits or cancels the computation.`

## `session`

```text
Prepare, inspect, or control persistent console sessions; normal evaluation and polling use send. Requirements are additive session configuration and survive runtime restarts. prepare creates the session if needed and adds requirements without replacing an existing runtime; if activation requires replacement, it reports that a restart is required. restart starts a fresh runtime generation; any existing in-memory R, Python, and SQL state is lost, while requirements, workspace files, and the transcript are retained. close ends the logical session.
```

Property descriptions:

- `action`: `Session operation: list, status, prepare, interrupt, restart, or close.`
- `session`: `Target session; defaults to default.`
- `requirements`: `Additive package requirements, valid with prepare or restart. Resolution runs outside the worker sandbox, where package installation or build code may execute on the host.`
- `requirements.r`: `` R package requirement strings. `ir` prevents installation from local package sources because it runs with server permissions. ``
- `requirements.python`: `PEP 508 Python requirement strings.`

## Inclusion rule

Descriptions should communicate facts that affect whether or how an agent calls the tools: breadth, persistence, interoperability, help and debugger support, cell/stdin/poll semantics, bounded outputs, environment persistence, and destructive lifecycle boundaries.

Do not include internal facts that do not change agent behavior: Ark, Jupyter, `harp`, `libr`, worker IPC, stack-frame implementation, the internal JSONL journal, or exact output limits.
DuckDB is named because it defines the SQL dialect and discovery commands.
