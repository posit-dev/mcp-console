#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    POLL_STDIN_RECEIVED_NAME,
    PRELUDE_PROCESSED_NAME,
    PRELUDE_RELEASE_NAME,
    ServerRelayClient,
)
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
@executions(DIRECT)
def test_withholding_attached_cell_preserves_idle_input(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        attached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "attached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        completed = resources.enter_context(
            closing(FifoCheckpoint.create(root / "completed"))
        )
        environment = os.environ.copy()
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "cell_attachment_checkpoint")
                ),
                "MCP_CONSOLE_TEST_CELL_ATTACHED": str(attached.path),
                "MCP_CONSOLE_TEST_CELL_RELEASE": str(release.path),
                "MCP_CONSOLE_TEST_CELL_COMPLETED": str(completed.path),
            }
        )
        client = ServerRelayClient(
            binary, "withheld_idle_input", environment, execution=execution
        )
        finished = False
        try:
            client.start_worker()
            prelude = resources.enter_context(
                closing(
                    FifoCheckpoint.attach(client.relay_root() / PRELUDE_RELEASE_NAME)
                )
            )
            processed = resources.enter_context(
                closing(
                    FifoCheckpoint.attach(client.relay_root() / PRELUDE_PROCESSED_NAME)
                )
            )
            received = resources.enter_context(
                closing(
                    FifoCheckpoint.attach(
                        client.relay_root() / POLL_STDIN_RECEIVED_NAME
                    )
                )
            )
            prelude.release()
            processed.wait("the idle input request reached the controller")
            assert (
                tool_text(client.send())
                == '[input requested: "idle> "]\n[waiting for stdin]'
            )
            pending = client.send(r="withheld cell", timeout_ms=0)
            assert pending["isError"] is False, pending
            attached.wait("the pending cell owns the idle callback input request")
            assert tool_text(client.send()) == "\n[waiting for stdin]"
            client.send(control="interrupt", timeout_ms=0)
            release.release()
            completed.wait("the withheld cell detached and closed its output")
            settled = client.send()
            assert tool_text(settled) == "[done]", settled
            reply = client.send(stdin="answer\n")
            assert reply["isError"] is False, reply
            try:
                received.wait("InputReceived was accepted without replacing the worker")
            except AssertionError as error:
                capture = client._read_capture(client._capture_path())
                raise AssertionError((reply, client.send(), capture)) from error
            assert (
                tool_text(client.send(r="fresh cell")) == "original worker survived\n"
            )
            transcript = client.finish_active()
            finished = True
            commands = [
                entry["server"] for entry in transcript if entry.keys() == {"server"}
            ]
            assert commands == [
                {
                    "kind": "python_version_resolution_failed",
                    "message": "Python requirements are unavailable with a custom worker",
                },
                {"kind": "interrupt", "request_id": 0},
                {"kind": "stdin", "data": "answer\n"},
                {
                    "kind": "python_version_resolution_failed",
                    "message": "Python requirements are unavailable with a custom worker",
                },
                {"kind": "evaluate", "language": "r", "source": "fresh cell"},
            ], commands
            return transcript
        finally:
            release.release()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()


if __name__ == "__main__":
    run_this_suite(__file__)
