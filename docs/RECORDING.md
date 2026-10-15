# Recordings and reports

Console keeps transcripts, text output, and plots on the machine where it runs.
Ask the agent for the recorded session's path, or use the locations below.

Recordings contain source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.
There is no aggregate quota or automatic cleanup, so remove old sessions deliberately.

## Location and files

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.

Console does not create a project `.agents` directory just to select project storage.
Recording starts when a call, startup output, or startup failure needs it.

| File                      | Use                                                |
| ------------------------- | -------------------------------------------------- |
| `transcript.md`           | Read calls, results, and links to artifacts.       |
| `transcript.qmd`          | Review submitted source and edit it into a report. |
| `outputs/call-NNNNNN.log` | Read raw text from a code cell, up to 1 GiB.       |
| `outputs/session.log`     | Read startup and idle text, up to 1 GiB.           |
| `artifacts/`              | Find retained images.                              |

Returned paths refer to the Console host.
Project paths are normally relative to the launch directory; fallback paths are absolute.
A long-location notice may instead name a path relative to the selected Console directory.

## Retrieve omitted output

The agent can use its client's filesystem tools to read the named raw log.
Console does not provide a file-reading tool.
Rerunning a cell is not a way to retrieve its previous output.

Reading a log does not consume pending Console output.
Logs receive text before preview truncation and are flushed when Console assembles a response, so they can be read while a cell runs.
Notices identify output that was omitted from a response and distinguish partial retention from a complete recording.

A log cannot recover values that a language printer or SQL preview never emitted.
Fetch or save the complete data explicitly when it matters.

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

The recording journal and generated file formats are not independently versioned APIs.
Implementation and failure-handling details belong in [Architecture](ARCHITECTURE.md#output-and-delivery).
