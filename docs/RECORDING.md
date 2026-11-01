# Recordings and reports

Console records the agent's calls, text output, and plots on the machine where it runs.
You can use that record to check how it reached a result, inspect output that did not fit in a response, or turn reviewed code into a report.

Recordings contain notes, source, input, requirements, output, and artifacts **without redaction**.
Review them before sharing.

## Read the recording {#location-and-files}

Ask the agent for the recording's path, then open `transcript.md` in that directory.
It shows calls and results across all worker sessions, including recorded images and links to retained output.
Console announces each session bundle when it first becomes available, with paths to its source and report on the Console host.
The transcript links to the same bundles in `sessions/session-NNN/`.
Each bundle contains `session.qmd` with notes, code, and environment metadata, `session.md` with notes and recorded response previews, and an `images/` directory for portable image links.
An application using the [integrator control channel](INTEGRATOR_CONTROL.md#select-recording-projections) can omit its own calls from these documents while retaining their journal and raw capture.
Raw text and images are grouped under `capture/sessions/NNN/` by worker session and observed startup, cell, or post-cell interval.
Completed files are read-only; copy a file before editing it.
For the dice example in [Get started](getting-started.qmd), you can inspect both the simulation and the later comparison that reused its results.

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

Notes appear as prose in `transcript.md` and the associated session QMD and report, before code submitted in the same call.
Each QMD preserves call order, so notes sent between cells remain between those cells even when their worker associations become known later.
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

## Copy a session bundle {#export-a-working-copy}

Ask the agent for the session bundle path announced in its tool response.
Open `session.md` to read source and recorded results, including previews, errors, omission notices, and images.
Results collected by later polls stay with their cells; startup and between-cell output remain distinguishable.
Console updates this report as it assembles responses, without executing code, repeating SQL, recomputing plots, or invoking Quarto.

Copy the entire bundle, including `images/`, using file tools that can read the Console host and write your chosen destination.
Those tools' filesystem permissions apply; the announced path does not grant sandbox access.
There is no runtime export helper or destination directory argument.
If submitted code cannot write the destination, use the client's file tools or copy the bundle yourself.

A live bundle can change while it is copied.
Wait until the Console process finishes for a completed record, and make your working copy writable before editing if the copy preserved read-only permissions.
Included images use relative links and remain usable after the bundle is moved.
Supplementary raw logs mentioned in omission notices remain in capture and are not included in the bundle.

See the [recording reference](RECORDING_REFERENCE.md#session-bundles) for completion and delivery limitations.

## Turn the code into a report {#render-a-reviewed-copy}

Each `sessions/session-NNN/session.qmd` is a starting point for a report, **not a replay or checkpoint**.
It contains notes and the R, Python, and SQL source associated with that worker, including failed evaluations, rejected calls, and source submitted alongside a control.
Restarts and automatic worker replacement begin a separate document; an interrupt that preserves the worker stays in the same document.
A note sent with code follows that code into its worker's document.
A note without code belongs to the current worker, or the next worker if none exists yet.
The documents omit interactive input, polling, control operations, output, and recorded images.

Copy it before editing:

```sh
cp sessions/session-001/session.qmd report.qmd
chmod u+w report.qmd
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
Each document records accepted requirements for its worker session and describes the original working directory without setting the rendering directory.
Copy required inputs beside the working copy or adjust their paths before rendering.
Chunks permit errors so a recorded failure does not prevent reviewing later code; review those failures before relying on the report.

<span id="storage-and-retention"></span> <span id="recording-failures"></span>

Reference: [Recording storage, files, and retention](RECORDING_REFERENCE.md).

For other workflows, the [documentation index](README.md#task-index) links to guides by what you want to do.
To prepare Console for a particular project or agent, continue with [Build a data assistant](BUILD_ON_CONSOLE.md).
