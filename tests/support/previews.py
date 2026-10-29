"""Assertions on public previews and their separately retained raw files."""

import json
import re
from pathlib import Path

from support.client import McpClient
from support.evidence import compact_text
from support.progress import normalize_elapsed

TEXT_BUDGET = 8 * 1024
OMISSION = re.compile(r"\n\[output omitted: (\d+) UTF-8 bytes; [^\n]*\]\n")

CONTROL_OMISSION = re.compile(
    r"\[… omitted (\d+) rendered UTF-8 bytes; not retained …\]"
)


def assert_preview(
    text: str, emitted: str, *, pattern: re.Pattern[str] = OMISSION
) -> int:
    """Check the exact emitted prefix/suffix and accounting around one omission."""
    assert len(text.encode()) <= TEXT_BUDGET, len(text.encode())
    matches = list(pattern.finditer(text))
    assert len(matches) == 1, text
    marker = matches[0]
    head, tail = text[: marker.start()], text[marker.end() :]
    assert head and emitted.startswith(head), (head[:100], emitted[:100])
    assert tail and emitted.endswith(tail), (tail[-100:], emitted[-100:])
    omitted = int(marker[1])
    assert len(head.encode()) + omitted + len(tail.encode()) == len(emitted.encode()), (
        len(head.encode()),
        omitted,
        len(tail.encode()),
        len(emitted.encode()),
    )
    return omitted


def session_directory(client: McpClient) -> Path:
    assert client.temporary_directory is not None
    return next(
        (Path(client.temporary_directory.name) / ".agents/console/sessions").iterdir()
    )


def cell_text(client: McpClient, call_id: int) -> str:
    return (
        cell_output_path(session_directory(client), call_id)
        .read_bytes()
        .decode("utf-8", errors="replace")
    )


def normalize_preview_paths(client: McpClient) -> None:
    run_id = session_directory(client).name
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                block["text"] = block["text"].replace(run_id, "<run ID>")


def normalize_pipe_counts(client: McpClient) -> None:
    """Normalize variable pipe-fill counts after exact raw-file assertions."""
    normalize_preview_paths(client)
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text":
                text = block["text"]
                text = re.sub(
                    r"(output omitted: )\d+( UTF-8 bytes)",
                    r"\1<omitted byte count>\2",
                    text,
                )
                text = re.sub(
                    r"\d+( raw bytes (?:retained|observed))",
                    r"<raw byte count>\1",
                    text,
                )
                text = re.sub(
                    r"(zod expected [^\n]* tail: )\d+", r"\1<pipe tail bytes>", text
                )
                block["text"] = text


def compact_previews(client: McpClient, *units: str) -> None:
    """Represent selected repetitions only after public assertions and path normalization."""
    for entry in client.transcript:
        for block in entry.get("result", {}).get("content", []):
            if block["type"] == "text" and isinstance(block["text"], str):
                block["text"] = compact_text(normalize_elapsed(block["text"]), *units)


def recording_events(recording: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (recording / "capture/events.jsonl").read_text().splitlines()
    ]


def cell_output_path(recording: Path, call_id: int) -> Path:
    """Resolve public call/cell associations, including polls and replacements."""
    # Later delivery events may still be mid-append after a response arrives.
    # Read only through this cell's association, without decoding the live tail.
    with (recording / "capture/events.jsonl").open("rb") as journal:
        for line in journal:
            event = json.loads(line)
            if event["event"] == "cell_started" and event["call_id"] == call_id:
                owner = event["owner"]
                return (
                    recording
                    / f"capture/sessions/{owner['session_id']:03}/cell-{owner['cell_id']:03}.txt"
                )
    raise AssertionError(f"No recorded cell for call {call_id}")
