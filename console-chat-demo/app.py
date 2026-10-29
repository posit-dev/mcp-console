"""A session-scoped Shiny interface to Console and chatlas."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import pandas as pd
from chatlas import ContentToolRequest, ContentToolResult
from chatlas.types import ContentImageInline
from runtime import MODEL, Pool
from shiny import App, Inputs, Outputs, Session, reactive, render, ui
from shinychat import message_content
from yaml12 import format_yaml

HERE = Path(__file__).resolve().parent
LOG = logging.getLogger(__name__)


def create_app(pool: Pool) -> App:
    """Create one web server; each browser gets its own Console and chat."""
    sample = pd.read_csv(HERE / "data" / "sample.csv")
    suggestions = [
        (
            "Compare regions",
            (
                "Which regions have the strongest profit margins? Use SQL to calculate "
                "revenue minus cost from data.csv, then compare margins and explain the result."
            ),
            "Find where the business earns the most.",
        ),
        (
            "See the trend",
            (
                "Use R to plot monthly revenue from data.csv. Explain the trend and "
                "any differences between Atlas and Beacon."
            ),
            "Make a revenue chart with R.",
        ),
        (
            "Build a model",
            (
                "Use Python to fit a simple monthly revenue trend from data.csv. "
                "Show the fit and explain the limits of forecasting from one year of synthetic data."
            ),
            "Fit a model and inspect its assumptions.",
        ),
    ]
    greeting = ui.TagList(
        ui.div("A question is a place to start.", class_="welcome-heading"),
        ui.p(
            "Explore the sample sales data, make a chart, or try a model. "
            "Your R, Python and SQL state stays available as the conversation develops.",
            class_="welcome-copy",
        ),
        ui.tags.ul(
            *[
                ui.tags.li(
                    ui.tags.span(
                        description,
                        title=title,
                        class_="suggestion submit",
                        **{"data-suggestion": prompt},
                    )
                )
                for title, prompt, description in suggestions
            ],
            class_="starter-prompts",
        ),
    )
    app_ui = ui.page_fillable(
        ui.tags.head(
            ui.tags.link(rel="stylesheet", href="styles.css"),
            ui.tags.meta(name="color-scheme", content="light"),
        ),
        ui.tags.header(
            ui.div(
                ui.div(">_", class_="brand-mark", **{"aria-hidden": "true"}),
                ui.div(
                    ui.h1("Console Chat"),
                    ui.p("Explore · calculate · model"),
                ),
                class_="brand",
            ),
            ui.div(
                ui.span("R + Python + SQL", class_="runtime-badge"),
                ui.output_text("connection_status", inline=True),
                class_="header-status",
                **{"aria-live": "polite"},
            ),
            class_="app-header",
        ),
        ui.layout_columns(
            ui.card(
                ui.card_header(
                    ui.div(
                        ui.span("Your analysis", class_="section-title"),
                        ui.div(
                            ui.input_action_button(
                                "stop",
                                "Stop",
                                class_="btn-sm btn-quiet",
                                title="Stop the current work and discard this runtime and agent session",
                            ),
                            ui.input_action_button(
                                "reset", "New session", class_="btn-sm btn-quiet"
                            ),
                            ui.download_button(
                                "download_analysis", "Export", class_="btn-sm btn-quiet"
                            ),
                            class_="chat-actions",
                        ),
                        class_="section-bar",
                    )
                ),
                ui.chat_ui(
                    "chat",
                    greeting=greeting,
                    placeholder="Ask a question about the data…",
                    height="100%",
                    width="100%",
                    show_history=False,
                    allow_attachments=False,
                    enable_cancel=False,
                ),
                ui.div(ui.output_text("session_info"), class_="session-info"),
                class_="chat-card",
                fill=True,
            ),
            ui.div(
                ui.card(
                    ui.card_header(
                        ui.div("THE STARTING POINT", class_="eyebrow"),
                        ui.h2("A year in sales"),
                        ui.p("A small, synthetic dataset with room to explore."),
                    ),
                    ui.div(
                        ui.div(ui.strong(str(len(sample))), ui.span("rows")),
                        ui.div(ui.strong("12"), ui.span("months")),
                        ui.div(ui.strong("4"), ui.span("regions")),
                        class_="dataset-facts",
                    ),
                    ui.output_data_frame("data_preview"),
                    ui.p(
                        "Original sample · data.csv · currency in USD",
                        class_="data-caption",
                    ),
                    class_="data-card",
                ),
                ui.card(
                    ui.card_header(
                        ui.div("WHAT YOU DISCOVER", class_="eyebrow"),
                        ui.h2("Plots & results"),
                    ),
                    ui.output_ui("plot_gallery"),
                    class_="results-card",
                ),
                ui.p(
                    "Code runs in your private Console workspace. Questions and "
                    "execution results are sent to the model. Expand tool results "
                    "in the chat to inspect the code and output.",
                    class_="privacy-note",
                ),
                class_="inspection-column",
            ),
            col_widths=(7, 5),
            class_="main-layout",
        ),
        title="Console Chat",
        fillable_mobile=True,
        class_="console-chat-app",
    )

    def server(input: Inputs, output: Outputs, session: Session) -> None:
        chat = ui.Chat("chat", history=False)
        status = reactive.value("Ready")
        identity = reactive.value("A private session starts with your first question")
        images = reactive.value(())
        transcript: list[str] = []
        backend = None
        active: asyncio.Task | None = None
        maintenance: asyncio.Task | None = None
        failed = False
        ending = False

        async def reply(prompt: str):
            nonlocal backend, active, failed
            if ending or maintenance is not None:
                yield "The session is closing. Please wait before sending another question."
                return
            if failed:
                yield "Start a new session to continue after this error."
                return
            if active is not None:
                yield "Wait for the current analysis to finish before sending another question."
                return
            active = asyncio.current_task()
            answer: list[str] = []
            transcript.append(f"## You\n\n{prompt}\n")
            try:
                if backend is None:
                    status.set("Starting")
                    backend = await pool.open()
                    identity.set(
                        f"Session {backend.workspace.parent.name[:8]} · {MODEL}",
                    )
                status.set("Working")
                async with contextlib.aclosing(backend.stream(prompt)) as stream:
                    async for chunk in stream:
                        if isinstance(chunk, ContentToolRequest):
                            options = {
                                key: value
                                for key, value in chunk.arguments.items()
                                if key not in ("r", "python", "sql")
                                and value is not None
                            }
                            if options:
                                yaml = format_yaml(options, width=None)
                                longest = max(
                                    (len(run) for run in re.findall(r"`+", yaml)),
                                    default=0,
                                )
                                fence = "`" * max(3, longest + 1)
                                transcript.append(
                                    f"### Tool options\n\n{fence}yaml\n{yaml}\n{fence}\n"
                                )
                            for language in ("r", "python", "sql"):
                                code = chunk.arguments.get(language)
                                if code:
                                    longest = max(
                                        (len(run) for run in re.findall(r"`+", code)),
                                        default=0,
                                    )
                                    fence = "`" * max(3, longest + 1)
                                    transcript.append(
                                        f"### Code submitted\n\n{fence}{language}\n"
                                        f"{code}\n{fence}\n"
                                    )
                            # Keep Shiny's native cards; change only their display text.
                            display = message_content(chunk)
                            display.blocks[0]["arguments"] = format_yaml(
                                chunk.arguments, width=None
                            )
                            yield display
                        elif isinstance(chunk, ContentToolResult):
                            parts = (
                                chunk.value
                                if isinstance(chunk.value, list)
                                else [chunk.value]
                            )
                            plots = [
                                x for x in parts if isinstance(x, ContentImageInline)
                            ]
                            text = "\n".join(
                                str(x)
                                for x in parts
                                if not isinstance(x, ContentImageInline)
                            )
                            transcript.append(
                                f"### Console output\n\n```text\n{text}\n```\n"
                            )
                            # Only the display copy changes. Chatlas retains the original
                            # image-bearing result and sends it to the model.
                            display = message_content(
                                chunk.model_copy(update={"value": text})
                            )
                            display.blocks[0]["request_call"] = format_yaml(
                                chunk.request.arguments, width=None
                            )
                            yield display
                            for plot in plots:
                                url = (
                                    f"data:{plot.image_content_type};base64,{plot.data}"
                                )
                                with reactive.isolate():
                                    images.set((*images.get(), url)[-6:])
                                yield ui.tags.img(
                                    src=url,
                                    alt="Plot produced by Console",
                                    class_="chat-plot",
                                )
                            if plots:
                                transcript.append(
                                    f"\n[{len(plots)} plot(s) displayed in the app]\n"
                                )
                        else:
                            # Chatlas streams assistant text as strings even
                            # with content="all"; other chunks are rich content.
                            if isinstance(chunk, str):
                                answer.append(chunk)
                            yield chunk
                status.set("Ready")
            except asyncio.CancelledError:
                raise
            except Exception as error:
                LOG.exception("Analysis session failed")
                failed = True
                status.set("Error")
                if backend is not None:
                    await backend.close()
                    backend = None
                yield f"The analysis could not complete ({type(error).__name__}). Start a new session to retry."
            finally:
                if answer:
                    transcript.append("## Assistant\n\n" + "".join(answer) + "\n")
                active = None

        @chat.on_user_submit
        async def submit(prompt: str) -> None:
            # Shiny consumes this iterator in an ExtendedTask so startup and
            # model/tool calls do not block other interactions or clients.
            await chat.append_message_stream(reply(prompt))

        async def retire(clear: bool, disconnected: bool = False) -> None:
            nonlocal backend, failed, maintenance
            try:
                task = active
                if task is not None:
                    task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await task
                if backend is not None:
                    await backend.close()
                    backend = None
                failed = False
                if not disconnected:
                    if clear:
                        await chat.clear_messages(greeting=False)
                        transcript.clear()
                        images.set(())
                    else:
                        message = (
                            "Stopped. This runtime and agent conversation were discarded. "
                            "Your next question starts a fresh session; the earlier messages remain here for reference."
                        )
                        await chat.append_message(message)
                        transcript.append(f"## Session ended\n\n{message}\n")
                    identity.set("A private session starts with your next question")
                    status.set("Ready")
            except Exception:
                failed = True
                LOG.exception("Could not close analysis session")
                if not disconnected:
                    status.set("Error")
                    await chat.append_message(
                        "Could not close the session. Try New session again."
                    )
                else:
                    raise
            finally:
                maintenance = None

        @reactive.effect
        @reactive.event(input.stop)
        def stop() -> None:
            nonlocal maintenance
            if maintenance is None:
                status.set("Stopping")
                maintenance = asyncio.create_task(retire(clear=False))

        @reactive.effect
        @reactive.event(input.reset)
        def reset() -> None:
            nonlocal maintenance
            if maintenance is None:
                status.set("Starting")
                maintenance = asyncio.create_task(retire(clear=True))

        async def cleanup() -> None:
            nonlocal ending
            ending = True
            if maintenance is not None:
                await maintenance
            await retire(clear=False, disconnected=True)

        session.on_ended(cleanup)

        @reactive.effect
        def controls() -> None:
            current = status.get()
            ui.update_action_button(
                "stop", disabled=current not in ("Starting", "Working")
            )
            ui.update_action_button("reset", disabled=current == "Stopping")

        @render.text
        def connection_status() -> str:
            return status.get()

        @render.text
        def session_info() -> str:
            return identity.get()

        @render.data_frame
        def data_preview():
            return render.DataGrid(sample, height="265px", filters=True, summary=True)

        @render.ui
        def plot_gallery():
            plots = images.get()
            if not plots:
                return ui.div(
                    ui.span("↗", class_="empty-plot-icon", **{"aria-hidden": "true"}),
                    ui.strong("The next insight starts with a question"),
                    ui.p(
                        "Ask for a chart. Your latest six plots will appear here, with the calculations in the chat."
                    ),
                    class_="empty-gallery",
                )
            return ui.div(
                *[
                    ui.tags.figure(
                        ui.tags.img(src=url, alt=f"Analysis plot {i + 1}"),
                        ui.tags.figcaption(f"Plot {i + 1}"),
                    )
                    for i, url in enumerate(plots)
                ],
                class_="plot-gallery",
            )

        @render.download_button(
            filename="console-chat-analysis.md", media_type="text/markdown"
        )
        def download_analysis():
            yield "# Console Chat analysis\n\n" + "\n".join(transcript)

    app = App(app_ui, server, static_assets=HERE / "www")
    shiny_lifespan = app.starlette_app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(asgi_app):
        try:
            async with shiny_lifespan(asgi_app):
                yield
        finally:
            await pool.close()

    app.starlette_app.router.lifespan_context = lifespan
    return app


binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
if binary is None:
    raise RuntimeError(
        "Install mcp-console or set CONSOLE_CHAT_BINARY to its executable."
    )

app = create_app(
    Pool(
        root=Path(os.environ.get("CONSOLE_CHAT_RUNTIME", HERE / ".runtime" / "server")),
        binary=Path(binary),
        capacity=int(os.environ.get("CONSOLE_CHAT_CAPACITY", "8")),
        private_paths=[
            Path(x)
            for x in json.loads(os.environ.get("CONSOLE_CHAT_PRIVATE_PATHS", "[]"))
        ],
        r=Path(os.environ["CONSOLE_CHAT_R"])
        if os.environ.get("CONSOLE_CHAT_R")
        else None,
    )
)
