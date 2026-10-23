"""Prompted, unprompted, idle, and direct-descriptor stdin scenarios."""

import os
import time

from .control import (
    emit_test_event,
    publish_marker,
    wait_for_test_control,
)
from .output import PENDING_TEXT_BUDGET
from .protocol import (
    send,
    send_batch,
    send_output,
    wait_for_server_to_process_sideband,
)
from .state import LoopAction, WorkerContext


def request_input(context: WorkerContext, source: str) -> None:
    send(context.writer, {"kind": "input_requested", "prompt": "zod> "})
    stdin = input()
    send(context.writer, {"kind": "input_received"})
    send_output(context.writer, f"zod stdin: {stdin}\n")
    send(context.writer, {"kind": "completed"})
    wait_for_server_to_process_sideband(context.reader, context.writer)
    publish_marker(context.root / "zod-prompted-input-processed")


def preview_prompt(context: WorkerContext, source: str) -> None:
    send_output(
        context.writer,
        "prompt output head\n"
        + "x" * (2 * PENDING_TEXT_BUDGET)
        + "\nprompt output tail\n",
    )
    send(
        context.writer,
        {
            "kind": "input_requested",
            "prompt": "prompt head " + "p" * 20000 + " prompt tail> ",
        },
    )
    wait_for_test_control(context, 0, "read_preview_input")
    stdin = input()
    send(context.writer, {"kind": "input_received"})
    send_output(context.writer, f"received {stdin}\n")
    send(context.writer, {"kind": "completed"})
    wait_for_server_to_process_sideband(context.reader, context.writer)
    emit_test_event(context, 0, "preview_input_processed")


def request_input_after_timeout(context: WorkerContext, source: str) -> None:
    waiting = context.root / "zod-waiting-to-request-input"
    publish_marker(waiting)
    while not (context.root / "zod-release-input-request").exists():
        time.sleep(0.01)
    send_output(context.writer, "before")
    send(context.writer, {"kind": "input_requested", "prompt": "late> "})
    send_output(context.writer, "during")
    send_output(context.writer, " request\n")
    stdin = input()
    send(context.writer, {"kind": "input_received"})
    send_output(context.writer, f"zod stdin: {stdin}\n")
    # Prove that the receipt has cleared the provisional request before
    # the client polls, so the fixed grace cannot add another boundary.
    wait_for_server_to_process_sideband(context.reader, context.writer)
    publish_marker(context.root / "zod-input-received")
    send(context.writer, {"kind": "completed"})


def input_without_request_then_request_input(
    context: WorkerContext, source: str
) -> None:
    first = input()
    send(context.writer, {"kind": "input_requested", "prompt": "second> "})
    second = input()
    send(context.writer, {"kind": "input_received"})
    send_output(context.writer, f"zod stdin: {first}|{second}\n")
    send(context.writer, {"kind": "completed"})
    wait_for_server_to_process_sideband(context.reader, context.writer)
    publish_marker(context.root / "zod-combined-input-processed")


def input_without_request(context: WorkerContext, source: str) -> None:
    stdin = input()
    output = (
        f"zod stdin: {stdin}\n"
        if source == "input without request"
        else f"zod stdin length: {len(stdin.encode())}\n"
    )
    send_output(context.writer, output)
    send(context.writer, {"kind": "completed"})


def read_fd_zero_directly(context: WorkerContext, source: str) -> None:
    chunks = []
    while not chunks or not chunks[-1].endswith(b"\n"):
        chunk = os.read(0, 3)
        assert chunk, "Zod received stdin EOF before a complete line"
        chunks.append(chunk)
    stdin = b"".join(chunks).decode()
    send_output(context.writer, f"zod fd 0: {stdin!r}\n")
    send(context.writer, {"kind": "completed"})


def request_input_while_idle(context: WorkerContext, source: str) -> LoopAction | None:
    release = context.root / "zod-release-idle-input-request"
    os.mkfifo(release)
    send(context.writer, {"kind": "completed"})
    publish_marker(context.root / "zod-idle-input-cell-completed")
    with release.open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    send(context.writer, {"kind": "input_requested", "prompt": "idle> "})
    wait_for_server_to_process_sideband(context.reader, context.writer)
    publish_marker(context.root / "zod-idle-input-request-processed")
    try:
        value = input()
    except EOFError:
        return LoopAction.STOP
    assert value == "continue"
    send_batch(
        context.writer,
        [{"kind": "input_received"}],
    )
    context.idle_input_received = True
