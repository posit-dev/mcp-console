"""Inspect recorded output on demand without executing Console code."""

import asyncio
import base64
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from shiny import Inputs, Session, reactive, render, ui

PAGE_BYTES = 16 * 1024
IMAGE_BYTES = 8 * 1024 * 1024


@dataclass
class RecordedCall:
    directory: Path
    call_id: int
    immediate: bool
    cursor: int = 0
    owner: dict[str, Any] | None = None
    pieces: list[dict[str, Any]] = field(default_factory=list)
    images: list[dict[str, Any]] = field(default_factory=list)
    discarded: int = 0
    omitted_images: int = 0
    complete: bool = False

    def path(self, relative: str) -> Path:
        path = (self.directory / relative).resolve()
        if not path.is_relative_to(self.directory):
            raise ValueError("Recorded output path is outside the recording directory.")
        return path

    def refresh(self) -> None:
        # Keep only the selected call's compact ranges, not a second journal.
        # Refresh resumes after the last complete JSONL record. Paging only
        # reads the selected range from its raw file.
        with (self.directory / "capture/events.jsonl").open("rb") as journal:
            journal.seek(self.cursor)
            while line := journal.readline():
                if not line.endswith(b"\n"):
                    break
                event = json.loads(line)
                self.cursor = journal.tell()
                kind = event["event"]
                if kind == "cell_started" and event["call_id"] == self.call_id:
                    self.owner = event["owner"]
                elif kind == "cell_ended" and event["call_id"] == self.call_id:
                    self.complete = True
                elif kind == "output_text" and self.owner == event["owner"]:
                    self.discarded += event["discarded_bytes"]
                    if event["bytes"]:
                        piece = {
                            "type": "text",
                            "path": event["path"],
                            "offset": event["offset"],
                            "bytes": event["bytes"],
                        }
                        previous = self.pieces[-1] if self.pieces else None
                        if (
                            previous
                            and previous["type"] == "text"
                            and previous["path"] == piece["path"]
                            and previous["offset"] + previous["bytes"]
                            == piece["offset"]
                        ):
                            previous["bytes"] += piece["bytes"]
                        else:
                            self.pieces.append(piece)
                elif (
                    kind == "image_retained"
                    and self.immediate
                    and event["owner"].get("call_id") == self.call_id
                ):
                    self.images.append({"type": "image", **event, "additional": True})
                elif (
                    kind == "image_retained"
                    and not self.immediate
                    and self.owner == event["owner"]
                ):
                    self.pieces.append({"type": "image", **event})
                elif (
                    kind == "image_omitted"
                    and event["owner"] is not None
                    and (
                        self.owner == event["owner"]
                        or self.immediate
                        and event["owner"].get("call_id") == self.call_id
                    )
                ):
                    self.omitted_images += 1
                elif (
                    kind == "tool_result"
                    and event["call_id"] == self.call_id
                    and self.immediate
                ):
                    # Immediate managed text has no ordinary-cell raw file.
                    # Its recorded response is independently bounded; do not
                    # pretend it is the raw foreground cell's output.
                    self.complete = True
                    self.pieces = event["result"]["content"]
        if self.immediate:
            included = {piece.get("path") for piece in self.pieces}
            self.pieces.extend(
                image for image in self.images if image["path"] not in included
            )

    @property
    def pages(self) -> int:
        return max(1, sum(self.page_count(piece) for piece in self.pieces))

    def page_count(self, piece: dict[str, Any]) -> int:
        return (
            max(1, math.ceil(piece["bytes"] / PAGE_BYTES))
            if piece["type"] == "text" and "path" in piece
            else 1
        )

    def page(self, number: int) -> ui.TagChild:
        for piece in self.pieces:
            count = self.page_count(piece)
            if number >= count:
                number -= count
                continue
            if piece["type"] == "text":
                if "path" not in piece:
                    return ui.tags.pre(piece["text"])
                start = number * PAGE_BYTES
                length = min(PAGE_BYTES, piece["bytes"] - start)
                with self.path(piece["path"]).open("rb") as source:
                    source.seek(piece["offset"] + start)
                    data = source.read(min(length + 3, piece["bytes"] - start))
                # Include a UTF-8 code point crossing the fixed page boundary;
                # the next page skips those continuation bytes.
                end = length
                while end < len(data) and data[end] & 0xC0 == 0x80:
                    end += 1
                begin = 0
                if start:
                    while begin < min(3, len(data)) and data[begin] & 0xC0 == 0x80:
                        begin += 1
                return ui.tags.pre(data[begin:end].decode("utf-8", errors="replace"))
            if piece["type"] == "image":
                if not piece.get("path"):
                    return ui.p("This image was not retained in the recording.")
                path = self.path(piece["path"])
                if path.stat().st_size > IMAGE_BYTES:
                    return ui.p(
                        "This retained image exceeds the viewer's 8 MiB display limit."
                    )
                mime_type = piece.get("mime_type", piece.get("mimeType"))
                if mime_type not in {
                    "image/png",
                    "image/jpeg",
                    "image/gif",
                    "image/webp",
                    "image/svg+xml",
                }:
                    return ui.p(
                        f"The retained image type {mime_type} is not supported by this viewer."
                    )
                data = base64.b64encode(path.read_bytes()).decode()
                image = ui.tags.img(
                    src=f"data:{mime_type};base64,{data}",
                    alt="Retained Console plot",
                    style="max-width:100%;height:auto;",
                )
                if piece.get("additional"):
                    return ui.TagList(
                        ui.p(
                            "Additional retained image. Its position relative to omitted response text is unavailable."
                        ),
                        image,
                    )
                return image
            return ui.p(f"Recorded {piece['type']} content has no viewer.")
        return ui.p(
            "This call has no retained text or images."
            if self.complete
            else "No retained output is available for this call yet. Refresh after output arrives."
        )


