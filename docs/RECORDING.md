# Recordings and reports

Console records the agent's calls, text output, and plots on the machine where it runs.
You can use that record to check how it reached a result, inspect output that did not fit in a response, or turn reviewed code into a report.

Recordings contain notes, source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.

## Read the recording {#location-and-files}

Ask the agent for the recording's path, then open `transcript.md` in that directory.
It shows calls and results across all worker sessions, including recorded images and links to retained output.
Links labeled “Session NNN source (QMD)” open source documents in `sessions/session-NNN.qmd`.
Each contains the notes, code, and environment metadata for one worker lifetime; a restart begins a new document.
Recorded results and images remain in `transcript.md` and the capture files.
See the [recording reference](RECORDING_REFERENCE.md) for file layout, retention, and application-controlled omissions.

The recording documents what happened; it does not save the live Python objects, R objects, or in-memory database for a later session.
To repeat an analysis, you will need its inputs and reviewed code.

## Keep a journal

The agent can use `send`'s `note` argument to record observations, questions, and the purpose of each step as plain text or Markdown.
A note can accompany code:

```json
{
  "note": "Check the range before fitting a model.",
  "python": "min(measurements), max(measurements)"
}
```

It can also stand alone between cells:

```json
{
  "note": "The range is wider than expected. Next, inspect the largest values."
}
```

Notes appear as prose in `transcript.md` and the associated session QMD, before code submitted in the same call.
A note-only call returns without waiting for a running cell or collecting its output.
You can use these annotations to retrace an investigation or draft a narrative when reviewing the recording.

## Retrieve omitted output

Console limits how much text it sends to the agent at once.
When a response leaves out text, a notice points to the existing log file and says if any output could not be saved.
The agent can read that file with its client's file-reading tools, without running the code again.

The agent can also read the log while the code is still running.
Console updates the log before sending each response.
Reading it does not remove output from later responses.

Logs contain the text that R, Python, or SQL displayed.
For example, if a table's display includes only a few rows, the log contains that abbreviated display.
To get the complete data, ask the agent to fetch it or save it to a file.
For SQL, your agent can retrieve the last result in R or Python without running the query again.

Reference: [Retrieve the last SQL result](RUNTIME_REFERENCE.md#retrieve-the-last-sql-result).

## Turn the code into a report {#render-a-reviewed-copy}

Each `sessions/session-NNN.qmd` is a starting point for a report, **not a replay or checkpoint**.
It includes failed evaluations and rejected source, and omits interactive input, polling, controls, output, and images.
Review the recording to decide which code belongs in the report.

Completed files are read-only; copy a document before editing:

```sh
cp sessions/session-001.qmd report.qmd
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
The recorded working directory is descriptive metadata, not the rendering directory.
Copy required inputs beside the working copy or adjust their paths before rendering.
Chunks permit errors so a recorded failure does not prevent reviewing later code; review those failures before relying on the report.

<span id="storage-and-retention"></span> <span id="recording-failures"></span>

Reference: [Recording storage, files, and retention](RECORDING_REFERENCE.md).

For other workflows, the [documentation index](README.md#task-index) links to guides by what you want to do.
To prepare Console for a particular project or agent, continue with [Build a data assistant](BUILD.md#build-a-data-assistant).
