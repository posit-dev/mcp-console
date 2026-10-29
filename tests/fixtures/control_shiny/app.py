# /// script
# requires-python = ">=3.11"
# dependencies = ["shiny>=1.8.0", "openai-codex>=0.163.0", "pandas[output-formatting]"]
# ///
"""Local integration fixture: one agent, named Console sessions, two input paths."""

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
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from environments import EnvironmentActions, environments_server, environments_ui
from exports import export_server, export_ui
from inspection import inspection_source
from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.async_client import AsyncCodexClient
from output_views import output_controls, response_controls
from pydantic import RootModel
from retained_output import retained_output_server
from session_controls import new_session_modal, session_controls_ui
from shiny import App, Inputs, Outputs, Session, reactive, render, req, ui
from shiny.session import session_context
from source_format import SourceFormatter
from table_pane import TableActions, table_server, table_ui

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


@dataclass
class ConsoleSession:
    """UI ownership follows a named Console worker, never the selected dropdown."""

    name: str
    incarnation: str = field(default_factory=lambda: uuid.uuid4().hex)
    worker_id: int | None = None
    retired_worker: int = 0
    state: reactive.Value[str] = field(
        default_factory=lambda: reactive.value("Starting…")
    )
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    immediate: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[Any]] = field(default_factory=set)
    variable_views: dict[str, reactive.Value[str]] = field(
        default_factory=lambda: {
            language: reactive.value("Waiting for variables…")
            for language in ("r", "python")
        }
    )
    table_names: dict[str, reactive.Value[list[str]]] = field(
        default_factory=lambda: {
            language: reactive.value([]) for language in ("r", "python")
        }
    )
    table: TableActions = field(init=False)
    environments: EnvironmentActions = field(init=False)
    preparing: asyncio.Task[Any] | None = None
    subscribed: bool = False
    closing: bool = False
    restart_active: bool = False

    def metadata(self) -> dict[str, str]:
        return {"session": self.name, "incarnation": self.incarnation}

    def module_id(self, kind: str) -> str:
        return kind if self.name == "default" else f"{kind}_{self.incarnation}"


