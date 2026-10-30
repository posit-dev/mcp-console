# Console Chat

A Shiny for Python app that gives each browser client a chatlas conversation and a separate, sandboxed MCP Console process with persistent R, Python and SQL.
Ask questions, calculate summaries, fit models, and make plots.
The app includes 96 rows of synthetic monthly sales data.
Expand tool calls to inspect their arguments as YAML, with multiline R and Python code preserved.
The Markdown export shows submitted code in language-tagged code blocks.
Suggested follow-up questions are clickable buttons that submit the next turn.

The interface follows [querychat](https://posit-dev.github.io/querychat/py/) and its [distinction between data viewing and analytical tool results](https://posit-dev.github.io/querychat/py/tools.html): conversation beside data, with the executed code and results available for inspection.
Console owns code execution, interpreter state, plot capture, and sandbox enforcement.
Chatlas owns the model/tool loop and the MCP connection.
The app uses public MCP tools; it does not need the [Shiny fixture's](../tests/fixtures/control_shiny/README.md) private control socket or its object-inspection machinery.

## Run locally

Use macOS or Linux, Python 3.13, R, and the Console binary built from this checkout with its matching native companion.
The installed Console must support `sessions.max` and the current tool configuration.
Older installed binaries can report an unknown configuration field.

From this directory:

```sh
mkdir -p .runtime/tmp
export TMPDIR="$PWD/.runtime/tmp"
export UV_CACHE_DIR="$PWD/.runtime/uv-cache"
uv venv --python 3.13 .venv
uv pip install --python .venv/bin/python -r requirements.txt
.venv/bin/python -B run.py --binary ../target/release/mcp-console --warmup
.venv/bin/python -B run.py --binary ../target/release/mcp-console --renviron ~/.Renviron
```

Open <http://127.0.0.1:8000>.
The launcher reads only `ANTHROPIC_API_KEY` from the specified file; it does not execute the file or copy the key to disk.
Alternatively, set `ANTHROPIC_API_KEY` in the server environment and omit `--renviron`.
The model is `claude-sonnet-5-5` through chatlas's `ChatAnthropic()` with low reasoning effort.
`ANTHROPIC_BASE_URL` routes requests through a gateway, such as the one [Posit Connect](#deploy-on-posit-connect) supplies.
There is no model substitution.
Use `--r /path/to/R` when R is not on `PATH`.

Each client gets a copy of `data/sample.csv` as `data.csv` in its own workspace.
Objects and files persist for that browser session.
Closing the browser, stopping work, or starting a new session retires its Console process and removes its workspace and recordings.
The data preview is the original dataset; transformations performed by the agent appear in tool results and plots.
Download the conversation before ending a session if you want to retain the analysis.

## Concurrent sessions

Run one server process.
Shiny's nonblocking chat stream lets other clients progress while one client waits for a model or executes code.
The default capacity is eight active conversations; set `--sessions N` before startup to change it.
A full pool returns a visible error.
Each turn has a three-minute deadline, or ten minutes with dynamic packages, and a maximum of sixteen tool calls.

The app pre-creates all session slots.
Each Console policy denies every other slot, including slots that have not been used yet.
It also denies slot directories retained from an earlier server with a larger capacity; changing the capacity does not expose or delete those files.
Its workspace and native temporary files live beneath its own slot.
Slots are released only after chatlas has closed the MCP connection and Console process.
A filesystem lock prevents two server processes from sharing one runtime root.
When scaling beyond one process, give each deployment instance a separate service account or container filesystem as well as its own runtime root.

## Sandbox boundary

Evaluated code can write only its own workspace and Console's private temporary storage.
Direct networking is restricted.
Console starts from an explicit configuration file, clears inherited worker environment variables, and starts R with `--vanilla`.
The server's API key is never passed to Console.
The agent can use all Console tool parameters, including SQL, named sessions, interactive input and, in dynamic sessions, explicit package requests.
Each client can run up to four Console sessions.

By default, sessions use prepared packages, so startup does no package preparation.
Python runs in the app's own environment, which includes the analysis packages in `requirements.txt`.
R uses its installation's libraries and any library in the server's `R_LIBS`; Console's R package preparation is disabled.
Package requests fail in these sessions.

The **Dynamic packages** checkbox applies to the next session.
Console then prepares a managed Python environment and R dependencies separately for that client, and accepts explicit requests for additional packages.
Preparation has its own sandbox and can download packages from configured sources; package installation and builds can run code, so use trusted packages and sources.
Each client has its own preparation cache, which is removed when the client disconnects; cold starts can take minutes and substantial disk space.
On the tested macOS host, a client cache reached roughly 1 GiB.
`--warmup` checks R, Python and SQL with prepared packages on the deployment host.

**Deploy on a dedicated service account and a clean filesystem containing only the intended runtime and app files.** Console's read-only base permits host-file reads.
This app denies sibling sessions, its private configuration and recordings, common credential paths, and every path supplied with `--private-path`.
It does not discover arbitrary secrets elsewhere on a developer's machine.
Do not mount home directories, production databases, cloud credentials, or unrelated application data into the service.
Create and identify private paths before starting the server; filesystem policy is captured at launch.
A missing explicit `--private-path` is a startup error.

The native runner also constrains subprocesses and retires their process trees.
Use host/container resource limits for CPU, memory, process count, and disk capacity.
Per-session directories provide access isolation, not per-client resource quotas.
The model receives prompts and the tool output it requests, including data and plots.
Treat the model provider as a recipient of that data.

The app has no authentication or billing controls.
Place it behind an authenticated HTTPS reverse proxy with WebSocket support and an appropriate idle timeout.
Bind to loopback unless the deployment network supplies that boundary.

## Deploy

Install the app and pinned Python requirements on a Linux host supported by Console's native sandbox.
Install R, `uv`, and a complete Console build from this revision, including its verified companion, under `/opt/mcp-console`.
Keep runtime executables and packages outside session workspaces and unwritable by evaluated code.
Copy the app's source files to `/srv/console-chat-demo`, excluding `.runtime` and `.venv`, create its virtual environment, and give the dedicated service account ownership of `.runtime`.
Check runtime preparation with `run.py --warmup` as that account.

[deploy/console-chat.service](deploy/console-chat.service) is a systemd template for one server process with a bounded session pool and service-level resource limits.
It reads the API key from `/srv/console-chat-secrets/anthropic.env` and explicitly denies that directory to evaluated code and dependency preparation.
Set that file to mode `0600`, restrict access to the service account, and configure the HTTPS/authentication proxy separately.
The service template is intended for a host/VM; containers must permit the native Linux sandbox's required namespaces and syscalls.
If native sandbox setup fails, fix the host configuration; the app does not retry without enforcement.

## Deploy on Posit Connect

On a Connect server with native execution, the app runs as Shiny content.
Console's bubblewrap sandbox needs Connect to permit unprivileged user namespaces; the app does not run without enforcement.
Build the Linux wheel for this revision with a manual run of the Release workflow, which does not publish.
Stage a copy of the app's source files without `.runtime`, `.venv`, `tests`, and `deploy`, and download the wheel into it.
Add the wheel's relative path, such as `wheels/mcp_console-VERSION-py3-none-manylinux_2_34_x86_64.whl`, to the staged copy of `requirements.txt`.
From the staged directory:

```sh
gh run download RUN_ID --name pypi-x86_64-unknown-linux-gnu --dir wheels
uvx --from rsconnect-python rsconnect write-manifest shiny \
  --entrypoint app:app --override-python-version 3.13.18 .
python3 PATH/TO/console-chat-demo/deploy/r_manifest.py manifest.json 4.6.1
uvx --from rsconnect-python rsconnect deploy manifest -n SERVER manifest.json
```

Connect installs the Python requirements into the app's environment, including the `mcp-console` and `uv` executables.
[deploy/r_manifest.py](deploy/r_manifest.py) adds the packages in [deploy/r-packages.txt](deploy/r-packages.txt) and their dependencies, as Ubuntu 22.04 binaries from a fixed Package Manager snapshot.
Connect restores them for the requested R version and exposes them through `R_LIBS`, which prepared sessions use.
Use the Python and R versions that the server provides.

Then configure the content:

- Attach an Anthropic integration that uses environment variables.
  When Connect's LLM gateway serves it, Connect injects `ANTHROPIC_BASE_URL` and an ephemeral `ANTHROPIC_API_KEY`; the provider key never enters the content.
- Set `CONSOLE_CHAT_R` to the R executable for the manifest's R version, such as `/opt/R/4.6.1/bin/R`.
  Connect does not put R on Python content's `PATH`.
- Set `CONSOLE_CHAT_RUNTIME` to a directory under `/tmp`, which Connect keeps local and private to each process, instead of the shared, networked app directory.
  Each process then owns its own runtime root and session pool.
- Set **Max connections per process** to the pool capacity, `CONSOLE_CHAT_CAPACITY` (default 8), so Connect starts another process before a pool is full.
  Bound **Max processes** by the host's CPU, memory, and disk.
- Set `CONSOLE_CHAT_DYNAMIC_PACKAGES=0` to hide the **Dynamic packages** checkbox.
  On a fresh Connect host, a dynamic session's first package preparation took about three and a half minutes.
- Set `CONSOLE_CHAT_PRIVATE_PATHS` to a JSON list of Connect paths that evaluated code must not read: `["/opt/rstudio-connect/mnt/job", "/opt/rstudio-connect/mnt/app-data"]`.
  The job directory holds the content's Connect session token.

Connect authenticates viewers, so leave access restricted to the intended users.
Each viewer's sessions still share the server's model budget and host resources.

## Validate

```sh
uv pip install --python .venv/bin/python -r requirements-dev.txt
CONSOLE_CHAT_BINARY="$PWD/../target/release/mcp-console" \
  .venv/bin/python -B -m pytest --basetemp=.runtime/pytest
```

The process tests use a dummy API key and exercise persistent R/Python state, concurrent execution, denied cross-client reads, credential protection, denied host writes, and clean slot reuse against real Console processes.
Browser checks and live model validation are described in [VALIDATION.md](VALIDATION.md).
