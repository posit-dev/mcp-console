"""A session-scoped Shiny interface to Console and chatlas."""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import logging
import os
import re
import shutil
import sys
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

import duckdb
import sqlglot
from chatlas import ContentToolRequest, ContentToolResult
from chatlas.types import ContentImageInline
from runtime import DATA, MODEL, TABLES, Pool
from shiny import App, Inputs, Outputs, Session, reactive, render, ui
from shinychat import message_content
from shinychat.types import ToolResultDisplay
from yaml12 import format_yaml

HERE = Path(__file__).resolve().parent
LANGUAGES = {"r": "R", "python": "Python", "sql": "SQL"}
LOG = logging.getLogger(__name__)


async def formatted(language: str, code: str) -> str:
    """Code in the formatter's style, or unchanged when it does not parse."""
    if language == "sql":
        try:
            return ";\n\n".join(
                sqlglot.transpile(code, read="duckdb", write="duckdb", pretty=True)
            )
        except sqlglot.ParseError:
            return code
    command = {
        "r": ["air", "format", "--no-configuration", "--stdin-file-path", "cell.R"],
        "python": ["ruff", "format", "--isolated", "--stdin-filename", "cell.py", "-"],
    }[language]
    # The formatters ship in this environment's bin directory.
    process = await asyncio.create_subprocess_exec(
        str(Path(sys.executable).parent / command[0]),
        *command[1:],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await process.communicate(code.encode())
    return stdout.decode().rstrip("\n") if process.returncode == 0 else code


def fenced(language: str, code: str) -> str:
    longest = max((len(run) for run in re.findall(r"`+", code)), default=0)
    fence = "`" * max(3, longest + 1)
    return f"{fence}{language}\n{code}\n{fence}"


async def cell(arguments: dict) -> str:
    """Markdown for a send call's note and code, as written and formatted.

    The Formatted switch chooses a version with CSS, so it also applies to
    earlier messages without re-rendering them.
    """
    parts = [arguments["note"]] if arguments.get("note") else []
    for language, label in LANGUAGES.items():
        if code := arguments.get(language):
            pretty = await formatted(language, code)
            versions = (
                [("code-written", code), ("code-formatted", pretty)]
                if pretty != code
                else [("", code)]
            )
            parts += [
                f'<div class="code-cell {kind}" data-language="{label}">\n\n'
                f"{fenced(language, text)}\n\n</div>"
                for kind, text in versions
            ]
    # Options such as a named session or a timeout explain how the code ran.
    options = {
        key: value
        for key, value in arguments.items()
        if key not in (*LANGUAGES, "note") and value is not None
    }
    if options:
        parts.append(fenced("yaml", format_yaml(options, width=None)))
    return "".join(f"\n\n{part}\n\n" for part in parts)


def create_app(pool: Pool, offer_dynamic: bool = True) -> App:
    """Create one web server; each browser gets its own Console and chat.

    `offer_dynamic` shows the checkbox that lets a session prepare packages.
    """
    # The preview needs only a slice; sessions query the full tables.
    flights = DATA / "flights.parquet"
    [(total,)] = duckdb.sql(f"SELECT count(*) FROM '{flights}'").fetchall()
    sample = duckdb.sql(f"FROM '{flights}' LIMIT 1000").df()
    suggestions = [
        (
            "Compare carriers",
            (
                "Which carriers have the worst departure delays from each New York "
                "airport? Use SQL, and account for how many flights each carrier operates."
            ),
            "Find where delays concentrate.",
        ),
        (
            "See the seasons",
            (
                "Use R and ggplot2 to plot the average departure delay for each day "
                "of 2013. Explain the seasonal pattern and the worst days."
            ),
            "Chart a year of delays with R.",
        ),
        (
            "Model the weather",
            (
                "Use Python to join flights to weather and model how visibility, "
                "precipitation and wind relate to departure delays. Explain the limits."
            ),
            "Fit a model and inspect its assumptions.",
        ),
    ]
    greeting = ui.TagList(
        ui.div("A question is a place to start.", class_="welcome-heading"),
        ui.p(
            "Explore every flight that left New York in 2013, make a chart, or try a model. "
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
                            ui.output_ui("export", inline=True),
                            ui.div(
                                ui.input_switch(
                                    "formatted_code", "Formatted", value=True
                                ),
                                title="Show code formatted with air, ruff and sqlglot, or as the model wrote it",
                                class_="package-mode",
                            ),
                            ui.div(
                                ui.input_checkbox(
                                    "dynamic", "Dynamic packages", value=False
                                ),
                                title="Let the agent install packages. Startup is slower. Applies to the next session.",
                                class_="package-mode",
                            )
                            if offer_dynamic
                            else None,
                            class_="chat-actions",
                        ),
                        class_="section-bar",
                    )
                ),
                ui.chat_ui(
                    "chat",
                    greeting=greeting,
                    placeholder="Ask a question about the flights…",
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
                    # A view shown by the chat hides this default header.
                    ui.div(
                        ui.card_header(
                            ui.div("THE STARTING POINT", class_="eyebrow"),
                            ui.h2("New York flights, 2013"),
                            ui.p(
                                "Every departure from EWR, JFK and LGA, with weather, "
                                "planes, airports and airlines."
                            ),
                        ),
                        ui.div(
                            ui.div(ui.strong(f"{total:,}"), ui.span("flights")),
                            ui.div(ui.strong(str(len(TABLES))), ui.span("tables")),
                            ui.div(ui.strong("3"), ui.span("airports")),
                            class_="dataset-facts",
                        ),
                        class_="default-header",
                    ),
                    ui.output_ui("data_header"),
                    ui.output_data_frame("data_preview"),
                    ui.output_ui("data_caption"),
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
        # The latest recorded report outlives a stopped session until the next
        # New session, like the messages it accompanies.
        report = reactive.value(None)
        # The table the chat last showed with show_data; None shows all flights.
        view = reactive.value(None)
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
            try:
                if backend is None:
                    status.set("Starting")
                    with reactive.isolate():
                        dynamic = offer_dynamic and input.dynamic()
                    backend = await pool.open(dynamic=dynamic)
                    packages = "dynamic packages" if dynamic else "prepared packages"
                    identity.set(
                        f"Session {backend.workspace.parent.name[:8]} · {MODEL} · {packages}",
                    )
                status.set("Working")
                await backend.send(note=f"**Question:** {prompt}")
                async with contextlib.aclosing(backend.stream(prompt)) as stream:
                    async for chunk in stream:
                        if isinstance(chunk, ContentToolRequest):
                            # Show the submitted code itself; the result card
                            # below holds the output.
                            if chunk.name == "show_data":
                                yield await cell({"sql": chunk.arguments["query"]})
                            else:
                                yield await cell(chunk.arguments)
                        elif (
                            isinstance(chunk, ContentToolResult)
                            and chunk.request.name == "show_data"
                        ):
                            if chunk.error is None:
                                with reactive.isolate():
                                    view.set(chunk.extra)
                                # The panel's query runs outside Console, so the
                                # report keeps it as prose rather than a chunk.
                                await backend.send(
                                    note=f"**Shown in the data panel:** {chunk.extra['title']}\n\n"
                                    + fenced("sql", chunk.extra["query"])
                                )
                            yield message_content(
                                chunk.model_copy(
                                    update={
                                        "extra": {
                                            "display": ToolResultDisplay(
                                                title="Shown in the data panel",
                                                show_request=False,
                                            )
                                        }
                                    }
                                )
                            )
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
                            language = next(
                                (
                                    x
                                    for x in LANGUAGES
                                    if chunk.request.arguments.get(x)
                                ),
                                None,
                            )
                            # Only the display copy changes. Chatlas retains the original
                            # image-bearing result and sends it to the model.
                            yield message_content(
                                chunk.model_copy(
                                    update={
                                        "value": text,
                                        "extra": {
                                            "display": ToolResultDisplay(
                                                title=f"{LANGUAGES[language]} output"
                                                if language
                                                else "Console output",
                                                show_request=False,
                                            )
                                        },
                                    }
                                )
                            )
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
                        else:
                            # Chatlas streams assistant text as strings even
                            # with content="all"; other chunks are rich content.
                            if isinstance(chunk, str):
                                answer.append(chunk)
                            yield chunk
                # The answer, without its clickable follow-up question, closes
                # this turn in the session's recorded report.
                await backend.send(
                    note=re.sub(
                        r"<span class=\"suggestion[^>]*>.*?</span>",
                        "",
                        "".join(answer),
                        flags=re.DOTALL,
                    ).strip()
                )
                report.set(backend.report())
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
                        report.set(None)
                        images.set(())
                        view.set(None)
                    else:
                        message = (
                            "Stopped. This runtime and agent conversation were discarded. "
                            "Your next question starts a fresh session; the earlier messages remain here for reference."
                        )
                        await chat.append_message(message)
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

        @reactive.effect
        @reactive.event(input.all_flights)
        def all_flights() -> None:
            view.set(None)

        @render.ui
        def data_header():
            shown = view.get()
            if shown is None:
                return None
            return ui.card_header(
                ui.div(
                    ui.div("SHOWN BY THE CHAT", class_="eyebrow"),
                    ui.input_action_button(
                        "all_flights", "All flights", class_="btn-sm btn-quiet"
                    ),
                    class_="view-heading",
                ),
                ui.h2(shown["title"]),
                ui.tags.details(
                    ui.tags.summary("Query"),
                    ui.pre(ui.code(shown["query"], class_="language-sql")),
                ),
            )

        @render.data_frame
        def data_preview():
            shown = view.get()
            data = sample if shown is None else shown["data"]
            return render.DataGrid(data, height="265px", filters=True, summary=True)

        @render.ui
        def data_caption():
            shown = view.get()
            if shown is None:
                text = f"First {len(sample):,} flights · nycflights13 · read-only"
            elif shown["rows"] > len(shown["data"]):
                text = f"First {len(shown['data']):,} of {shown['rows']:,} rows"
            else:
                text = f"{shown['rows']:,} rows"
            return ui.p(text, class_="data-caption")

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

        @render.ui
        def export():
            if report.get() is None:
                return None
            return ui.download_button(
                "download_analysis",
                "Export QMD",
                class_="btn-sm btn-quiet",
                title="Download a Quarto report of the code with the data it reads",
            )

        @render.download(
            filename="console-chat-analysis.zip", media_type="application/zip"
        )
        def download_analysis():
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("console-chat-analysis/analysis.qmd", report.get())
                for table in TABLES:
                    archive.write(
                        DATA / f"{table}.parquet",
                        f"console-chat-analysis/nycflights13/{table}.parquet",
                    )
            yield buffer.getvalue()

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
    ),
    offer_dynamic=os.environ.get("CONSOLE_CHAT_DYNAMIC_PACKAGES", "1") == "1",
)
