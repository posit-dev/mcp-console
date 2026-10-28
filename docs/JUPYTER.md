# Run Console as a Jupyter kernel

`mcp-console kernel` runs one R, Python, or SQL session through the standard Jupyter channels.
Select the source language when launching the process:

```sh
mcp-console kernel \
  --language python \
  --jupyter-python /path/to/jupyter/bin/python \
  --connection-file /path/to/connection.json \
  --config-file /path/to/console.yaml
```

The transport uses Python with `jupyter_client` and `pyzmq` installed:

```sh
uv venv /path/to/jupyter
uv pip install --python /path/to/jupyter/bin/python jupyter-client pyzmq
```

These dependencies are also available through the `mcp-console[jupyter]` package extra.
`--jupyter-python` selects that transport interpreter; it defaults to `python3` on Unix and `python` on Windows.
This interpreter runs outside the workload sandbox and is independent of the R, Python, or SQL runtime selected in Console configuration.
It does not prepare workload packages or execute submitted source itself.

`--language` is required and accepts `r`, `python`, or `sql`.
Every execution uses that language for the lifetime of the process.
Source reaches Console unchanged; cells need no prefix, metadata, or package requirements fields.
The selected language must be enabled and available under the launch configuration.
A SQL kernel retains the supporting runtimes needed by its configured SQL provider.

Use the ordinary [startup configuration](RUNTIME_CONFIGURATION.md), [package settings](PACKAGE_CONFIGURATION.md), [sandbox policy](SANDBOX_CONFIGURATION.md), and [recording settings](RECORDING_REFERENCE.md).
The `--config-file` argument names a file; the existing `--config KEY=VALUE` option overrides an individual setting.
For example, an existing Python environment can be selected with:

```yaml
python: /path/to/environment/bin/python
sandbox:
  profile: workspace
```

The kernel opens the Jupyter transport before preparing interpreters and packages.
Execution waits for necessary preparation and returns a standard execution error if preparation fails.
Ordinary code can trigger package preparation according to Console's existing policies.
There is no Jupyter package-management extension.
The Python transport starts one native Console controller outside the workload sandbox; submitted code retains the existing worker boundary.
The controller retires its workers and settles recordings if the transport exits and closes its pipes.

## Launch through Kallichore

Kallichore creates the connection file and replaces `{connection_file}` in the launch arguments.
Use its existing session API with `interrupt_mode: "message"`.
For example, start an installed Kallichore with a bearer-token file:

```sh
kcserver --port 8080 --token /path/to/kallichore-token
```

The examples below use `http://127.0.0.1:8080` and assume `KALLICHORE_TOKEN` contains that token.
Replace the executable, configuration, working-directory, and username values in `session.json`:

```json
{
  "session_id": "example",
  "display_name": "Console Python",
  "language": "python",
  "username": "user",
  "input_prompt": ">>> ",
  "continuation_prompt": "... ",
  "session_mode": "console",
  "startup_environment": "none",
  "working_directory": "/path/to/project",
  "env": [],
  "argv": [
    "/path/to/mcp-console",
    "kernel",
    "--language",
    "python",
    "--jupyter-python",
    "/path/to/jupyter/bin/python",
    "--connection-file",
    "{connection_file}",
    "--config-file",
    "/path/to/console.yaml"
  ],
  "interrupt_mode": "message",
  "protocol_version": "5.3"
}
```

Create and start the session:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $KALLICHORE_TOKEN" \
  -H 'Content-Type: application/json' \
  -X PUT --data-binary @session.json \
  http://127.0.0.1:8080/sessions

curl --fail-with-body \
  -H "Authorization: Bearer $KALLICHORE_TOKEN" \
  -X POST http://127.0.0.1:8080/sessions/example/start
```

Wait for the session to become ready on Kallichore's existing channel connection or inspect `GET /sessions/example`.
Save this ordinary request as `execute.json`:

```json
{
  "code": "answer = 6 * 7\nprint(answer)"
}
```

Execute it:

```sh
curl --fail-with-body \
  -H "Authorization: Bearer $KALLICHORE_TOKEN" \
  -H 'Content-Type: application/json' \
  --data-binary @execute.json \
  http://127.0.0.1:8080/sessions/example/execute
