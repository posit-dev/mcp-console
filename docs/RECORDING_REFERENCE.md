# Recording reference

Console retains calls, source, input, requirements, output, and artifacts on the execution host, without redaction.
For reading a recording, retrieving omitted output, or making a report, see [Recordings and reports](RECORDING.md).

Jupyter kernels use the same recording owner through integrator execution.
`silent` and `store_history` control Jupyter publication and history; they do not disable Console's audit recording.
Shutdown settles the execution and recording before the kernel process exits, including when the request asks for restart.
The replacement starts a new recording from launch configuration.

## Storage and retention

If `.agents/console` already exists in the launch directory, recordings use `.agents/console/sessions/<run-id>/`.
Otherwise they use `~/.agents/console/sessions/<run-id>/`; `MCP_CONSOLE_HOME` replaces that fallback Console directory.
Console does not create a project `.agents` directory just to select project storage.
There is no aggregate quota or automatic cleanup, so remove old recordings deliberately.

The first received MCP `tools/call` targeting `send`, or a request on the optional [integrator control channel](INTEGRATOR_CONTROL.md), activates a recording for the Console process.
Polls, input, controls, rejected submissions, and requests cancelled before execution all activate recording.
Initialization, tool discovery, pings, startup output, and startup failure alone do not create a persistent recording.

Console retains preceding protocol traffic and startup text/images temporarily.
The activating request publishes that prelude in its observed order with its original timestamps.
If the process exits without either kind of request, Console discards the temporary capture.
The recording retains the process start time separately from publication time.
Restarts and automatic worker replacement create numbered sessions within that same recording.
Each cell gets a log file when execution starts, even if it produces no text output.

```text
recording/
├── capture/
│   ├── events.jsonl
│   └── sessions/
│       ├── 001/
│       │   ├── startup.txt
│       │   ├── startup-001.png
│       │   ├── cell-001.txt
│       │   ├── cell-001-001.png
│       │   ├── cell-001-post.txt
│       │   ├── cell-001-post-001.png
│       │   └── cell-002.txt
│       └── 002/
│           └── startup.txt
├── sessions/
│   ├── session-001.qmd
│   └── session-002.qmd
└── transcript.md
```

`transcript.md` records calls, source, results, polls, input, controls, and worker session boundaries, with links to source documents and retained output.
It preserves response previews and omission notices while leaving routine protocol and lifecycle details in the journal.
Results describe response assembly; local transport delivery is recorded separately in `capture/events.jsonl`.

Each `sessions/session-NNN.qmd` contains source associated with that actual worker lifetime, including failed and rejected source, with error-permitting chunk options.
Source admitted before worker acquisition stays pending until its worker is known, even if an initial response has already returned.
A validation-rejected restart leaves its source with the surviving addressed worker.
A failed acquisition does not create a session, and a session without source need not create a QMD.
Source without a worker association remains in the journal and Markdown transcript.

QMD metadata records accepted R and Python declarations, Python version and cutoff constraints, and DuckDB extension additions.
Retained R requirements carry into replacement sessions; rejected additions do not change metadata, and replacement declarations do not rewrite the retiring session's document.
The original working directory is descriptive metadata rather than an execution root.
QMDs omit results, images, input, polls, and control wrappers.
Existing recordings are not migrated or rewritten.

An integrator evaluation can set `record: false` to omit its source, result, and associated output from the Markdown and QMD projections.
Inspection requests on that channel also stay out of those documents.
Their journal events and raw capture remain retained under the same limits and failure behavior.

### Output intervals

Each actual worker session numbers executed cells from `001`.
Polls, input, controls, and rejected source do not create cells.
Source remains in the journal and documents even when it does not execute.

Output belongs to the interval in which Console observes it:

- `startup`: before the first cell begins, including the entire lifetime of a worker that executes no cells.
- `cell-NNN`: while that cell executes.
- `cell-NNN-post`: after the cell ends and before the next begins.

The final interval includes shutdown output.
A later poll can return output from several intervals or sessions without changing its ownership.
Console does not infer which computation caused background output or impose chronology across independent streams.

Every executed cell has a text file, including silent cells.
Startup and post-cell text files are created only when text arrives.
One file retains each interval's raw text, up to 1 GiB, even when text and images alternate.
Each retained image has its own numbered file in that interval; PNGs use `.png`, and other emitted image formats use `.bin`.
Raw images have a separate budget per startup, cell, or post-cell interval: 64 MiB of decoded image data, 4,096 files, and 64 KiB of MIME metadata.
An image is saved whole only if it fits the remaining raw budget.
The response preview has its own limits: images omitted from a preview can still be saved, and images that exceed the raw budget can still appear in a response.
Console retains emitted text and images, not arbitrary files written by submitted code.
Output observed before worker acquisition or after worker termination is retained in process-output events rather than assigned to a worker session.
Omission notices for that output reference the capture journal.

### Capture events and completion

`capture/events.jsonl` records one JSON object per event, including MCP and integrator requests and responses, MCP notifications, canonical calls/results, worker lifetimes, dependency selections, and references to output logs and images.
MCP protocol events reference the canonical send call/result records instead of duplicating their payloads.
Events preserve observation timestamps and sequence numbers and identify the session, cell, and interval explicitly.
Text events give byte ranges in the interval log so text/image ordering can be reconstructed.
Each retained image has an `image_retained` event with its owner, artifact ID, and file path relative to the recording directory.
An `image_omitted` event records an image rejected by the raw retention budget; it has no file to locate.
For an image returned without raw retention, the result record has `retained: false` and no file reference; it does not duplicate the image data in the journal.
Result records associate fragments of the assembled response with their observed intervals, preserving previews, prompts, errors, omission notices, and image references.
A response assembly event and a completed local transport write are separate observations.
Neither proves that the client acknowledged the response or that a model read it.

Worker events record actual start, readiness, and termination.
The `cell_started` event associates a call with its executed cell; accepted source that is withheld before execution remains in `tool_call` without a cell event or log.
A restart request does not itself establish a session; a failed preparation or launch that never acquires a worker has no session number.
Interrupting a surviving worker stays in its session.
The journal can help inspect a recording with scripts or investigate a failed call.
Its format may change between Console releases.

Completed managed files become read-only while retaining their existing privacy permissions.
Cell logs close at their cell boundary; startup and post-cell logs stay writable until their next boundary.
Images become read-only after writing.
The journal, Markdown transcript, and session QMDs remain writable until worker output, queued capture, source associations, result writers, and transport settlement can no longer update them.
Directories remain writable while Console needs them.
These permissions protect against accidental edits; abrupt termination can leave files writable.

Returned paths refer to the Console host.
Project paths are normally relative to the launch directory; fallback paths are absolute.
A long-location notice may instead name a path relative to the selected Console directory.

<span id="recording-failures"></span>

Recording can fail while computation continues.
A notice identifies files or artifacts that are unavailable or only partially retained; do not assume all output was saved.
If a stalled journal writer exhausts the bounded protocol backlog, Console disables recording and reports the failure on stderr while continuing to receive MCP traffic.

The recording journal and generated file formats are not independently versioned APIs.
Implementation and failure-handling details belong in [Architecture](ARCHITECTURE.md#output-and-delivery).
