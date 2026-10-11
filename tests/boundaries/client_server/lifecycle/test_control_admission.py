#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import tool_text, wait_for_worker_ready
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PTHREAD_RUNTIME_PARKING,
    requires,
)
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_interrupt_following_cell_does_not_migrate_to_automatic_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "first-started",
                "interrupt-selected",
                "fault-release",
                "replacement-ready",
                "grace-reached",
                "grace-release",
            )
        }
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "interrupt_grace_checkpoint")),
            "MCP_CONSOLE_TEST_ADMISSION_ROOT": str(root),
            "MCP_CONSOLE_TEST_INTERRUPT_GRACE_REACHED": str(
                checkpoints["grace-reached"].path
            ),
            "MCP_CONSOLE_TEST_INTERRUPT_GRACE_RELEASE": str(
                checkpoints["grace-release"].path
            ),
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/interrupt_replacement.py"
        )
        writable = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        with McpClient(
            binary,
            execution.serve(*writable, "--worker", str(binary), "--relay", str(relay)),
            environment,
        ) as client:
            try:
                client.initialize_and_list_tools()
                wait_for_worker_ready(client, "configuration before replacement test")
                client.send(control="restart", timeout_ms=5_000)
                client.send(r="first cell", timeout_ms=0)
                checkpoints["first-started"].wait(
                    "the selected connection executes the first cell"
                )
                following = client.start_send(control="interrupt", r="must not migrate")
                checkpoints["grace-reached"].wait(
                    "signal dispatch has been acknowledged"
                )
                checkpoints["fault-release"].release()
                checkpoints["replacement-ready"].wait(
                    "automatic replacement has a different connection"
                )
                checkpoints["grace-release"].release()
                client.receive(following)
                result = following["result"]
                assert result.get("isError") is True, result
                text = "".join(
                    item["text"] for item in result["content"] if item["type"] == "text"
                )
                assert "cell was not run" in text, text
                assert "must not migrate\n" not in text, text
                # A rejected follow-up leaves the prior failure recoverable.
                prior = client.send()
                assert "selected worker failed" in "".join(
                    item["text"] for item in prior["content"] if item["type"] == "text"
                ), prior
                client.send(r="fresh cell")
                assert tool_text(client.transcript[-1]["result"]) == "fresh cell\n"
                return client.finish()
            finally:
                checkpoints["fault-release"].release()
                checkpoints["grace-release"].release()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_lost_interrupt_ack_does_not_signal_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "first-started",
                "interrupt-selected",
                "fault-release",
                "replacement-ready",
            )
        }
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "MCP_CONSOLE_TEST_ADMISSION_ROOT": str(root),
            "MCP_CONSOLE_TEST_ADMISSION_LOST_ACK": "1",
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/interrupt_replacement.py"
        )
        writable = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        with McpClient(
            binary,
            execution.serve(*writable, "--worker", str(binary), "--relay", str(relay)),
            environment,
        ) as client:
            try:
                client.initialize_and_list_tools()
                wait_for_worker_ready(
                    client, "configuration before lost acknowledgment test"
                )
                client.send(control="restart", timeout_ms=5_000)
                client.send(r="first cell", timeout_ms=0)
                checkpoints["first-started"].wait(
                    "the selected connection executes the first cell"
                )
                interrupt = client.start_send(control="interrupt")
                checkpoints["interrupt-selected"].wait(
                    "the old connection received interrupt"
                )
                checkpoints["fault-release"].release()
                checkpoints["replacement-ready"].wait(
                    "automatic replacement has a new connection"
                )
                client.receive(interrupt)
                assert interrupt["result"].get("isError") is True, interrupt
                client.send()
                client.send(r="fresh cell")
                assert tool_text(client.transcript[-1]["result"]) == "fresh cell\n"
                # The replacement fixture accepts only fresh Evaluate or Shutdown.
                # Any late/reselected interrupt makes its protocol assertion fail.
                return client.finish()
            finally:
                checkpoints["fault-release"].release()


@requires(NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
@executions(DIRECT)
def test_interrupt_before_first_resolver_registration_withholds_accepted_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("reached", "release", "shutdown")
        }
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(
                build_interposer(root, "startup_admission_checkpoint")
            ),
            "MCP_CONSOLE_TEST_ADMISSION_ORDINAL": "0",
            "MCP_CONSOLE_TEST_ADMISSION_REACHED": str(checkpoints["reached"].path),
            "MCP_CONSOLE_TEST_ADMISSION_RELEASE": str(checkpoints["release"].path),
            "MCP_CONSOLE_TEST_ADMISSION_SHUTDOWN": str(checkpoints["shutdown"].path),
        }
        with McpClient(binary, execution.serve(), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                checkpoints["reached"].wait(
                    "startup has not registered a resolver or worker"
                )
                accepted = client.send(python="discarded_cell = True", timeout_ms=0)
                assert not accepted.get("isError"), accepted
                assert tool_text(accepted).endswith(
                    "[running; poll with an empty send]"
                ), accepted
                interrupted = client.send(control="interrupt", timeout_ms=0)
                assert not interrupted.get("isError"), interrupted
                checkpoints["release"].release()
                settled = client.send()
                assert settled.get("isError") is True, settled
                assert "worker startup interrupted" in str(settled), settled
                client.send(control="restart", timeout_ms=600_000)
                client.send(python="assert 'discarded_cell' not in globals(); 42")
                assert tool_text(client.transcript[-1]["result"]) == "42\n"
                return client.finish()
            finally:
                checkpoints["release"].release()


if __name__ == "__main__":
    run_this_suite(__file__)
