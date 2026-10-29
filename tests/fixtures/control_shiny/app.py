# /// script
# requires-python = ">=3.11"
# dependencies = ["shiny>=1.8.0", "openai-codex>=0.163.0", "pandas[output-formatting]"]
# ///
"""Local integration fixture: one agent, one Console worker, two input paths."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import re
import tempfile
import uuid
from collections import OrderedDict
from collections.abc import AsyncIterator, Coroutine
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.async_client import AsyncCodexClient
from pydantic import RootModel
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

from environments import environments_server, environments_ui
from exports import export_server, export_ui
from inspection import PYTHON_SOURCE, R_SOURCE
from output_views import output_controls, response_controls
from retained_output import retained_output_server
from source_format import SourceFormatter
from table_pane import table_server, table_ui

MODEL = "gpt-6.1-terra"
LOGGER = logging.getLogger(__name__)
# Console previews retain up to 8 MiB of encoded images in addition to text.
RESPONSE_LIMIT = 32 * 1024 * 1024


class ControlError(RuntimeError):
    def __init__(self, error: dict[str, str]) -> None:
        self.code = error["code"]
        super().__init__(error["message"])


async def request(endpoint: Path, **fields: Any) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(
        str(endpoint), limit=RESPONSE_LIMIT
    )
    try:
        writer.write(json.dumps({"id": uuid.uuid4().hex, **fields}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        if "error" in response:
            raise ControlError(response["error"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def make_app(binary: Path, workspace: Path) -> App:
    app_ui = ui.page_fillable(
        ui.tags.head(
            ui.tags.link(rel="stylesheet", href="fixture.css"),
            ui.tags.script(src="fixture.js"),
        ),
        ui.h2("Console integration fixture"),
        ui.output_text("connection_state"),
        ui.layout_columns(
            ui.card(
                ui.card_header(f"Agent chat · {MODEL}", ui.output_text("agent_state")),
                ui.card_body(
                    ui.chat_ui(
                        "agent_chat",
                        placeholder="Ask the agent…",
                        width="100%",
                        height="100%",
                        show_history=False,
                        drawer=False,
                        enable_cancel=False,
                        allow_attachments=False,
                        greeting=ui.TagList(
                            ui.p(
                                "You and the agent share one R, Python, and SQL session."
                            ),
                            ui.p(
                                "Choose a suggestion to edit before sending, or write your own request."
                            ),
                            ui.tags.ul(
                                *(
                                    ui.tags.li(
                                        ui.tags.span(prompt, class_="suggestion")
                                    )
                                    for prompt in (
                                        "List objects in our shared session",
                                        "Create a small example table in R",
                                        "Create a small example table in Python",
                                    )
                                )
                            ),
                        ),
                    ),
                    min_height=0,
                    class_="agent-chat-body",
                ),
            ),
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.span("Console transcript"),
                        export_ui(),
                        class_="d-flex justify-content-between align-items-center",
                    ),
                    ui.div(
                        ui.input_radio_buttons(
                            "source_view",
                            "Code view",
                            {"original": "Original", "formatted": "Formatted"},
                            selected="formatted",
                            inline=True,
                        ),
                        class_="source-view-controls",
                    ),
                ),
                ui.card_body(
                    ui.div(id="transcript"),
                    ui.div(id="output-omissions"),
                    fillable=False,
                    min_height=0,
                    class_="history",
                    id="transcript-history",
                ),
                ui.card_footer(
                    ui.div(
                        ui.input_radio_buttons(
                            "language",
                            "Language",
                            {"r": "R", "python": "Python", "sql": "SQL"},
                            selected="python",
                            inline=True,
                        ),
                        ui.input_radio_buttons(
                            "execution",
                            "Execution",
                            {"serial": "Ordinary", "immediate": "Immediate"},
                            inline=True,
                        ),
                        class_="editor-options",
                    ),
                    ui.tags.p(
                        "Immediate shares session state. R requires event processing; Python requires the GIL. "
                        "SQL supports Ordinary execution only.",
                        id="execution-help",
                        class_="small text-muted",
                    ),
                    ui.div(
                        ui.input_code_editor(
                            "source",
                            "Code · Python syntax",
                            language="python",
                            height="130px",
                            line_numbers=False,
                            word_wrap=True,
                            fill=False,
                        ),
                        ui.div(
                            ui.input_action_button(
                                "submit", "Submit", class_="btn-primary btn-sm"
                            ),
                            ui.input_action_button(
                                "interrupt",
                                "Interrupt",
                                class_="btn-sm",
                                title="Request interruption of the ordinary evaluation",
                            ),
                            ui.input_action_button(
                                "restart",
                                "Restart",
                                class_="btn-sm",
                                title="Restart Console and clear session state",
                            ),
                            class_="editor-actions",
                            role="group",
                            **{"aria-label": "Console controls"},
                        ),
                        class_="editor-row",
                    ),
                ),
            ),
            ui.navset_card_tab(
                ui.nav_panel(
                    "Variables",
                    ui.div(
                        ui.h6("R global environment"),
                        ui.output_code("r_variables", placeholder=False),
                        ui.output_ui("r_tables"),
                        ui.h6("Python __main__"),
                        ui.output_code("python_variables", placeholder=False),
                        ui.output_ui("python_tables"),
                        class_="variables",
                    ),
                ),
                ui.nav_panel(
                    "Plots",
                    ui.p(
                        "Captured images: click to enlarge. Resizing scales the image without rerunning code.",
                        class_="small text-muted",
                    ),
                    ui.div(id="plots"),
                ),
                ui.nav_panel("Table", table_ui("table")),
                ui.nav_panel("Environments", environments_ui("environments")),
                id="inspector_tab",
            ),
            col_widths=(3, 5, 4),
            row_heights="minmax(0, 1fr)",
        ),
        ui.tags.dialog(
            ui.tags.button("Close", id="plot-close", type="button"),
            ui.tags.img(id="plot-enlarged"),
            id="plot-dialog",
        ),
        padding="0.75rem",
        gap="0.5rem",
        title="Console integration fixture",
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        state = reactive.value("Starting…")
        # The SDK owns conversation state; Shiny owns composition, Markdown,
        # rendering, scrolling, and the streamed assistant message.
        agent_chat = ui.Chat("agent_chat", history=False)
        agent_status = reactive.value("Idle")
        variable_views = {
            "r": reactive.value("Waiting for variables…"),
            "python": reactive.value("Waiting for variables…"),
        }
        table_names = {"r": reactive.value([]), "python": reactive.value([])}
        worker_id: int | None = None
        immediate = asyncio.Lock()
        tasks: set[asyncio.Task[None]] = set()
        formatter = SourceFormatter()
        formatting: set[str] = set()
        formatted: OrderedDict[str, None] = OrderedDict()
        ready = asyncio.Event()
        endpoint: Path
        thread: AsyncThread | None = None
        agent_active = False
        restart_active = False
        connected = False

        async def change(value: reactive.Value[Any], item: Any) -> None:
            async with reactive.lock():
                with reactive.isolate():
                    value.set(item)
                await reactive.flush()

        async def private_evaluate(language: str, source: str) -> dict[str, Any]:
            return await request(
                endpoint,
                op="evaluate",
                language=language,
                execution="immediate",
                code=source,
                record=False,
            )

        async def prepare() -> None:
            ready.clear()
            observed_worker = worker_id
            await change(state, "Preparing managed environments…")
            # A replacement's output identity is published before restart has
            # released admission. Observe that transition before submitting setup.
            while observed_worker == worker_id:
                try:
                    status = await request(endpoint, op="status")
                    if status["state"] not in {
                        "starting",
                        "preparing",
                        "restarting",
                        "busy",
                    }:
                        break
                except ControlError as error:
                    if error.code != "busy":
                        raise
                await asyncio.sleep(1)
            if observed_worker != worker_id:
                return
            for language, source in (
                (
                    "r",
                    'reticulate::py_require(c("pandas", "duckdb")); invisible(reticulate::import_builtins())',
                ),
                ("python", "pass"),
            ):
                await request(
                    endpoint,
                    op="evaluate",
                    language=language,
                    code=source,
                    busy="wait",
                    wait_ms=60000,
                    record=False,
                )
                if observed_worker != worker_id:
                    return
            ready.set()
            await change(state, "Ready")
            if connected:
                await environments.refresh()

        def source_html(identifier: str, language: str, source: str) -> str:
            # Native MarkdownStream performs syntax highlighting. The fence is
            # longer than every source backtick run, so code stays literal.
            fence = "`" * max(
                3, 1 + max((len(run) for run in re.findall(r"`+", source)), default=0)
            )
            view = ui.output_markdown_stream(
                "source_" + identifier.encode().hex(),
                content=f"{fence}{language}\n{source}\n{fence}",
                auto_scroll=False,
                width="100%",
            )
            # ui.Chat loads the shared native MarkdownStream dependency.
            return view.render()["html"]

        async def format_source(
            key: str, language: str, source: str, truncated: bool
        ) -> None:
            try:
                result = await formatter.format(language, source, truncated=truncated)
                message = {
                    "format_key": key,
                    "status": result["status"],
                    "message": result["message"],
                }
                if result["status"] == "formatted":
                    message["source_html"] = source_html(
                        key, language, result["source"]
                    )
                await session.send_custom_message("formatted-source", message)
                formatted[key] = None
                while len(formatted) > SourceFormatter.CACHE_ENTRIES:
                    formatted.popitem(last=False)
            finally:
                formatting.discard(key)

        def submitted_source(metadata: dict[str, Any]) -> dict[str, Any]:
            source = metadata["source"]
            language = metadata["language"]
            truncated = metadata.get("source_truncated", False)
            key = hashlib.sha256(
                json.dumps([language, source, truncated]).encode()
            ).hexdigest()
            # Snapshot updates reuse a display result rather than launching a
            # formatter for every pending-output change. Neither view mutates
            # the submitted source used for execution, attribution, or exports.
            if key in formatted:
                formatted.move_to_end(key)
            elif key not in formatting:
                formatting.add(key)
                start(format_source(key, language, source, truncated))
            return {
                **metadata,
                "source_html": source_html(str(metadata["id"]), language, source),
                "output_controls": output_controls(str(metadata["id"])),
                "format_key": key,
                "format_status": "pending",
            }

        async def publish_snapshot(snapshot: dict[str, Any]) -> None:
            nonlocal worker_id
            observed_worker = snapshot["worker_id"]
            # A private result can arrive after replacement's subscription event.
            if worker_id is not None and observed_worker < worker_id:
                return
            changed = worker_id is not None and observed_worker != worker_id
            worker_id = observed_worker
            for item in snapshot["evaluations"]:
                retained.observe(item)
            if changed:
                ready.clear()

                async def replace() -> None:
                    for language in variable_views:
                        await change(
                            variable_views[language],
                            "Session replaced. Waiting for variables…",
                        )
                        await change(table_names[language], [])
                    await table.reset()
                    await prepare()

                start(replace())
            await session.send_custom_message(
                "output-snapshot",
                {
                    **snapshot,
                    "reset": changed,
                    "evaluations": [
                        submitted_source(item) for item in snapshot["evaluations"]
                    ],
                },
            )

        async def guarded(coroutine: Coroutine[Any, Any, None]) -> None:
            try:
                await coroutine
            except Exception as error:
                LOGGER.exception("Fixture action failed")
                await change(state, f"Error: {error}")

        def start(coroutine: Coroutine[Any, Any, None]) -> None:
            task = asyncio.create_task(guarded(coroutine))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

        table = table_server("table", private_evaluate, immediate, ready, start)

        async def environment_request(**fields: Any) -> dict[str, Any]:
            if not connected:
                raise RuntimeError("Console is not connected yet.")
            return await request(endpoint, **fields)

        async def prepare_requirements(requirements: dict[str, Any]) -> dict[str, Any]:
            if not connected or not ready.is_set():
                raise RuntimeError("Wait for Console's managed session to be ready.")
            observed_worker = worker_id
            ready.clear()
            await control_notice(
                "requirements",
                f"Human requested requirement preparation: {json.dumps(requirements)}",
            )
            try:
                async with immediate:
                    if not connected or observed_worker != worker_id:
                        raise RuntimeError(
                            "Session changed before requirement preparation; review the draft and retry."
                        )
                    result = await request(
                        endpoint, op="prepare", requirements=requirements
                    )
                await publish_snapshot(await request(endpoint, op="output"))
                await control_notice(
                    "requirements", f"Requirement preparation: {result['status']}."
                )
                return result
            except (RuntimeError, OSError) as error:
                await control_notice(
                    "requirements", f"Requirement preparation failed: {error}"
                )
                raise
            finally:
                if connected and observed_worker == worker_id:
                    ready.set()

        environments = environments_server(
            "environments",
            request=environment_request,
            prepare=prepare_requirements,
            start=start,
        )
        export_server(input, session, lambda: environment_request(op="recording"))
        retained = retained_output_server(
            input, session, lambda: environment_request(op="recording")
        )

        async def inspect_variables() -> None:
            while True:
                await ready.wait()
                for language, source in (("r", R_SOURCE), ("python", PYTHON_SOURCE)):
                    observed_worker = worker_id
                    try:
                        async with immediate:
                            if not ready.is_set():
                                break
                            result = await request(
                                endpoint,
                                op="evaluate",
                                language=language,
                                execution="immediate",
                                code=source,
                                record=False,
                            )
                    except ControlError as error:
                        # Retirement may fail an already admitted inspection.
                        # Keep the periodic observer alive across that boundary;
                        # a request failure is visible in its pane, not a fatal
                        # error for the subscription or future inspections.
                        if error.code != "busy" and observed_worker == worker_id:
                            await change(
                                variable_views[language],
                                f"Inspection unavailable: {error}",
                            )
                            await change(table_names[language], [])
                        continue
                    if observed_worker != worker_id:
                        continue
                    text = "".join(
                        block["text"]
                        for block in result["content"]
                        if block["type"] == "text"
                    )
                    if error := result.get("execution_error"):
                        await change(
                            variable_views[language],
                            f"{error['ename']}: {error['evalue']}",
                        )
                    else:
                        view = json.loads(text)
                        await change(variable_views[language], view["text"])
                        await change(table_names[language], view["tables"])
                await asyncio.sleep(1)

        async def connect() -> None:
            nonlocal endpoint, thread, connected
            with tempfile.TemporaryDirectory(
                prefix="console-shiny-", dir="/tmp"
            ) as directory:
                root = Path(directory)
                endpoint = root / "control.sock"
                # Read effective server names, including project layers, without
                # exposing values or changing configuration/authentication files.
                async with AsyncCodexClient() as discovery:
                    await discovery.initialize()
                    settings = await discovery.request(
                        "config/read",
                        {"cwd": str(workspace), "includeLayers": False},
                        response_model=RootModel[dict[str, Any]],
                    )
                    servers = {
                        name: {"enabled": False}
                        for name in settings.root["config"].get("mcp_servers", {})
                    }
                config = {
                    "model_reasoning_effort": "low",
                    "features": {"code_mode": False, "multi_agent": False},
                    "mcp_servers": {
                        **servers,
                        "console_fixture": {
                            "enabled": True,
                            "command": str(binary),
                            "args": [
                                "serve",
                                "--control-socket",
                                str(endpoint),
                                "--no-sandbox",
                                "-c",
                                "languages=[r,python,sql]",
                            ],
                            "cwd": str(workspace),
                            "env": {
                                "MCP_CONSOLE_HOME": str(root / "home"),
                            },
                            "required": True,
                            "tool_timeout_sec": 300,
                            "tools": {"send": {"approval_mode": "approve"}},
                        },
                    },
                }
                async with AsyncCodex() as client:
                    thread = await client.thread_start(
                        model=MODEL,
                        cwd=str(workspace),
                        ephemeral=True,
                        sandbox=Sandbox.workspace_write,
                        approval_mode=ApprovalMode.deny_all,
                        config=config,
                        developer_instructions=(
                            "Use only the console_fixture MCP send tool to execute "
                            "R, Python, or SQL code requested by the user. The user also "
                            "runs code directly in this same persistent worker. "
                            "Do not use a shell, other tools, or another model."
                        ),
                    )
                    # This waits only for process startup; output is delivered by
                    # the subscription below, with no repeated preview requests.
                    async with asyncio.timeout(60):
                        while not endpoint.exists():
                            await asyncio.sleep(0.05)
                    reader, writer = await asyncio.open_unix_connection(
                        str(endpoint), limit=RESPONSE_LIMIT
                    )
                    try:
                        writer.write(
                            b'{"id":"output","op":"subscribe","format":"events"}\n'
                        )
                        await writer.drain()
                        acknowledgement = json.loads(await reader.readline())
                        if "error" in acknowledgement:
                            raise RuntimeError(acknowledgement["error"]["message"])
                        await prepare()
                        # Establish the current worker before processing queued
                        # subscription snapshots from startup.
                        await publish_snapshot(await request(endpoint, op="output"))
                        connected = True
                        await environments.refresh()
                        start(inspect_variables())
                        while line := await reader.readline():
                            event = json.loads(line)
                            if event.get("event") == "output":
                                await publish_snapshot(event["result"])
                        raise RuntimeError("Console subscription closed")
                    finally:
                        connected = False
                        writer.close()
                        await writer.wait_closed()

        async def evaluate(language: str, source: str, execution: str) -> None:
            await ready.wait()
            if language == "sql" and execution == "immediate":
                raise ValueError("SQL supports Ordinary execution only")
            request_id = uuid.uuid4().hex
            submission = {
                "id": "request:" + request_id,
                "request_id": request_id,
                "origin": "integrator",
                "source": source,
                "language": language,
                "execution": execution,
                "state": "running",
            }
            await session.send_custom_message(
                "submission", submitted_source(submission)
            )
            fields = {
                "id": request_id,
                "op": "evaluate",
                "language": language,
                "code": source,
                "include_output": True,
            }
            if execution == "serial":
                fields.update(busy="wait", wait_ms=60000)
            else:
                fields["execution"] = execution
            try:
                if execution == "immediate":
                    async with immediate:
                        result = await request(endpoint, **fields)
                else:
                    result = await request(endpoint, **fields)
            except Exception as error:
                await session.send_custom_message(
                    "submission", submitted_source({**submission, "state": "failed"})
                )
                await session.send_custom_message(
                    "notice",
                    {
                        "kind": "failed",
                        "text": f"{language.upper()} submission failed: {error}",
                    },
                )
                raise
            await publish_snapshot(result["output"])
            start(environments.refresh())

        async def control_notice(kind: str, text: str) -> None:
            await session.send_custom_message("notice", {"kind": kind, "text": text})

        async def interrupt_session() -> None:
            if not connected:
                await control_notice("interrupt", "Console is not connected.")
                return
            status = await request(endpoint, op="status")
            evaluation = status["evaluation"]
            if evaluation is None:
                await control_notice(
                    "interrupt",
                    "No ordinary evaluation is running."
                    if status["idle"]
                    else f"Console is {status['state']}; no interrupt target is available yet.",
                )
                return
            await control_notice(
                "interrupt",
                f"Human requested interruption of evaluation {evaluation['id']}; execution may still be running.",
            )
            try:
                await request(endpoint, op="interrupt", evaluation_id=evaluation["id"])
            except ControlError as error:
                await control_notice("interrupt", f"Interrupt request failed: {error}")
            else:
                await control_notice(
                    "interrupt",
                    "Interrupt request accepted; execution may still be running.",
                )

        async def restart_session() -> None:
            nonlocal restart_active
            if not connected:
                await control_notice("restart", "Console is not connected.")
                return
            if restart_active:
                return
            restart_active = True
            async with reactive.lock():
                ui.update_action_button("restart", disabled=True, session=session)
                await reactive.flush()
            try:
                await control_notice("restart", "Human requested a session restart.")
                await change(state, "Restarting…")
                await request(endpoint, op="restart")
                # Observe the new identity even if its subscription update is
                # still queued. Replacement resets viewers and prepares runtimes.
                await publish_snapshot(await request(endpoint, op="output"))
                # Replacement is confirmed. Preparation reports its progress and
                # failures separately; it must not prevent another restart.
                await control_notice(
                    "restart",
                    "Session restarted; previous in-memory state was cleared.",
                )
            except ControlError as error:
                await control_notice("restart", f"Restart request failed: {error}")
                await change(state, f"Restart failed: {error}")
            finally:
                restart_active = False
                async with reactive.lock():
                    ui.update_action_button("restart", disabled=False, session=session)
                    await reactive.flush()

        async def open_table(language: str, name: str) -> None:
            async with reactive.lock():
                ui.update_navset("inspector_tab", selected="Table", session=session)
            await table.open(language, name)

        async def agent_reply(prompt: str) -> AsyncIterator[str]:
            nonlocal agent_active
            if agent_active:
                raise RuntimeError("Wait for the active agent response")
            agent_active = True
            try:
                await ready.wait()
                assert thread is not None
                await change(agent_status, "Running")
                streamed_items: set[str] = set()
                previous_item: str | None = None
                turn = await thread.turn(prompt, model=MODEL, effort="low")
                async for event in turn.stream():
                    if event.method == "item/agentMessage/delta":
                        item_id = event.payload.item_id
                        if previous_item is not None and previous_item != item_id:
                            yield "\n\n"
                        previous_item = item_id
                        streamed_items.add(item_id)
                        yield event.payload.delta
                    elif event.method in ("item/started", "item/completed"):
                        item = event.payload.item.root
                        if (
                            item.type == "agentMessage"
                            and event.method == "item/completed"
                        ):
                            # The completed item repeats the streamed text. A
                            # nonstreamed item is emitted once at completion.
                            if item.id not in streamed_items and item.text:
                                if previous_item is not None:
                                    yield "\n\n"
                                previous_item = item.id
                                yield item.text
                        elif (
                            item.type == "mcpToolCall"
                            and item.server == "console_fixture"
                        ):
                            submissions = [
                                {
                                    "id": f"agent:{item.id}:{language}",
                                    "origin": "mcp",
                                    "language": language,
                                    "source": item.arguments[language],
                                    "execution": "serial",
                                    "state": "running",
                                }
                                for language in ("r", "python", "sql")
                                if item.arguments.get(language)
                            ]
                            if event.method == "item/started":
                                for submission in submissions:
                                    await session.send_custom_message(
                                        "submission", submitted_source(submission)
                                    )
                                if item.arguments.get("control") == "interrupt":
                                    await session.send_custom_message(
                                        "notice",
                                        {
                                            "kind": "interrupt",
                                            "text": "Agent requested interruption; execution may still be running.",
                                        },
                                    )
                            else:
                                await session.send_custom_message(
                                    "agent-response",
                                    {
                                        "id": item.id,
                                        "output_controls": response_controls(item.id),
                                        "submission_ids": [
                                            submission["id"]
                                            for submission in submissions
                                        ],
                                        "content": item.result.content
                                        if item.result
                                        else [],
                                        "structuredContent": (
                                            item.result.structured_content
                                            if item.result
                                            else None
                                        ),
                                        "error": item.error.message
                                        if item.error
                                        else None,
                                        "label": (
                                            "Code submission"
                                            if submissions
                                            else item.arguments["control"].title()
                                            if item.arguments.get("control")
                                            else "Requirements"
                                            if item.arguments.get("requirements")
                                            else "Poll"
                                        ),
                                        "status": item.status.value,
                                    },
                                )
                                outcome = (
                                    item.error.message
                                    if item.error
                                    else "".join(
                                        block.get("text", "")
                                        for block in item.result.content
                                        if block.get("type") == "text"
                                    )
                                    if item.result
                                    else "No result returned."
                                )
                                await session.send_custom_message(
                                    "agent-outcome",
                                    {
                                        "ids": [item["id"] for item in submissions],
                                        "text": outcome,
                                    },
                                )
                                if item.arguments.get("control") == "interrupt":
                                    await session.send_custom_message(
                                        "notice",
                                        {
                                            "kind": "interrupt",
                                            "text": "Agent interrupt request returned: "
                                            + outcome,
                                        },
                                    )
                    elif event.method == "turn/completed":
                        if event.payload.turn.error:
                            raise RuntimeError(event.payload.turn.error.message)
                        await change(
                            agent_status, event.payload.turn.status.value.title()
                        )
            except Exception as error:
                LOGGER.exception("Agent response failed")
                await change(state, f"Error: {error}")
                raise
            finally:
                agent_active = False
                start(environments.refresh())

        @reactive.effect
        def editor_language() -> None:
            language = input.language()
            label = {"r": "R", "python": "Python", "sql": "SQL"}[language]
            ui.update_code_editor(
                "source",
                language=language,
                label=f"Code · {label} syntax",
                session=session,
            )

        @reactive.effect
        def submission_availability() -> None:
            ui.update_action_button(
                "submit",
                disabled=input.language() == "sql" and input.execution() == "immediate",
                session=session,
            )

        @reactive.effect
        @reactive.event(input.submit)
        def submit_code() -> None:
            start(evaluate(input.language(), input.source(), input.execution()))

        @reactive.effect
        @reactive.event(input.interrupt)
        def interrupt_console() -> None:
            start(interrupt_session())

        @reactive.effect
        @reactive.event(input.restart)
        def restart_console() -> None:
            start(restart_session())

        @reactive.effect
        @reactive.event(input.open_table)
        def select_table() -> None:
            value = input.open_table()
            start(open_table(value["language"], value["name"]))

        @reactive.effect
        def inspect_environments() -> None:
            if input.inspector_tab() == "Environments" and connected:
                start(environments.refresh())

        @agent_chat.on_user_submit
        async def submit_prompt(prompt: str) -> None:
            # Shiny runs this iterator in its own ExtendedTask, so Console's
            # direct controls and subscriptions remain responsive during a turn.
            await agent_chat.append_message_stream(agent_reply(prompt))

        @render.text
        def connection_state() -> str:
            return state.get()

        @render.text
        def agent_state() -> str:
            return agent_status.get()

        @render.code
        def r_variables() -> str:
            return variable_views["r"].get()

        @render.code
        def python_variables() -> str:
            return variable_views["python"].get()

        def table_buttons(language: str) -> ui.TagList:
            return ui.TagList(
                *(
                    ui.tags.button(
                        f"Open {name}",
                        type="button",
                        class_="btn btn-outline-secondary table-open",
                        **{"data-table-language": language, "data-table-name": name},
                    )
                    for name in table_names[language].get()
                )
            )

        @render.ui
        def r_tables() -> ui.TagList:
            return table_buttons("r")

        @render.ui
        def python_tables() -> ui.TagList:
            return table_buttons("python")

        async def close() -> None:
            with reactive.isolate():
                agent_chat.latest_message_stream.cancel()
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            table.dispose()

        session.on_ended(close)
        start(connect())

    return App(app_ui, server, static_assets=Path(__file__).with_name("www"))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--workspace", type=Path)
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    with contextlib.ExitStack() as stack:
        workspace = args.workspace or Path(
            stack.enter_context(
                tempfile.TemporaryDirectory(prefix="console-shiny-work-")
            )
        )
        assert args.binary.is_file(), args.binary
        assert workspace.is_dir(), workspace
        make_app(args.binary.resolve(), workspace.resolve()).run(
            host="127.0.0.1", port=args.port, launch_browser=False
        )