```

A second request containing `{"code":"print(answer)"}` sees the same object.
Text appears in standard `stream` messages; images use MIME `display_data` messages.
A language error such as `raise ValueError('example')` produces a structured Jupyter error and an error execution reply.
Console observation timeouts do not finish a Jupyter execution: the request stays active until the underlying operation finishes.

Interrupt a running execution with `POST /sessions/example/interrupt`.
The independent control channel directs interruption to the active preparation or execution operation.
The next cell can use the surviving session.

Disconnecting `/sessions/example/channels` leaves the kernel and its objects alive.
Reconnect through the same endpoint to continue using them; complete replay of missed output is not promised.
`POST /sessions/example/restart` with `{}` shuts down the old kernel process and launches a replacement with the same arguments.
The replacement reads its startup configuration again.
The new session has no previous in-memory objects or live requirements declarations.
To shut down, send a standard Jupyter `shutdown_request` with `{"restart":false}` on the control channel.
After the kernel exits, `DELETE /sessions/example` removes the stopped session from Kallichore.
Both shutdown paths settle Console recordings before exit.

## Input and execution flags

Kallichore's REST and MCP execution collector sets `allow_stdin: false`.
Code that requests managed input through that collector returns an execution error instead of waiting for an answer.
For interactive input, use Kallichore's existing `/sessions/{id}/channels` interface and a standard Jupyter `execute_request` with `allow_stdin: true`.
Reply to `input_request` with `input_reply` on the stdin channel.
Console encodes the reply's `value` as UTF-8 and appends one newline before writing it to its byte-oriented input stream.
An empty reply therefore supplies a blank line.
There is no separate input endpoint.

`silent: true` suppresses execution output and does not advance the execution count.
`store_history: false` also leaves the execution count unchanged.
These flags do not disable Console's audit recording.
Startup input follows the execution request's `allow_stdin` policy too.
Only Console's managed reads can request input; arbitrary direct reads from the worker's stdin do not provide a Jupyter prompt.

Completion returns no matches, inspection returns `found: false`, and completeness returns `unknown`.
Source-only history supports tail and range requests for executions with `store_history: true` and `silent: false`; history search and output history return an unsupported error.
User expressions receive a per-expression unsupported error.
Python expression values from its default display hook and SQL previews use `text/plain` execution results.
Custom Python display hooks and redirected stdout retain their ordinary behavior.
R native REPL printing remains stream output.
Each successful execution ends with an `execute_reply`.
This entry point does not provide a language server, debugger, Positron extensions, per-cell language selection, or requirements arguments.
The existing MCP entry points remain available.

## Run the integration checks

The [Kallichore harness](../tests/kallichore.py) starts an isolated, authenticated Kallichore server and exercises its existing REST and WebSocket interfaces.
It requires a built Console executable, an unmodified `kcserver`, and a Python environment containing Matplotlib:

```sh
uv run tests/kallichore.py \
  --kallichore /path/to/kcserver \
  --binary /path/to/mcp-console \
  --python /path/to/environment/bin/python
```

The harness uses temporary workspaces and Console homes, leaves the sandbox enabled, and checks session creation, ordinary source, persistent objects, text, PNG output, structured errors, stdin, execution flags, interruption, reconnect, restart, process retirement, and recordings.
It also checks a managed startup package and a preparation failure.
Its server launch uses Kallichore's Unix handshake socket, so this harness runs on macOS and Linux.
Pass `--config /path/to/console.yaml` to exercise a specific startup configuration instead of the generated existing-Python configuration.

The [direct Jupyter harness](../tests/jupyter_kernel.py) checks standard wire behavior independently of Kallichore, including the R and SQL source languages, raw output beyond preview limits, and controller retirement after the transport exits.
Consult each script's `--help` for its arguments.

The runnable harness targets unmodified Kallichore and its [session API](https://github.com/posit-dev/kallichore/blob/4ac020249bd40e2780114a0dbf036aa5508aa598/kallichore.json).
The transport uses `jupyter_client` for Jupyter message signing and serialization and `pyzmq` for the channels.
Its advertised protocol version follows the installed `jupyter_client`; the tests report that version.
The optional Jupyter dependencies do not change the native executable's existing MCP entry points.
The [Jupyter messaging specification](https://jupyter-client.readthedocs.io/en/stable/messaging.html) defines the standard channels and messages.