class RetainedOutput:
    def __init__(self) -> None:
        self.calls: dict[int, str] = {}

    def observe(self, metadata: dict[str, Any]) -> None:
        if metadata.get("call_id") is not None:
            self.calls[metadata["call_id"]] = metadata["execution"]


def retained_output_server(
    input: Inputs,
    session: Session,
    recording: Callable[[], Awaitable[dict[str, Any]]],
) -> RetainedOutput:
    viewer = RetainedOutput()
    selected: RecordedCall | None = None
    page_number = 0
    status = reactive.value("")
    content = reactive.value(ui.TagList())

    async def display() -> None:
        assert selected is not None
        try:
            page = await asyncio.to_thread(selected.page, page_number)
            content.set(page)
            label = (
                "Recorded bounded immediate response"
                if selected.immediate
                else "Recorded raw output"
            )
            details = f"{label} · Page {page_number + 1} of {selected.pages}."
            if selected.immediate:
                details += " Immediate text has no separate raw log; omitted response text cannot be recovered here."
            elif not selected.complete:
                details += " Recording may still grow; Refresh to read new output."
            if selected.discarded or selected.omitted_images:
                details += f" Recording omitted {selected.discarded} text bytes and {selected.omitted_images} images."
            status.set(details)
        except (OSError, ValueError) as error:
            content.set(ui.TagList())
            status.set(f"Retained output unavailable: {error}")
        ui.update_action_button(
            "retained_output_previous", disabled=page_number == 0, session=session
        )
        ui.update_action_button(
            "retained_output_next",
            disabled=page_number + 1 >= selected.pages,
            session=session,
        )

    @reactive.effect
    @reactive.event(input.retained_output_request)
    async def open_output() -> None:
        nonlocal selected, page_number
        selected = None
        page_number = 0
        status.set("Reading retained output…")
        content.set(ui.TagList())
        ui.modal_show(
            ui.modal(
                ui.output_text("retained_output_status"),
                ui.div(
                    ui.output_ui("retained_output_content"),
                    style="max-height:55vh;overflow:auto;overflow-wrap:anywhere;white-space:pre-wrap;",
                ),
                title="Retained output",
                size="l",
                easy_close=True,
                footer=ui.TagList(
                    ui.input_action_button("retained_output_previous", "Previous"),
                    ui.input_action_button("retained_output_next", "Next"),
                    ui.input_action_button("retained_output_refresh", "Refresh"),
                    ui.modal_button("Close"),
                ),
            ),
            session=session,
        )
        try:
            call_id = input.retained_output_request()["call_id"]
            if call_id not in viewer.calls:
                raise ValueError("This call is not part of the visible transcript.")
            paths = await recording()
            selected = RecordedCall(
                Path(paths["directory"]).resolve(),
                call_id,
                viewer.calls[call_id] == "immediate",
            )
            await asyncio.to_thread(selected.refresh)
            await display()
        except (OSError, RuntimeError, ValueError) as error:
            status.set(f"Retained output unavailable: {error}")
            ui.update_action_button(
                "retained_output_previous", disabled=True, session=session
            )
            ui.update_action_button(
                "retained_output_next", disabled=True, session=session
            )

    @reactive.effect
    @reactive.event(input.retained_output_previous)
    async def previous_page() -> None:
        nonlocal page_number
        if selected is not None:
            page_number = max(0, page_number - 1)
            await display()

    @reactive.effect
    @reactive.event(input.retained_output_next)
    async def next_page() -> None:
        nonlocal page_number
        if selected is not None:
            page_number = min(selected.pages - 1, page_number + 1)
            await display()

    @reactive.effect
    @reactive.event(input.retained_output_refresh)
    async def refresh_output() -> None:
        if selected is not None:
            try:
                await asyncio.to_thread(selected.refresh)
                await display()
            except (OSError, ValueError) as error:
                status.set(f"Retained output unavailable: {error}")

    @render.text
    def retained_output_status() -> str:
        return status.get()

    @render.ui
    def retained_output_content() -> ui.TagChild:
        return content.get()

    return viewer
