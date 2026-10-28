# /// script
# requires-python = ">=3.11"
# dependencies = ["shiny>=1.8.0", "openai-codex>=0.163.0"]
# ///
"""Local integration fixture: one agent, one Console worker, two input paths."""

import argparse
import asyncio
import contextlib
import json
import logging
import sys
import tempfile
import uuid
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from openai_codex import ApprovalMode, AsyncCodex, AsyncThread, Sandbox
from openai_codex.async_client import AsyncCodexClient
from pydantic import RootModel
from shiny import App, Inputs, Outputs, Session, reactive, render, ui

MODEL = "gpt-6-luna"
LOGGER = logging.getLogger(__name__)
# Console previews retain up to 8 MiB of encoded images in addition to text.
RESPONSE_LIMIT = 16 * 1024 * 1024


async def request(endpoint: Path, **fields: Any) -> dict[str, Any]:
    reader, writer = await asyncio.open_unix_connection(
        str(endpoint), limit=RESPONSE_LIMIT
    )
    try:
        writer.write(json.dumps({"id": "ui", **fields}).encode() + b"\n")
        await writer.drain()
        response = json.loads(await reader.readline())
        if "error" in response:
            raise RuntimeError(response["error"]["message"])
        return response["result"]
    finally:
        writer.close()
        await writer.wait_closed()


def blocks(content: list[dict[str, Any]]) -> list[Any]:
    rendered = []
    for block in content:
        if block["type"] == "text":
            rendered.append(ui.tags.pre(block["text"]))
        elif block["type"] == "image":
            rendered.append(
                ui.tags.img(
                    src=f"data:{block['mimeType']};base64,{block['data']}",
                    style="max-width: 100%",
                )
            )
    return rendered


