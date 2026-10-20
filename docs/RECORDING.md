# Recordings and reports

Console keeps transcripts, text output, and plots on the machine where it runs.
Ask the agent for the recording's path, or use the locations below.

Recordings contain source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.
There is no aggregate quota or automatic cleanup, so remove old recordings deliberately.

## Location and files

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.

Console does not create a project `.agents` directory just to select project storage.
The first received MCP `tools/call` targeting `send` activates a recording for the Console process.
Polls, input, controls, rejected submissions, and requests cancelled before execution all activate recording.
Initialization, tool discovery, pings, startup output, and startup failure alone do not create a persistent recording.

Console retains preceding protocol traffic and startup text/images temporarily.
The first send publishes that prelude in its observed order with its original timestamps.
If the process exits without send, Console discards the temporary capture.
The recording retains the process start time separately from publication time.
Restarts and automatic worker replacement create numbered sessions within that same recording.

| File                      | Use                                                |
| ------------------------- | -------------------------------------------------- |
| `transcript.md`           | Read calls, results, and links to artifacts.       |
| `transcript.qmd`          | Review submitted source and edit it into a report. |
| `internal/events.jsonl`   | Inspect recording events as JSON.                  |
| `outputs/call-NNNNNN.log` | Read raw text from a code cell, up to 1 GiB.       |
| `outputs/session.log`     | Read startup and idle text, up to 1 GiB.           |
| `artifacts/`              | Find retained images.                              |

`internal/events.jsonl` records one JSON object per event, including MCP requests, responses, and notifications, canonical tool calls/results, worker lifetimes, dependency selections, and references to output logs and images.
Protocol events reference the canonical send call/result records instead of duplicating their payloads.
A response assembly event and a completed local transport write are separate observations.
Neither proves that the client acknowledged the response or that a model read it.

Worker events record actual start, readiness, and termination.
A restart request does not itself establish a session; a failed preparation or launch that never acquires a worker has no session number.
Interrupting a surviving worker stays in its session.
The journal can help inspect a recording with scripts or investigate a failed call.
Its format may change between Console releases.

Returned paths refer to the Console host.
Project paths are normally relative to the launch directory; fallback paths are absolute.
A long-location notice may instead name a path relative to the selected Console directory.

## Retrieve omitted output

Each cell gets a log file when execution starts, even if it produces no text output.

Console limits how much text it sends to the agent at once.
When a response leaves out text, a notice points to the existing log file and says if any output could not be saved.
The agent can read that file with its client's file-reading tools, without running the code again.

The agent can also read the log while the code is still running.
Console updates the log before sending each response.
Reading it does not remove output from later responses.

Logs contain the text that R, Python, or SQL displayed.
For example, if a table's display includes only a few rows, the log contains that abbreviated display.
To get the complete data, ask the agent to fetch it or save it to a file.
For SQL, your agent can [retrieve the last result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result) in R or Python without running the query again.

## Render a reviewed copy

`transcript.qmd` is a starting point for a report, **not a replay or checkpoint**.
It can contain failed evaluations and rejected calls.
It omits interactive input, control operations, output, and recorded images.

Copy it before editing:

```sh
cp transcript.qmd report.qmd
```

Review the code, inputs, package declarations, and evaluation settings in `report.qmd`.
Rendering executes code outside the Console sandbox.

After reviewing the copy:

```sh
uv tool run --from r-lib-ir ir render report.qmd
```

With `ir` already installed, use `ir render report.qmd`.

Rendering requires R, even when the original session used only Python.
SQL chunks need a user-supplied DBI connection.
Package declarations are not a lockfile or a complete history of the environment.

Some recordings disable evaluation because runtime information is unavailable or the session changed between incompatible dependency declarations.
Review those boundaries and choose the rendering environment explicitly rather than simply enabling every chunk.

## Recording failures

Recording can fail while computation continues.
A notice identifies files or artifacts that are unavailable or only partially retained; do not assume all output was saved.
If a stalled journal writer exhausts the bounded protocol backlog, Console disables recording and reports the failure on stderr while continuing to receive MCP traffic.

The recording journal and generated file formats are not independently versioned APIs.
Implementation and failure-handling details belong in [Architecture](ARCHITECTURE.md#output-and-delivery).
