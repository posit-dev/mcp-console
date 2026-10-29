"""Copy Console-owned recording projections without reconstructing output."""

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from shiny import Inputs, Session, reactive, ui


def export_ui() -> ui.Tag:
    return ui.popover(
        ui.tags.button(
            "Export",
            type="button",
            class_="btn btn-outline-secondary btn-sm dropdown-toggle",
        ),
        ui.div(
            ui.tags.button(
                "Copy QMD",
                type="button",
                class_="btn btn-sm text-start",
                **{"data-export-format": "qmd"},
            ),
            ui.tags.button(
                "Copy Markdown",
                type="button",
                class_="btn btn-sm text-start",
                **{"data-export-format": "markdown"},
            ),
            class_="d-flex flex-column",
        ),
        ui.p(
            "QMD: current worker source. Markdown: this recording's transcript.",
            class_="small text-muted",
        ),
        ui.div(id="export_status", role="status", **{"aria-live": "polite"}),
        id="export_menu",
        placement="bottom",
    )


def export_server(
    input: Inputs,
    session: Session,
    recording: Callable[[dict[str, Any]], Awaitable[dict[str, Any]]],
) -> None:
    @reactive.effect
    @reactive.event(input.export_request)
    async def export_requested() -> None:
        request = input.export_request()
        response: dict[str, Any] = {"id": request["id"]}
        try:
            kind = request["format"]
            assert kind in {"qmd", "markdown"}
            paths = await recording(request)
            path = paths[kind]
            if path is None:
                raise RuntimeError(
                    "No QMD exists for the current worker. Submit code first."
                    if kind == "qmd"
                    else "No Markdown recording is available."
                )
            response["text"] = await asyncio.to_thread(
                Path(path).read_text, encoding="utf-8"
            )
        except (AssertionError, RuntimeError, OSError, UnicodeError) as error:
            response["error"] = str(error)
        await session.send_custom_message("export-projection", response)