def make_app(binary: Path, workspace: Path) -> App:
    app_ui = ui.page_fillable(
        ui.tags.style(
            ".entry {border-bottom: 1px solid #ddd; padding: .8rem 0;}"
            ".entry pre {white-space: pre-wrap; overflow-wrap: anywhere;}"
            ".ghost {opacity: .55; border-left: 3px solid #aaa; padding-left: 1rem;}"
            ".assistant {background: #f4f7fa; padding: .7rem;}"
        ),
        ui.h2("Console integration fixture"),
        ui.output_text("connection_state"),
        ui.layout_columns(
            ui.card(
                ui.card_header(f"Agent chat · {MODEL}", ui.output_text("agent_state")),
                ui.card_body(
                    ui.output_ui("chat"),
                    fillable=False,
                    min_height=0,
                    class_="history",
                ),
                ui.card_footer(
                    ui.input_text_area("prompt", "Ask the agent", rows=4, width="100%"),
                    ui.input_action_button("ask", "Send prompt"),
                ),
            ),
            ui.card(
                ui.card_header("Console transcript"),
                ui.card_body(
                    ui.output_ui("transcript"),
                    ui.div(ui.output_ui("pending_output"), class_="ghost"),
                    fillable=False,
                    min_height=0,
                    class_="history",
                ),
                ui.card_footer(
                    ui.layout_columns(
                        ui.input_select("language", "Language", ["python", "r"]),
                        ui.input_select(
                            "execution",
                            "Execution",
                            {
                                "serialized": "Ordinary cell",
                                "immediate": "Immediate",
                            },
                        ),
                        col_widths=(4, 8),
                        fill=False,
                    ),
                    ui.input_text_area("source", "Code", rows=5, width="100%"),
                    ui.input_action_button("evaluate", "Evaluate"),
                ),
            ),
            col_widths=(6, 6),
            row_heights="minmax(0, 1fr)",
        ),
        padding="0.75rem",
        gap="0.5rem",
        title="Console integration fixture",
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        state = reactive.value("Starting…")
        chat_entries = reactive.value([])
        agent_status = reactive.value("Idle")
        transcript_entries = reactive.value([])
        pending = reactive.value([])
        tasks: set[asyncio.Task[None]] = set()
        ready = asyncio.Event()
        endpoint: Path
        thread: AsyncThread | None = None
        agent_active = False

        async def change(
            value: reactive.Value[Any], item: Any, *, append: bool = False
        ) -> None:
            async with reactive.lock():
                with reactive.isolate():
                    value.set([*value.get(), item] if append else item)
                await reactive.flush()

        async def agent_message(
            item_id: str, text: str, *, delta: bool = False
        ) -> None:
            if not text:
                return
            async with reactive.lock():
                with reactive.isolate():
                    entries = list(chat_entries.get())
                for index, item in enumerate(entries):
                    if item.get("id") == item_id:
                        entries[index] = {
                            **item,
                            "text": item["text"] + text if delta else text,
                        }
                        break
                else:
                    entries.append({"id": item_id, "role": "assistant", "text": text})
                chat_entries.set(entries)
                await reactive.flush()

        async def transcript_entry(entry_id: str, **fields: Any) -> None:
            async with reactive.lock():
                with reactive.isolate():
                    entries = list(transcript_entries.get())
                for index, item in enumerate(entries):
                    if item["id"] == entry_id:
                        entries[index] = {**item, **fields}
                        break
                else:
                    entries.append({"id": entry_id, **fields})
                transcript_entries.set(entries)
                await reactive.flush()

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

        async def connect() -> None:
            nonlocal endpoint, thread
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
                                "languages=[r,python]",
                            ],
                            "cwd": str(workspace),
                            "env": {
                                "MCP_CONSOLE_HOME": str(root / "home"),
                                "RETICULATE_PYTHON": sys.executable,
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
                            "R or Python code requested by the user. The user also "
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
                        writer.write(b'{"id":"preview","op":"subscribe"}\n')
                        await writer.drain()
                        acknowledgement = json.loads(await reader.readline())
                        if "error" in acknowledgement:
                            raise RuntimeError(acknowledgement["error"]["message"])
                        ready.set()
                        await change(state, "Ready")
                        while line := await reader.readline():
                            event = json.loads(line)
                            if event.get("event") == "preview":
                                await change(pending, event["result"]["content"])
                        raise RuntimeError("Console subscription closed")
                    finally:
                        writer.close()
                        await writer.wait_closed()

        async def evaluate(language: str, source: str, execution: str) -> None:
            await ready.wait()
            entry_id = uuid.uuid4().hex
            await transcript_entry(
                entry_id, origin="direct", source=source, content=[], state="running"
            )
            fields = {"op": "evaluate", "language": language, "code": source}
            if execution == "serialized":
                fields.update(busy="wait", wait_ms=60000)
            else:
                fields["execution"] = execution
            try:
                result = await request(endpoint, **fields)
            except Exception as error:
                await transcript_entry(
                    entry_id,
                    content=[{"type": "text", "text": str(error)}],
                    state="failed",
                )
                raise
            await transcript_entry(
                entry_id, content=result["content"], state="complete"
            )

        async def ask(prompt: str) -> None:
            nonlocal agent_active
            if agent_active:
                raise RuntimeError("Wait for the active agent response")
            agent_active = True
            try:
                await ready.wait()
                assert thread is not None
                await change(agent_status, "Running")
                await change(
                    chat_entries, {"role": "user", "text": prompt}, append=True
                )
                turn = await thread.turn(prompt, model=MODEL, effort="low")
                async for event in turn.stream():
                    if event.method == "item/agentMessage/delta":
                        await agent_message(
                            event.payload.item_id, event.payload.delta, delta=True
                        )
                    elif event.method in ("item/started", "item/completed"):
                        item = event.payload.item.root
                        if (
                            item.type == "agentMessage"
                            and event.method == "item/completed"
                        ):
                            await agent_message(item.id, item.text)
                        elif (
                            item.type == "mcpToolCall"
                            and item.server == "console_fixture"
                        ):
                            source = "\n".join(
                                item.arguments[language]
                                for language in ("r", "python", "sql")
                                if item.arguments.get(language)
                            )
                            if event.method == "item/started":
                                await transcript_entry(
                                    item.id,
                                    origin="agent",
                                    source=source,
                                    content=[],
                                    state="running",
                                )
                                continue
                            content = (
                                item.result.content
                                if item.result
                                else [{"type": "text", "text": item.error.message}]
                            )
                            await transcript_entry(
                                item.id,
                                origin="agent",
                                source=source,
                                content=content,
                                state="complete",
                            )
                    elif event.method == "turn/completed":
                        if event.payload.turn.error:
                            raise RuntimeError(event.payload.turn.error.message)
                        await change(
                            agent_status, event.payload.turn.status.value.title()
                        )
            finally:
                agent_active = False

        @reactive.effect
        @reactive.event(input.evaluate)
        def submit_code() -> None:
            start(evaluate(input.language(), input.source(), input.execution()))

        @reactive.effect
        @reactive.event(input.ask)
        def submit_prompt() -> None:
            start(ask(input.prompt()))

        @render.text
        def connection_state() -> str:
            return state.get()

        @render.text
        def agent_state() -> str:
            return agent_status.get()

        @render.ui
        def chat() -> ui.TagList:
            return ui.TagList(
                *(
                    ui.div(
                        ui.tags.strong(item["role"].title()),
                        ui.p(item["text"]),
                        class_=item["role"],
                    )
                    for item in chat_entries.get()
                )
            )

        @render.ui
        def transcript() -> ui.TagList:
            return ui.TagList(
                *(
                    ui.div(
                        ui.tags.strong(item["origin"].title()),
                        ui.tags.small(f" · {item['state']}"),
                        ui.tags.pre(item["source"], class_="source"),
                        ui.div(*blocks(item["content"]), class_="content"),
                        class_=f"entry {item['origin']} {item['state']}",
                    )
                    for item in transcript_entries.get()
                )
            )

        @render.ui
        def pending_output() -> ui.TagList:
            content = pending.get()
            return ui.TagList(
                *([ui.tags.small("Pending ordinary cell output")] if content else []),
                *blocks(content),
            )

        async def close() -> None:
            active = list(tasks)
            for task in active:
                task.cancel()
            await asyncio.gather(*active, return_exceptions=True)

        session.on_ended(close)
        start(connect())

    return App(app_ui, server)


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
