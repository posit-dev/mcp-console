"""Worker setup, receive-loop ownership, and language dispatch precedence."""

import io
import json
import os
import signal
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

from .control import configure_test_fixture_control, publish_marker
from .dispatch import dispatch
from .preparation import prepare_r
from .probes import configure_blocked_sideband, stall_sideband_reader
from .protocol import open_sideband, send, send_output
from .output import PNG_1X1
from .startup import configure_startup
from .state import LoopAction, WorkerContext


def main() -> None:
    """Run the deterministic sideband fixture until the server shuts it down."""
    reader, writer = open_sideband()
    context = WorkerContext(Path(tempfile.gettempdir()), reader, writer)
    configure_test_fixture_control(context)
    if started_marker := os.environ.get("MCP_CONSOLE_TEST_ZOD_STARTED"):
        publish_marker(Path(started_marker))

    def handle_sigint(_signum: int, _frame: object) -> None:
        context.received_sigint = True
        publish_marker(context.root / "zod-sigint-received")

    signal.signal(signal.SIGINT, handle_sigint)
    if os.environ.get("ZOD_REPORT_PID") == "1":
        publish_marker(context.root / "zod-worker-pid", str(os.getpid()))
    if os.environ.get("ZOD_REPORT_PROCESS_GROUP") == "1":
        publish_marker(
            context.root / "zod-process-group",
            str(os.getpgrp()),
        )
    startup_callback, startup_ready_sent = configure_startup(
        context.root,
        context.reader,
        context.writer,
    )
    if not startup_ready_sent:
        send(context.writer, {"kind": "ready"})
    context.recording_startup_pending = os.environ.get("ZOD_RECORDING_INTERVALS") == "1"
    if os.environ.get("ZOD_RECORDING_STARTUP") == "1":
        send_output(context.writer, "startup text\n")
        context.recording_startup_pending = False
    if startup_callback:
        send(
            context.writer,
            {
                "kind": "resolve_python_version",
                "request": {"constraints": [">=3.11"]},
            },
        )
        response = json.loads(context.reader.readline())
        assert response["kind"] == "python_version_resolution_failed", response
        publish_marker(
            context.root / "zod-startup-callback-response",
            response["message"],
        )
    configure_blocked_sideband(context)

    while True:
        if context.block_next_sideband_write is not None:
            stall_sideband_reader(context)
        if context.queued_message is None:
            line = context.reader.readline()
            if line == "":
                return
            message = json.loads(line)
        else:
            message = context.queued_message
            context.queued_message = None
        message_kind = message["kind"]
        if message_kind == "shutdown":
            if os.environ.get("ZOD_RECORDING_INTERVALS") == "1":
                send_output(context.writer, "shutdown text\n")
                send(
                    context.writer,
                    {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"},
                )
            return

        if message_kind == "prepare_r":
            prepare_r(context, message)
            continue

        assert message_kind == "evaluate"
        assert set(message) == {"kind", "language", "source"}
        if context.recording_startup_pending:
            # Evaluate follows the restart/admission response cuts. Emit startup
            # before cell entry, without racing those cuts against Ready.
            send_output(context.writer, "startup text\n")
            send(
                context.writer,
                {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"},
            )
            context.recording_startup_pending = False
        send(context.writer, {"kind": "cell_started"})
        language = message["language"]
        source = message["source"]
        if record := os.environ.get("MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS"):
            # Admission probes use real Python cells and record every evaluation.
            with Path(record).open("a", encoding="utf-8") as cells:
                cells.write(json.dumps(message) + "\n")
            assert language == "python", message
            with redirect_stdout(io.StringIO()) as output:
                exec(source, context.python_globals)
            if text := output.getvalue():
                send_output(context.writer, text)
            send(context.writer, {"kind": "completed"})
            continue
        if context.idle_input_received:
            assert (language, source) == ("r", "echo echo"), message
        if language in {"python", "sql"}:
            assert source.startswith("echo "), source
            payload = source.removeprefix("echo ")
            for output in (f"zod {language}: ", f"{payload}\n"):
                send_output(context.writer, output)
            send(context.writer, {"kind": "completed"})
            continue

        assert language == "r"
        if dispatch(context, source) is LoopAction.STOP:
            return
