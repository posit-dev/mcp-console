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
from shiny import App, Inputs, Outputs, Session, reactive, render, req, ui
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

    The Format code switch chooses a version with CSS, so it also applies to
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
    # Small previews are shared by all viewers; full tables stay in Parquet.
    originals = {}
    with duckdb.connect() as preview:
        for table in TABLES:
            source = str(DATA / f"{table}.parquet")
            [(total,)] = preview.execute(
                "SELECT count(*) FROM read_parquet(?)", [source]
            ).fetchall()
            originals[table] = {
                "title": table,
                "rows": total,
                "query": f"FROM {table}",
                "data": preview.execute(
                    "FROM read_parquet(?) LIMIT 1000", [source]
                ).df(),
            }
    choices = {
        name: f"{name} · {item['rows']:,} rows" for name, item in originals.items()
    }
    suggestions = [
        (
            "SQL",
            "Which airlines have the longest delays?",
            (
                "Which carriers have the worst departure delays from each New York "
                "airport? Use SQL, and account for how many flights each carrier operates."
            ),
            "The agent joins tables and compares groups.",
        ),
        (
            "R",
            "How do delays change through the year?",
            (
                "Use R and ggplot2 to plot the average departure delay for each day "
                "of 2013. Explain the seasonal pattern and the worst days."
            ),
            "The agent plots patterns with ggplot2.",
        ),
        (
            "Python",
            "Can weather help predict a delay?",
            (
                "Join flights to weather in SQL, then use Python to model how "
                "visibility, precipitation and wind relate to departure delays. "
                "Explain the limits."
            ),
            "The agent combines a SQL join with a Python model.",
        ),
    ]
    greeting = ui.TagList(
        ui.div("An agent with R, Python and SQL", class_="welcome-heading"),
        ui.p(
            "Ask a question in plain English. The agent writes and runs code to join "
            "and filter data, draw plots, fit models, and show selected rows or summaries. "
            "It can pass results between R, Python and SQL as the analysis develops.",
            class_="welcome-copy",
        ),
        ui.tags.ul(
            *[
                ui.tags.li(
                    ui.tags.button(
                        ui.span(language + " ", class_="starter-language"),
                        ui.strong(question + " "),
                        ui.span(description, class_="starter-detail"),
                        type="button",
                        class_="suggestion submit",
                        **{"data-suggestion": prompt, "data-runtime": language},
                    )
                )
                for language, question, prompt, description in suggestions
            ],
            class_="starter-prompts",
        ),
        ui.p(
            "Choose a question or ask your own. Follow-ups help you visualize, investigate, or model the data.",
            class_="welcome-copy",
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
                    ui.p("A data analysis agent with MCP Console"),
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
                                    "formatted_code", "Format code", value=True
                                ),
                                title="Format the displayed code for readability. The executed code is unchanged.",
                                class_="package-mode",
                            ),
                            ui.div(
                                ui.input_checkbox(
                                    "dynamic", "Allow package installs", value=False
                                ),
                                title="Applies to the next session. Packages are prepared in its own sandbox; startup can take minutes.",
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
                ui.navset_card_tab(
                    ui.nav_panel(
                        "About the demo",
                        ui.h2("New York flights, 2013"),
                        ui.p(
                            "The agent's runtime has access to five read-only Parquet files "
                            "from nycflights13: 336,776 scheduled flights from Newark, JFK "
                            "and LaGuardia in 2013, plus weather, aircraft, airports and airlines."
                        ),
                        ui.tags.table(
                            ui.tags.caption("Five Parquet files, one table per file"),
                            ui.tags.thead(
                                ui.tags.tr(ui.tags.th("Table"), ui.tags.th("Rows"))
                            ),
                            ui.tags.tbody(
                                *[
                                    ui.tags.tr(
                                        ui.tags.td(name),
                                        ui.tags.td(f"{item['rows']:,}"),
                                    )
                                    for name, item in originals.items()
                                ]
                            ),
                            class_="dataset-table",
                        ),
                        ui.p(
                            "The runtime exposes these files as table views. The agent can "
                            "join, filter and summarize them on demand. Sessions share the files "
                            "instead of loading a full copy into memory for each visitor. "
                            "Query results and objects the agent creates still use memory."
                        ),
                        ui.h3("The agent's workspace"),
                        ui.tags.ul(
                            ui.tags.li(
                                "The agent can run R, Python and SQL in one persistent workspace, "
                                "reuse earlier results and pass objects between languages."
                            ),
                            ui.tags.li(
                                "Each visitor gets a separate agent and sandboxed runtime. "
                                "Several people can work at once on one app server."
                            ),
                            ui.tags.li(
                                "The agent can use installed packages and create files in its workspace. "
                                "The sandbox blocks access to other sessions and restricts file writes and networking."
                            ),
                        ),
                        ui.p(
                            "The agent's code, output and plots appear in the chat. It can also "
                            "present selected rows or summaries in Table views. Download the "
                            "analysis as a Quarto report when you finish."
                        ),
                        value="about",
                    ),
                    ui.nav_panel(
                        "Table views",
                        ui.p(
                            "Ask the agent to show a subset of the data or a summary here. "
                            "Choose a source table or one of the views it has created."
                        ),
                        ui.input_select(
                            "table_view", "View", choices=choices, selected="flights"
                        ),
                        ui.output_ui("data_header"),
                        ui.output_data_frame("data_preview"),
                        ui.output_ui("data_caption"),
                        value="tables",
                    ),
                    id="inspection",
                    selected="about",
                    full_screen=True,
                ),
                ui.p(
                    "Questions, requested data and tool output are sent to the model provider. "
                    "Your session ends when you disconnect or choose New session.",
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
        # The latest recorded report outlives a stopped session until the next
        # New session, like the messages it accompanies.
        reports = reactive.value({})
        # Keep the bounded previews returned by show_data, not full tables.
        views = reactive.value({})
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
                        if (
                            isinstance(chunk, (ContentToolRequest, ContentToolResult))
                            and (
                                chunk.name
                                if isinstance(chunk, ContentToolRequest)
                                else chunk.request.name
                            )
                            == "suggest_next"
                        ):
                            continue
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
                                    shown = chunk.extra
                                    key = "query:" + shown["title"]
                                    saved = {**views.get(), key: shown}
                                    views.set(saved)
                                    ui.update_select(
                                        "table_view",
                                        choices={
                                            **choices,
                                            **{k: v["title"] for k, v in saved.items()},
                                        },
                                        selected=key,
                                    )
                                    ui.update_navset("inspection", selected="tables")
                                # The panel's query runs outside Console, so the
                                # report keeps it as prose rather than a chunk.
                                await backend.send(
                                    note=f"**Shown in Table views:** {chunk.extra['title']}\n\n"
                                    + fenced("sql", chunk.extra["query"])
                                )
                            yield message_content(
                                chunk.model_copy(
                                    update={
                                        "extra": {
                                            "display": ToolResultDisplay(
                                                title="Shown in Table views"
                                                if chunk.error is None
                                                else "Table query failed",
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
                await backend.send(note="".join(answer).strip())
                for kind, language, question in backend.suggestions:
                    label = {
                        "visualize": "Visualize",
                        "investigate": "Investigate",
                        "method": "Model",
                    }[kind]
                    next_prompt = f"Use {language} as the main language. {question}"
                    if kind == "visualize":
                        next_prompt += f"\n\nInclude a plot drawn in {language}."
                    # Suggestions are enhanced by ShinyChat's Markdown renderer.
                    # A raw HTML stream block would display an inert span.
                    yield (
                        "\n\n"
                        + str(
                            ui.tags.button(
                                ui.span(
                                    ui.span(label, class_="next-category"),
                                    ui.span(
                                        language,
                                        class_="next-language",
                                        title="Preferred language",
                                    ),
                                    class_="next-heading",
                                ),
                                ui.span(question, class_="next-question-text"),
                                type="button",
                                class_="suggestion submit next-question",
                                **{
                                    "data-suggestion": next_prompt,
                                    "data-runtime": language,
                                    "data-kind": kind,
                                },
                            )
                        )
                        + "\n\n"
                    )
                reports.set(backend.reports())
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
                        reports.set({})
                        views.set({})
                        ui.update_select(
                            "table_view", choices=choices, selected="flights"
                        )
                        ui.update_navset("inspection", selected="about")
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

        @reactive.calc
        def selected_view():
            key = input.table_view()
            if key in originals:
                return originals[key]
            saved = views.get()
            req(key in saved)  # The select input settles after New session clears it.
            return saved[key]

        @render.ui
        def data_header():
            shown = selected_view()
            return ui.div(
                ui.h2(shown["title"]),
                ui.tags.details(
                    ui.tags.summary("View SQL query"),
                    ui.pre(ui.code(shown["query"], class_="language-sql")),
                ),
                class_="view-heading",
            )

        @render.data_frame
        def data_preview():
            shown = selected_view()
            return render.DataGrid(
                shown["data"], width="100%", filters=True, summary=True
            )

        @render.ui
        def data_caption():
            shown = selected_view()
            displayed, total = len(shown["data"]), shown["rows"]
            text = (
                f"{total:,} rows"
                if displayed == total
                else f"First {displayed:,} of {total:,} rows"
            )
            return ui.p(
                text
                + ". Table filters apply to this preview; ask the agent to filter the full dataset.",
                class_="data-caption",
            )

        @render.ui
        def export():
            if not reports.get():
                return None
            return ui.download_button(
                "download_analysis",
                "Download report",
                class_="btn-sm btn-quiet",
                title="Download a zip containing a Quarto (.qmd) report and its Parquet data",
            )

        @render.download(
            filename="console-chat-analysis.zip", media_type="application/zip"
        )
        def download_analysis():
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, report in reports.get().items():
                    archive.writestr(f"console-chat-analysis/{name}", report)
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