def make_app(binary: Path, workspace: Path) -> App:
    app_ui = ui.page_fillable(
        ui.tags.head(
            ui.tags.link(rel="stylesheet", href="fixture.css"),
            ui.tags.script(src="fixture.js"),
        ),
        ui.h2("Console integration fixture"),
        session_controls_ui(),
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
                                "You and the agent share the selected R, Python, and SQL session."
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
                ui.nav_panel("Table", ui.div(id="session-tables")),
                ui.nav_panel("Environments", ui.div(id="session-environments")),
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
        # The SDK owns the conversation; each Console session owns its runtime
        # and inspector state. Shiny owns composition and chat rendering.
        agent_chat = ui.Chat("agent_chat", history=False)
        agent_status = reactive.value("Idle")
        contexts: dict[str, ConsoleSession] = {}
        selected = reactive.value("default")
        tasks: set[asyncio.Task[Any]] = set()
        formatter = SourceFormatter()
        formatting: set[str] = set()
        formatted: OrderedDict[str, None] = OrderedDict()
        endpoint: Path
        thread: AsyncThread | None = None
        agent_active = False
        connected = False
        registry_lock = asyncio.Lock()
        selection_lock = asyncio.Lock()

        def current() -> ConsoleSession:
            return contexts[selected.get()]

        def requested() -> ConsoleSession:
            # A dropdown change can arrive in the same flush as a button click,
            # before the asynchronous view activation has finished.
            ctx = contexts.get(input.console_session())
            req(ctx is not None and live(ctx))
            assert ctx is not None
            return ctx

        def live(ctx: ConsoleSession) -> bool:
            return contexts.get(ctx.name) is ctx and not ctx.closing

        def resolve(value: dict[str, Any]) -> ConsoleSession:
            ctx = contexts.get(value["session"])
            if ctx is None or not live(ctx) or ctx.incarnation != value["incarnation"]:
                raise RuntimeError(
                    "This Console session has closed. Select an active session."
                )
            return ctx

        async def scoped_request(ctx: ConsoleSession, **fields: Any) -> dict[str, Any]:
            if not connected or not live(ctx):
                raise RuntimeError("Console session is not connected.")
            return await request(endpoint, session=ctx.name, **fields)

        async def message(
            ctx: ConsoleSession, kind: str, value: dict[str, Any]
        ) -> None:
            if live(ctx):
                await session.send_custom_message(kind, {**value, **ctx.metadata()})

        async def change(value: reactive.Value[Any], item: Any) -> None:
            async with reactive.lock():
                with reactive.isolate():
                    value.set(item)
                await reactive.flush()

        async def private_evaluate(
            ctx: ConsoleSession, language: str, source: str
        ) -> dict[str, Any]:
            return await scoped_request(
                ctx,
                op="evaluate",
                language=language,
                execution="immediate",
                code=source,
                record=False,
            )

        async def prepare(ctx: ConsoleSession) -> None:
            ctx.ready.clear()
            observed_worker = ctx.worker_id or 0
            await change(ctx.state, "Preparing managed environments…")
            # A replacement's output identity is published before restart has
            # released admission. Observe that transition before submitting setup.
            while live(ctx):
                try:
                    status = await scoped_request(ctx, op="status")
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
            if not live(ctx) or (
                observed_worker > 0 and observed_worker != ctx.worker_id
            ):
                return
            for language, source in (
                (
                    "r",
                    'reticulate::py_require(c("pandas", "duckdb")); invisible(reticulate::import_builtins())',
                ),
                ("python", "pass"),
            ):
                result = await scoped_request(
                    ctx,
                    op="evaluate",
                    language=language,
                    code=source,
                    busy="wait",
                    wait_ms=60000,
                    record=False,
                )
                if result.get("isError") or result.get("execution_error"):
                    diagnostic = "".join(
                        block["text"]
                        for block in result["content"]
                        if block["type"] == "text"
                    )
                    raise RuntimeError(
                        f"Managed environment setup failed: {diagnostic}"
                    )
                if not live(ctx) or (
                    observed_worker > 0 and observed_worker != ctx.worker_id
                ):
                    return
            ctx.ready.set()
            await change(ctx.state, "Ready")
            if connected:
                await ctx.environments.refresh()

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

        def submitted_source(
            ctx: ConsoleSession, metadata: dict[str, Any]
        ) -> dict[str, Any]:
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
                "source_html": source_html(
                    ctx.incarnation + str(metadata["id"]), language, source
                ),
                "output_controls": output_controls(
                    ctx.incarnation + str(metadata["id"])
                ),
                "format_key": key,
                "format_status": "pending",
            }

        async def publish_snapshot(
            ctx: ConsoleSession, snapshot: dict[str, Any]
        ) -> None:
            if not live(ctx):
                return
            observed_worker = snapshot["worker_id"]
            # Reopening a stopped name can subscribe before setup launches its
            # next worker. Its old retained history belongs to the closed view.
            if ctx.retired_worker and observed_worker <= ctx.retired_worker:
                return
            if ctx.worker_id is not None and observed_worker < ctx.worker_id:
                return
            changed = bool(ctx.worker_id) and observed_worker != ctx.worker_id
            ctx.worker_id = observed_worker
            for item in snapshot["evaluations"]:
                retained.observe({**item, **ctx.metadata()})
            if changed:
                ctx.ready.clear()
                if ctx.preparing is not None:
                    ctx.preparing.cancel()
                for language in ctx.variable_views:
                    await change(
                        ctx.variable_views[language],
                        "Session replaced. Waiting for variables…",
                    )
                    await change(ctx.table_names[language], [])
                await ctx.table.reset()
                ctx.preparing = start(prepare(ctx), ctx)
            await message(
                ctx,
                "output-snapshot",
                {
                    **snapshot,
                    "reset": changed,
                    "evaluations": [
                        submitted_source(ctx, item) for item in snapshot["evaluations"]
                    ],
                },
            )

        async def guarded(
            coroutine: Coroutine[Any, Any, Any], ctx: ConsoleSession | None
        ) -> None:
            if ctx is not None and not live(ctx):
                coroutine.close()
                return
            try:
                await coroutine
            except Exception as error:
                LOGGER.exception("Fixture action failed")
                if ctx is None or live(ctx):
                    await change(
                        ctx.state if ctx is not None else state, f"Error: {error}"
                    )

        def start(
            coroutine: Coroutine[Any, Any, Any], ctx: ConsoleSession | None = None
        ) -> asyncio.Task[Any]:
            task = asyncio.create_task(guarded(coroutine, ctx))
            owner = ctx.tasks if ctx is not None else tasks
            owner.add(task)
            task.add_done_callback(owner.discard)
            return task

        async def prepare_requirements(
            ctx: ConsoleSession, requirements: dict[str, Any]
        ) -> dict[str, Any]:
            if not connected or not ctx.ready.is_set():
                raise RuntimeError("Wait for Console's managed session to be ready.")
            observed_worker = ctx.worker_id
            ctx.ready.clear()
            await control_notice(
                ctx,
                "requirements",
                f"Human requested requirement preparation: {json.dumps(requirements)}",
            )
            try:
                async with ctx.immediate:
                    if not live(ctx) or observed_worker != ctx.worker_id:
                        raise RuntimeError(
                            "Session changed before requirement preparation; review the draft and retry."
                        )
                    result = await scoped_request(
                        ctx, op="prepare", requirements=requirements
                    )
                await publish_snapshot(ctx, await scoped_request(ctx, op="output"))
                await control_notice(
                    ctx, "requirements", f"Requirement preparation: {result['status']}."
                )
                return result
            except (RuntimeError, OSError) as error:
                await control_notice(
                    ctx, "requirements", f"Requirement preparation failed: {error}"
                )
                raise
            finally:
                if live(ctx) and observed_worker == ctx.worker_id:
                    ctx.ready.set()

        def make_context(name: str) -> ConsoleSession:
            ctx = ConsoleSession(name)
            contexts[name] = ctx
            # Modules are mounted once. Switching preserves input drafts and
            # their callbacks always target the worker which owns the module.
            with session_context(session):
                ctx.table = table_server(
                    ctx.module_id("table"),
                    lambda language, source: private_evaluate(ctx, language, source),
                    ctx.immediate,
                    ctx.ready,
                    lambda action: start(action, ctx),
                )
                ctx.environments = environments_server(
                    ctx.module_id("environments"),
                    request=lambda **fields: scoped_request(ctx, **fields),
                    prepare=lambda requirements: prepare_requirements(
                        ctx, requirements
                    ),
                    start=lambda action: start(action, ctx),
                )
                for kind, factory, host in (
                    ("table", table_ui, "session-tables"),
                    ("environments", environments_ui, "session-environments"),
                ):
                    ui.insert_ui(
                        ui.div(
                            factory(ctx.module_id(kind)),
                            class_="session-inspector",
                            hidden=True,
                            **{
                                "data-session": name,
                                "data-incarnation": ctx.incarnation,
                                "data-inspector": kind,
                            },
                        ),
                        selector="#" + host,
                        where="beforeEnd",
                        session=session,
                    )
            return ctx

        async def recording(value: dict[str, Any]) -> dict[str, Any]:
            return await scoped_request(resolve(value), op="recording")

        export_server(input, session, recording)
        retained = retained_output_server(input, session, recording)
        make_context("default")

        async def update_sessions() -> None:
            async with reactive.lock():
                with reactive.isolate():
                    name = selected.get()
                ui.update_select(
                    "console_session",
                    choices=sorted(
                        name
                        for name, ctx in contexts.items()
                        if name == "default" or ctx.subscribed
                    ),
                    selected=name,
                    session=session,
                )
                ui.update_action_button(
                    "close_console_session", disabled=name == "default", session=session
                )
                ui.update_action_button(
                    "new_console_session", disabled=not connected, session=session
                )
                await reactive.flush()

        async def activate(ctx: ConsoleSession) -> None:
            async with selection_lock:
                if not live(ctx):
                    return
                with reactive.isolate():
                    old = current()
                if old is not ctx:
                    if live(old):
                        start(old.table.close(), old)
                    await change(selected, ctx.name)
                await change(state, "")
                await update_sessions()
                await message(ctx, "session-selected", {})
                if ctx.ready.is_set():
                    start(ctx.environments.refresh(), ctx)

        async def retire(ctx: ConsoleSession) -> None:
            if contexts.get(ctx.name) is not ctx:
                return
            with reactive.isolate():
                if current() is ctx:
                    await activate(contexts["default"])
            ctx.closing = True
            active = list(ctx.tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            ctx.table.dispose()
            del contexts[ctx.name]
            async with reactive.lock():
                ui.remove_ui(
                    f'.session-inspector[data-incarnation="{ctx.incarnation}"]',
                    multiple=True,
                    session=session,
                )
                await reactive.flush()
            await session.send_custom_message("session-closed", ctx.metadata())
            await update_sessions()

        async def subscribe(ctx: ConsoleSession) -> None:
            reader, writer = await asyncio.open_unix_connection(
                str(endpoint), limit=RESPONSE_LIMIT
            )
            try:
                writer.write(
                    json.dumps(
                        {
                            "id": "output",
                            "op": "subscribe",
                            "format": "events",
                            "session": ctx.name,
                        }
                    ).encode()
                    + b"\n"
                )
                await writer.drain()
                acknowledgement = json.loads(await reader.readline())
                if "error" in acknowledgement:
                    raise ControlError(acknowledgement["error"])
                await publish_snapshot(ctx, await scoped_request(ctx, op="output"))
                if ctx.preparing is None or ctx.preparing.done():
                    ctx.preparing = start(prepare(ctx), ctx)
                start(inspect_variables(ctx), ctx)
                while line := await reader.readline():
                    event = json.loads(line)
                    if event.get("event") == "output":
                        await publish_snapshot(ctx, event["result"])
                raise RuntimeError("Console subscription closed")
            finally:
                writer.close()
                await writer.wait_closed()

        def attach(ctx: ConsoleSession) -> None:
            if not ctx.subscribed:
                ctx.subscribed = True
                start(subscribe(ctx), ctx)

        async def discover_sessions() -> None:
            async with registry_lock:
                listing = await request(endpoint, op="sessions")
                for item in listing["sessions"]:
                    name = item["name"]
                    ctx = contexts.get(name)
                    if item["state"] == "stopped" and name != "default":
                        # Creation is lazy: setup will launch the new worker.
                        if (
                            ctx is not None
                            and item["worker_id"] > ctx.retired_worker
                            and not ctx.restart_active
                        ):
                            await retire(ctx)
                        continue
                    if ctx is None:
                        async with reactive.lock():
                            ctx = make_context(name)
                            await reactive.flush()
                    if not ctx.subscribed:
                        attach(ctx)
                        await update_sessions()

        async def watch_sessions() -> None:
            while True:
                await discover_sessions()
                await asyncio.sleep(1)

        async def inspect_variables(ctx: ConsoleSession) -> None:
            while True:
                await ctx.ready.wait()
                with reactive.isolate():
                    active = current() is ctx
                if not active:
                    await asyncio.sleep(1)
                    continue
                for language in ("r", "python"):
                    observed_worker = ctx.worker_id
                    try:
                        # Like table pages, structured inspection results use a
                        # private file. User str() diagnostics can fill Console's
                        # display budget without corrupting this payload.
                        with tempfile.TemporaryDirectory(
                            prefix="console-inspection-", dir="/tmp"
                        ) as directory:
                            output = Path(directory) / "variables.json"
                            async with ctx.immediate:
                                if not ctx.ready.is_set():
                                    break
                                result = await scoped_request(
                                    ctx,
                                    op="evaluate",
                                    language=language,
                                    execution="immediate",
                                    code=inspection_source(language, output),
                                    record=False,
                                )
                            if observed_worker != ctx.worker_id:
                                continue
                            if error := result.get("execution_error"):
                                await change(
                                    ctx.variable_views[language],
                                    f"{error['ename']}: {error['evalue']}",
                                )
                                await change(ctx.table_names[language], [])
                            elif result.get("isError"):
                                await change(
                                    ctx.variable_views[language],
                                    "Inspection unavailable: request failed",
                                )
                                await change(ctx.table_names[language], [])
                            else:
                                view = json.loads(output.read_text(encoding="utf-8"))
                                await change(ctx.variable_views[language], view["text"])
                                await change(ctx.table_names[language], view["tables"])
                    except ControlError as error:
                        # Retirement may fail an already admitted inspection.
                        # Keep the periodic observer alive across that boundary;
                        # a request failure is visible in its pane, not a fatal
                        # error for the subscription or future inspections.
                        if error.code != "busy" and observed_worker == ctx.worker_id:
                            await change(
                                ctx.variable_views[language],
                                f"Inspection unavailable: {error}",
                            )
                            await change(ctx.table_names[language], [])
                        continue
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
                            "runs code directly in these same named sessions. Each prompt identifies "
                            "the selected Console session; include its name in the send session argument "
                            "for code, polls, and controls unless asked to use another session. "
                            "Do not use a shell, other tools, or another model."
                        ),
                    )
                    # This waits only for process startup; output is delivered by
                    # the subscription below, with no repeated preview requests.
                    async with asyncio.timeout(60):
                        while not endpoint.exists():
                            await asyncio.sleep(0.05)
                    connected = True
                    try:
                        await activate(contexts["default"])
                        attach(contexts["default"])
                        await watch_sessions()
                    finally:
                        connected = False

        async def evaluate(
            ctx: ConsoleSession, language: str, source: str, execution: str
        ) -> None:
            await ctx.ready.wait()
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
            await message(ctx, "submission", submitted_source(ctx, submission))
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
                    async with ctx.immediate:
                        result = await scoped_request(ctx, **fields)
                else:
                    result = await scoped_request(ctx, **fields)
            except Exception as error:
                await message(
                    ctx,
                    "submission",
                    submitted_source(ctx, {**submission, "state": "failed"}),
                )
                await message(
                    ctx,
                    "notice",
                    {
                        "kind": "failed",
                        "text": f"{language.upper()} submission failed: {error}",
                    },
                )
                raise
            await publish_snapshot(ctx, result["output"])
            start(ctx.environments.refresh(), ctx)

        async def control_notice(ctx: ConsoleSession, kind: str, text: str) -> None:
            await message(ctx, "notice", {"kind": kind, "text": text})

        async def interrupt_session(ctx: ConsoleSession) -> None:
            if not connected:
                await control_notice(ctx, "interrupt", "Console is not connected.")
                return
            status = await scoped_request(ctx, op="status")
            evaluation = status["evaluation"]
            if evaluation is None:
                await control_notice(
                    ctx,
                    "interrupt",
                    "No ordinary evaluation is running."
                    if status["idle"]
                    else f"Console is {status['state']}; no interrupt target is available yet.",
                )
                return
            await control_notice(
                ctx,
                "interrupt",
                f"Human requested interruption of evaluation {evaluation['id']}; execution may still be running.",
            )
            try:
                await scoped_request(
                    ctx, op="interrupt", evaluation_id=evaluation["id"]
                )
            except ControlError as error:
                await control_notice(
                    ctx, "interrupt", f"Interrupt request failed: {error}"
                )
            else:
                await control_notice(
                    ctx,
                    "interrupt",
                    "Interrupt request accepted; execution may still be running.",
                )

        async def restart_session(ctx: ConsoleSession) -> None:
            if not connected:
                await control_notice(ctx, "restart", "Console is not connected.")
                return
            if ctx.restart_active:
                return
            ctx.restart_active = True
            try:
                await control_notice(
                    ctx, "restart", "Human requested a session restart."
                )
                await change(ctx.state, "Restarting…")
                await scoped_request(ctx, op="restart")
                # Observe the new identity even if its subscription update is
                # still queued. Replacement resets viewers and prepares runtimes.
                await publish_snapshot(ctx, await scoped_request(ctx, op="output"))
                # Replacement is confirmed. Preparation reports its progress and
                # failures separately; it must not prevent another restart.
                await control_notice(
                    ctx,
                    "restart",
                    "Session restarted; previous in-memory state was cleared.",
                )
            except ControlError as error:
                await control_notice(ctx, "restart", f"Restart request failed: {error}")
                await change(ctx.state, f"Restart failed: {error}")
            finally:
                ctx.restart_active = False

        async def open_table(ctx: ConsoleSession, language: str, name: str) -> None:
            async with reactive.lock():
                ui.update_navset("inspector_tab", selected="Table", session=session)
            await ctx.table.open(language, name)

        async def agent_reply(ctx: ConsoleSession, prompt: str) -> AsyncIterator[str]:
            nonlocal agent_active
            if agent_active:
                raise RuntimeError("Wait for the active agent response")
            agent_active = True
            try:
                await ctx.ready.wait()
                assert thread is not None
                await change(agent_status, "Running")
                streamed_items: set[str] = set()
                previous_item: str | None = None
                owners: dict[str, ConsoleSession] = {}
                origins: dict[str, ConsoleSession] = {}
                turn = await thread.turn(
                    f"Selected Console session: {json.dumps(ctx.name)}.\n\n{prompt}",
                    model=MODEL,
                    effort="low",
                )
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
                            if item.id not in owners:
                                has_code = any(
                                    item.arguments.get(language)
                                    for language in ("r", "python", "sql")
                                )
                                # An unscoped empty poll covers all sessions. Keep its
                                # response separate in the view active at call start.
                                aggregate = (
                                    not has_code
                                    and not item.arguments.get("control")
                                    and not item.arguments.get("requirements")
                                    and "session" not in item.arguments
                                )
                                with reactive.isolate():
                                    origins[item.id] = current()
                                    target = (
                                        current().name
                                        if aggregate
                                        else item.arguments.get("session", "default")
                                    )
                                if target not in contexts and has_code:
                                    async with reactive.lock():
                                        make_context(target)
                                        await reactive.flush()
                                owners[item.id] = contexts.get(target, origins[item.id])
                            owner = owners[item.id]
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
                                    await message(
                                        owner,
                                        "submission",
                                        submitted_source(owner, submission),
                                    )
                                if item.arguments.get("control") == "interrupt":
                                    await message(
                                        owner,
                                        "notice",
                                        {
                                            "kind": "interrupt",
                                            "text": "Agent requested interruption; execution may still be running.",
                                        },
                                    )
                            else:
                                await discover_sessions()
                                if not owner.subscribed:
                                    # Rejected code never created a Console session.
                                    # Show its failure in the originating view, without
                                    # leaving a phantom name in the active selector.
                                    await retire(owner)
                                    owner = origins[item.id]
                                    for submission in submissions:
                                        await message(
                                            owner,
                                            "submission",
                                            submitted_source(
                                                owner, {**submission, "state": "failed"}
                                            ),
                                        )
                                await message(
                                    owner,
                                    "agent-response",
                                    {
                                        "id": item.id,
                                        "output_controls": response_controls(
                                            owner.incarnation + item.id
                                        ),
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
                                            else "All sessions poll"
                                            if "session" not in item.arguments
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
                                await message(
                                    owner,
                                    "agent-outcome",
                                    {
                                        "ids": [item["id"] for item in submissions],
                                        "text": outcome,
                                        "status": item.status.value,
                                    },
                                )
                                if item.arguments.get("control") == "interrupt":
                                    await message(
                                        owner,
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
                if live(ctx):
                    start(ctx.environments.refresh(), ctx)

        async def create_session(name: str) -> None:
            async with registry_lock:
                created = await request(endpoint, op="create", session=name)
                if name not in contexts:
                    async with reactive.lock():
                        make_context(name)
                        await reactive.flush()
                ctx = contexts[name]
                if created["state"] == "stopped":
                    ctx.retired_worker = created["worker_id"]
                attach(ctx)
                await activate(ctx)

        async def close_session(ctx: ConsoleSession) -> None:
            async with registry_lock:
                # Fence and cancel UI producers before retirement; otherwise a
                # queued setup cell could reopen the worker just after close.
                ctx.closing = True
                ctx.ready.clear()
                active = list(ctx.tasks)
                for task in active:
                    task.cancel()
                await asyncio.gather(*active, return_exceptions=True)
                try:
                    await request(endpoint, session=ctx.name, op="close")
                except Exception:
                    ctx.closing = False
                    ctx.subscribed = False
                    attach(ctx)
                    raise
                await retire(ctx)

        @reactive.effect
        @reactive.event(input.console_session)
        def choose_session() -> None:
            name = input.console_session()
            if name in contexts and current().name != name:
                start(activate(contexts[name]))

        @reactive.effect
        @reactive.event(input.new_console_session)
        def new_session() -> None:
            ui.modal_show(new_session_modal())

        @reactive.effect
        @reactive.event(input.create_console_session)
        def create_clicked() -> None:
            name = input.new_session_name().strip()
            if not name:
                ui.notification_show("Enter a session name.", type="error")
                return
            ui.modal_remove()
            start(create_session(name))

        @reactive.effect
        @reactive.event(input.close_console_session)
        def close_clicked() -> None:
            ctx = requested()
            if ctx.name != "default":
                start(close_session(ctx))

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
            ctx = requested()
            start(
                evaluate(ctx, input.language(), input.source(), input.execution()), ctx
            )

        @reactive.effect
        @reactive.event(input.interrupt)
        def interrupt_console() -> None:
            ctx = requested()
            start(interrupt_session(ctx), ctx)

        @reactive.effect
        @reactive.event(input.restart)
        def restart_console() -> None:
            ctx = requested()
            start(restart_session(ctx), ctx)

        @reactive.effect
        @reactive.event(input.open_table)
        def select_table() -> None:
            value = input.open_table()
            ctx = resolve(value)
            if ctx is not requested():
                ui.notification_show(
                    "Variables changed. Choose the table again.", type="message"
                )
                return
            start(open_table(ctx, value["language"], value["name"]), ctx)

        @reactive.effect
        def inspect_environments() -> None:
            ctx = current()
            if input.inspector_tab() == "Environments" and connected:
                start(ctx.environments.refresh(), ctx)

        @agent_chat.on_user_submit
        async def submit_prompt(prompt: str) -> None:
            # Shiny runs this iterator in its own ExtendedTask, so Console's
            # direct controls and subscriptions remain responsive during a turn.
            await agent_chat.append_message_stream(agent_reply(requested(), prompt))

        @render.text
        def connection_state() -> str:
            shared = state.get()
            return shared if shared.startswith("Error:") else current().state.get()

        @render.text
        def agent_state() -> str:
            return agent_status.get()

        @output(suspend_when_hidden=False)
        @render.code
        def r_variables() -> str:
            return current().variable_views["r"].get()

        @output(suspend_when_hidden=False)
        @render.code
        def python_variables() -> str:
            return current().variable_views["python"].get()

        def table_buttons(language: str) -> ui.TagList:
            ctx = current()
            return ui.TagList(
                *(
                    ui.tags.button(
                        f"Open {name}",
                        type="button",
                        class_="btn btn-outline-secondary table-open",
                        **{
                            "data-table-language": language,
                            "data-table-name": name,
                            "data-session": ctx.name,
                            "data-incarnation": ctx.incarnation,
                        },
                    )
                    for name in ctx.table_names[language].get()
                )
            )

        @output(suspend_when_hidden=False)
        @render.ui
        def r_tables() -> ui.TagList:
            return table_buttons("r")

        @output(suspend_when_hidden=False)
        @render.ui
        def python_tables() -> ui.TagList:
            return table_buttons("python")

        async def close() -> None:
            with reactive.isolate():
                agent_chat.latest_message_stream.cancel()
            active = list(tasks) + [
                task for ctx in contexts.values() for task in ctx.tasks
            ]
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)
            for ctx in contexts.values():
                ctx.table.dispose()

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
