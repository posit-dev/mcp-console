import json
import os
import selectors
import time
from pathlib import Path
from typing import TextIO

from support.records import Transcript


def read_jsonl(stream: TextIO) -> Transcript:
    return [json.loads(line) for line in stream.read().splitlines()]


def read_jsonl_path(path: Path) -> Transcript:
    with path.open(encoding="utf-8") as stream:
        return read_jsonl(stream)


def read_lines(
    stream: object,
    count: int,
    description: str,
    *,
    timeout: float | None = 10,
) -> list[str]:
    # Startup can use the enclosing case deadline instead of an I/O deadline.
    descriptor = stream.fileno()  # type: ignore[attr-defined]
    output = bytearray()
    newline_count = 0
    deadline = None if timeout is None else time.monotonic() + timeout
    with selectors.DefaultSelector() as selector:
        selector.register(descriptor, selectors.EVENT_READ)
        while newline_count < count:
            remaining = None if deadline is None else deadline - time.monotonic()
            assert remaining is None or remaining > 0, (
                f"timed out waiting for {description}"
            )
            ready = selector.select(remaining)
            assert ready, f"timed out waiting for {description}"
            chunk = os.read(descriptor, 4096)
            assert chunk, f"sandbox closed before reporting {description}"
            output.extend(chunk)
            newline_count += chunk.count(b"\n")
    lines = output.decode("utf-8").splitlines()
    assert len(lines) == count, (description, lines)
    return lines


def assert_worker_started(event: dict) -> None:
    assert set(event) == {"kind", "pid"}, event
    assert event["kind"] == "worker_started", event
    assert isinstance(event["pid"], int) and event["pid"] > 0, event


def read_relay_ready(stream: object) -> dict:
    started, ready = [
        json.loads(line)
        for line in read_lines(stream, 2, "worker launch and readiness")
    ]
    assert_worker_started(started)
    assert ready == {"kind": "ready"}, ready
    return ready
