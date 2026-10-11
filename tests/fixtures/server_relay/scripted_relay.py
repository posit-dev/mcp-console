#!/usr/bin/env python3

import base64
import json
import os
import select
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from generation_retirement import failed_during_resolver_callback

SCENARIO_ENV = "MCP_CONSOLE_TEST_RELAY_SCENARIO"
CAPTURE_NAME = "mcp-console-server-relay-wire.jsonl"
DONE_NAME = "mcp-console-scripted-relay-done"
EVALUATING_NAME = "mcp-console-scripted-relay-evaluating"
CHECKPOINT_NAME = "mcp-console-scripted-relay-checkpoint"
RELEASE_NAME = "mcp-console-scripted-relay-release"
STDIN_FAILURE_RELEASED_ENV = "MCP_CONSOLE_TEST_STDIN_FAILURE_RELEASED"
SHUTDOWN_RECEIVED_NAME = "mcp-console-scripted-relay-shutdown-received"
RETIREMENT_RELEASE_NAME = "mcp-console-scripted-relay-retirement-release"
PRELUDE_RELEASE_NAME = "mcp-console-scripted-relay-prelude-release"
PRELUDE_PROCESSED_NAME = "mcp-console-scripted-relay-prelude-processed"
EVALUATION_OUTPUT_READY_NAME = "mcp-console-scripted-relay-evaluation-output-ready"
PREPARATION_RECEIVED_NAME = "mcp-console-scripted-relay-preparation-received"
PREPARATION_RESULT_RELEASE_NAME = (
    "mcp-console-scripted-relay-preparation-result-release"
)
PREPARATION_RESULT_SENT_NAME = "mcp-console-scripted-relay-preparation-result-sent"
R_PREPARATION_RESOLVE_CHECKPOINT_NAME = "mcp-console-r-preparation-resolve-checkpoint"
R_PREPARATION_RESOLVE_RELEASE_NAME = "mcp-console-r-preparation-resolve-release"
IDLE_R_RESOLUTION_READY_NAME = "mcp-console-idle-r-resolution-ready"
IDLE_R_RESOLUTION_RELEASE_NAME = "mcp-console-idle-r-resolution-release"
IDLE_R_EVALUATION_RECEIVED_NAME = "mcp-console-idle-r-evaluation-received"
EXPLICIT_R_PREPARATION_CALLBACK_NAME = "mcp-console-explicit-r-preparation-callback"
EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME = (
    "mcp-console-explicit-r-preparation-callback-reply"
)
INTERRUPT_ACTIVE_RELEASE_NAME = "mcp-console-interrupt-active-release"
INTERRUPT_ACK_RELEASE_NAME = "mcp-console-interrupt-ack-release"
INTERRUPT_ACKNOWLEDGED_NAME = "mcp-console-interrupt-acknowledged"
INTERRUPT_RECEIVED_NAME = "mcp-console-interrupt-received"
POLL_STDIN_RECEIVED_NAME = "mcp-console-poll-stdin-received"
CONTROLLED_INTERRUPT_FIRST_RECEIVED_NAME = (
    "mcp-console-controlled-interrupt-first-received"
)
CONTROLLED_INTERRUPT_SECOND_RECEIVED_NAME = (
    "mcp-console-controlled-interrupt-second-received"
)
CONTROLLED_INTERRUPT_EVALUATION_RELEASE_NAME = (
    "mcp-console-controlled-interrupt-evaluation-release"
)
CONTROLLED_COMPLETION_RELEASE_NAME = "mcp-console-controlled-completion-release"
CONTROLLED_COMPLETION_SENT_NAME = "mcp-console-controlled-completion-sent"
RESTART_REQUIREMENTS_CHECK_NAME = "mcp-console-restart-requirements-check"
RESTART_REQUIREMENTS_CHECKED_NAME = "mcp-console-restart-requirements-checked"
RESTART_REQUIREMENTS_RESOLVED_NAME = "mcp-console-restart-requirements-resolved"
RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME = (
    "mcp-console-restart-requirements-evaluation-received"
)
RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME = (
    "mcp-console-restart-requirements-evaluation-release"
)
WAIT_SECONDS = 10

EVALUATION = {
    "kind": "evaluate",
    "language": "r",
    "source": "42",
}
COMPLETED = {"kind": "completed"}
RESOLVE_R = {
    "kind": "resolve_r",
    "packages": ["github::owner/repo"],
}
R_RESOLUTION_FAILED = {
    "kind": "r_resolution_failed",
    "failure": "host",
    "message": "automatic R package name `github::owner/repo` is not accepted: names must start with an ASCII letter, end with an ASCII letter or digit, and contain only ASCII letters, digits, and dots",
}
RESOLVE_PYTHON = {
    "kind": "resolve_python",
    "request": {
        "requirements": {"packages": ["numpy", "pandas"]},
        "retained_requirements": {"packages": ["numpy", "pandas"]},
    },
}
PYTHON_RESOLUTION_FAILED = {
    "kind": "python_resolution_failed",
    "message": "Python requirements are unavailable with a custom worker",
}
RESOLVE_PYTHON_VERSION = {
    "kind": "resolve_python_version",
    "request": {"constraints": [">=3.11"]},
}
PYTHON_VERSION_RESOLUTION_FAILED = {
    "kind": "python_version_resolution_failed",
    "message": "Python requirements are unavailable with a custom worker",
}
PENDING_TEXT_BUDGET = 8 * 1024 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


class ScriptedRelay:
    def __init__(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(
            prefix="scripted-relay-", dir=os.environ["TMPDIR"]
        )
        self.root = Path(self.temporary.name)
        self.capture = (self.root / CAPTURE_NAME).open("w", encoding="utf-8")
        self.checkpoints: dict[str, int] = {}

    def close(self) -> None:
        for checkpoint in self.checkpoints.values():
            os.close(checkpoint)
        self.capture.close()
        self.temporary.cleanup()

    def record(self, entry: dict[str, Any]) -> None:
        self.capture.write(json.dumps(entry, separators=(",", ":")) + "\n")
        self.capture.flush()

    def receive(self) -> dict[str, Any]:
        line = sys.stdin.buffer.readline()
        assert line, "server closed relay stdin before sending the expected command"
        assert line.endswith(b"\n"), "server command ended midway through a frame"
        message = json.loads(line)
        assert isinstance(message, dict), message
        self.record({"server": message})
        return message

    def expect(self, expected: dict[str, Any]) -> None:
        actual = self.receive()
        assert actual == expected, f"expected {expected!r}, received {actual!r}"

    def expect_no_command(self) -> None:
        readable, _, _ = select.select([sys.stdin.buffer], [], [], 0)
        if readable:
            unexpected = self.receive()
            raise AssertionError(f"received unexpected server command: {unexpected!r}")

    def send(self, message: dict[str, Any]) -> None:
        self.record({"relay": message})
        frame = json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        write_all(1, frame)

    def send_batch(self, messages: list[dict[str, Any]]) -> None:
        frames = []
        for message in messages:
            self.record({"relay": message})
            frames.append(
                json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
            )
        write_all(1, b"".join(frames))

    def mark_done(self) -> None:
        (self.root / DONE_NAME).touch()

    def make_checkpoint(self, name: str) -> None:
        path = self.root / name
        os.mkfifo(path)
        self.checkpoints[name] = os.open(path, os.O_RDWR)

    def notify_checkpoint(self, name: str) -> None:
        write_all(self.checkpoints[name], b"1")

    def wait_for_checkpoint(self, name: str) -> None:
        assert os.read(self.checkpoints[name], 1) == b"1"

    def wait_for_release(self) -> None:
        (self.root / CHECKPOINT_NAME).touch()
        release = self.root / RELEASE_NAME
        deadline = time.monotonic() + WAIT_SECONDS
        while not release.exists():
            assert time.monotonic() < deadline, "test did not release scripted relay"
            time.sleep(0.01)

    def ready(self) -> None:
        self.send({"kind": "ready"})

    def complete(self) -> None:
        self.send(COMPLETED)
        self.mark_done()

    def retire(
        self,
        command: dict[str, Any] | None = None,
        before_close: dict[str, Any] | None = None,
    ) -> None:
        if command is None:
            command = self.receive()
        assert command.get("kind") == "shutdown", command
        grace_millis = command.get("grace_millis")
        assert isinstance(grace_millis, int) and 0 <= grace_millis <= 1_000, command
        self.send({"kind": "shutdown_started"})
        if before_close is not None:
            self.send(before_close)
            if release := os.environ.get("MCP_CONSOLE_TEST_RELAY_EXIT_RELEASE"):
                with open(release, "rb", buffering=0) as checkpoint:
                    assert checkpoint.read(1) == b"1"
        self.send({"kind": "stdout_closed"})
        self.send({"kind": "stderr_closed"})
        self.send({"kind": "worker_sideband_closed"})
        self.send({"kind": "worker_exited", "code": 0})

    def unexpected_outcome(
        self,
        outcome: dict[str, Any],
        output: list[dict[str, Any]] | None = None,
    ) -> None:
        self.ready()
        command = self.receive()
        if command.get("kind") == "shutdown":
            self.retire(command)
            return

        assert command == EVALUATION, command
        self.wait_for_release()
        self.send_batch(
            [
                *(output or []),
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                outcome,
            ]
        )


def write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining) :]


def run_ready(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.mark_done()
    relay.retire()


def run_evaluate(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.complete()
    relay.retire()


def run_raw_output(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.send(
        {
            "kind": "stdout",
            "data": "stdout text 👩🏽‍💻\n",
        }
    )
    relay.send(
        {
            "kind": "stderr",
            "data": "stderr text\n",
        }
    )
    relay.send(
        {
            "kind": "stdout_bytes",
            "data": base64.b64encode(b"\xffstdout bytes\n").decode("ascii"),
        }
    )
    relay.send(
        {
            "kind": "stderr_bytes",
            "data": base64.b64encode(b"\xfestderr bytes\n").decode("ascii"),
        }
    )
    relay.complete()
    relay.retire()


def run_split_terminal_redraws(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    for chunk in (
        "ordinary stdout\r\r\n",
        "old\r\b\n",
        "old\r\b",
        "newx\b\n",
        "old\x1b[",
        "2Knew\n",
    ):
        relay.send({"kind": "stdout", "data": chunk})
    relay.complete()
    relay.retire()


def run_independent_stream_redraws(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    for stream in ("stdout", "stderr"):
        relay.send({"kind": stream, "data": f"\r{stream} old"})
        relay.send({"kind": stream, "data": f"\r{stream} final\n"})
    relay.complete()
    relay.retire()


def run_interleaved_stream_redraws(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.send_batch(
        [
            {"kind": "stderr", "data": "\rstderr old"},
            {"kind": "stdout", "data": "\rstdout final\n"},
            {"kind": "stderr", "data": "\rstderr final\n"},
        ]
    )
    relay.complete()
    relay.retire()


def run_raw_malformed_redraw(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.send(
        {
            "kind": "stdout_bytes",
            "data": base64.b64encode(b"old\r").decode("ascii"),
        }
    )
    relay.send(
        {
            "kind": "stdout_bytes",
            "data": base64.b64encode(b"\xff\n").decode("ascii"),
        }
    )
    relay.complete()
    relay.retire()


def run_empty_raw_close_between_redraws(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.send_batch(
        [
            {"kind": "console_output", "data": "old\r"},
            {"kind": "stdout_closed"},
            {"kind": "console_output", "data": "new\n"},
        ]
    )
    relay.complete()

    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send_batch(
        [
            {"kind": "shutdown_started"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ]
    )


def run_raw_close_between_utf8_fragments(relay: ScriptedRelay) -> None:
    closed = (
        "stdout"
        if os.environ[SCENARIO_ENV] == "stdout_close_between_utf8_fragments"
        else "stderr"
    )
    pending = "stderr" if closed == "stdout" else "stdout"
    relay.ready()
    relay.expect(EVALUATION)
    relay.send_batch(
        [
            {
                "kind": f"{pending}_bytes",
                "data": base64.b64encode(b"\xe2").decode("ascii"),
            },
            {"kind": f"{closed}_closed"},
            {
                "kind": f"{pending}_bytes",
                "data": base64.b64encode(b"\x82\xac\n").decode("ascii"),
            },
        ]
    )
    relay.complete()
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send_batch(
        [
            {"kind": "shutdown_started"},
            {"kind": f"{pending}_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ]
    )


def run_stdin(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect({"kind": "stdin", "data": "answer\n"})
    relay.expect(EVALUATION)
    relay.complete()
    relay.retire()


def run_initial_requirements_stdin_idempotent(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect({"kind": "stdin", "data": "answer\n"})
    relay.expect(
        {
            "kind": "evaluate",
            "language": "python",
            "source": "42",
        }
    )
    relay.send(COMPLETED)
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "43",
        }
    )
    relay.complete()
    relay.retire()


def run_live_r_requirements_then_evaluate(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(IDLE_R_EVALUATION_RECEIVED_NAME)
    relay.make_checkpoint(RELEASE_NAME)
    relay.ready()
    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    relay.send({"kind": "r_prepared", "library": command["library"]})
    relay.expect(EVALUATION)
    relay.notify_checkpoint(IDLE_R_EVALUATION_RECEIVED_NAME)
    relay.wait_for_checkpoint(RELEASE_NAME)
    relay.complete()
    relay.retire()


def run_stdin_forwarding_failure(relay: ScriptedRelay) -> None:
    if Path(os.environ[STDIN_FAILURE_RELEASED_ENV]).exists():
        relay.ready()
        relay.expect(EVALUATION)
        relay.complete()
        relay.retire()
        return

    relay.ready()
    readable, _, _ = select.select([sys.stdin.buffer], [], [], WAIT_SECONDS)
    assert readable, "server sent no command to scripted relay"

    prefix = bytearray()
    while b'"kind":"' not in prefix or len(prefix) < 64:
        chunk = sys.stdin.buffer.read1(64 - len(prefix))
        assert chunk, "server command ended before its kind was readable"
        prefix.extend(chunk)
        if b"\n" in prefix:
            command = json.loads(prefix)
            relay.record({"server": command})
            relay.retire(command)
            return

    assert prefix.startswith(b'{"kind":"stdin","data":"'), prefix
    relay.record({"server_raw": base64.b64encode(prefix).decode("ascii")})
    relay.wait_for_release()
    signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGTERM})
    os.close(0)
    relay.mark_done()
    assert signal.sigwait({signal.SIGTERM}) == signal.SIGTERM


def run_interrupt(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.complete()
    relay.retire()


def run_withheld_idle_input(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(PRELUDE_RELEASE_NAME)
    relay.make_checkpoint(PRELUDE_PROCESSED_NAME)
    relay.make_checkpoint(POLL_STDIN_RECEIVED_NAME)
    relay.ready()
    relay.wait_for_checkpoint(PRELUDE_RELEASE_NAME)
    relay.send({"kind": "input_requested", "prompt": "idle> "})
    relay.send(RESOLVE_PYTHON_VERSION)
    relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
    relay.notify_checkpoint(PRELUDE_PROCESSED_NAME)

    # The idle callback ignores interruption and keeps its live input request.
    interrupt = relay.receive()
    assert interrupt == {"kind": "interrupt", "request_id": 0}, interrupt
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect({"kind": "stdin", "data": "answer\n"})
    relay.send({"kind": "input_received"})
    relay.send(RESOLVE_PYTHON_VERSION)
    relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
    relay.notify_checkpoint(POLL_STDIN_RECEIVED_NAME)

    relay.expect({"kind": "evaluate", "language": "r", "source": "fresh cell"})
    relay.send({"kind": "console_output", "data": "original worker survived\n"})
    relay.complete()
    relay.retire()


def run_controlled_restart_stdin(relay: ScriptedRelay) -> None:
    relay.ready()
    command = relay.receive()
    if command == {"kind": "stdin", "data": "stale\n"}:
        relay.retire()
        return

    assert command == {"kind": "stdin", "data": "fresh\n"}, command
    captured_stdin = command["data"]
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement cell",
        }
    )
    relay.send(
        {
            "kind": "console_output",
            "data": f"replacement cell consumed stdin: {captured_stdin}",
        }
    )
    relay.complete()
    relay.retire()


def run_controlled_restart_stdin_only(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect({"kind": "stdin", "data": "replacement input\n"})
    relay.mark_done()
    relay.retire()


def run_controlled_restart_requirements(relay: ScriptedRelay) -> None:
    counter = Path(os.environ["MCP_CONSOLE_TEST_IR_COUNTER"])
    if not counter.exists():
        relay.make_checkpoint(RESTART_REQUIREMENTS_CHECK_NAME)
        relay.make_checkpoint(RESTART_REQUIREMENTS_CHECKED_NAME)
        relay.make_checkpoint(RESTART_REQUIREMENTS_RESOLVED_NAME)
        relay.ready()
        relay.wait_for_checkpoint(RESTART_REQUIREMENTS_CHECK_NAME)
        relay.expect_no_command()
        relay.notify_checkpoint(RESTART_REQUIREMENTS_CHECKED_NAME)
        readable, _, _ = select.select(
            [
                relay.checkpoints[RESTART_REQUIREMENTS_RESOLVED_NAME],
                sys.stdin.buffer,
            ],
            [],
            [],
            WAIT_SECONDS,
        )
        assert relay.checkpoints[RESTART_REQUIREMENTS_RESOLVED_NAME] in readable, (
            "old worker received shutdown before restart requirements resolved"
        )
        relay.wait_for_checkpoint(RESTART_REQUIREMENTS_RESOLVED_NAME)
        relay.retire()
        return

    # The received checkpoint publishes that both sides of the gate are ready.
    relay.make_checkpoint(RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME)
    relay.make_checkpoint(RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME)
    relay.ready()
    relay.expect({"kind": "stdin", "data": "replacement requirement input\n"})
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement requirement cell",
        }
    )
    relay.notify_checkpoint(RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME)
    relay.wait_for_checkpoint(RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "replacement requirement cell ran\n",
        }
    )
    relay.complete()
    relay.retire()


def run_controlled_interrupt_stdin_requirements_evaluate(
    relay: ScriptedRelay,
) -> None:
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before successful requirements",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect(
        {
            "kind": "stdin",
            "data": "finish old before successful preparation\n",
        }
    )
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation settled before successful preparation\n",
        }
    )
    relay.send(COMPLETED)
    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    relay.send({"kind": "r_prepared", "library": command["library"]})
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "new evaluation after successful preparation",
        }
    )
    relay.send(
        {
            "kind": "console_output",
            "data": "new evaluation ran after successful preparation\n",
        }
    )
    relay.complete()
    relay.retire()


def run_controlled_interrupt_stdin_evaluate(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect({"kind": "stdin", "data": "finish old\n"})
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation finished from stdin\n",
        }
    )
    relay.send(COMPLETED)
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "new evaluation",
        }
    )
    relay.send({"kind": "console_output", "data": "new evaluation ran\n"})
    relay.complete()
    relay.retire()


def run_controlled_interrupt_stdin_requirement_failure(
    relay: ScriptedRelay,
) -> None:
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before failing requirements",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect({"kind": "stdin", "data": "finish old before preparation\n"})
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation finished before preparation\n",
        }
    )
    relay.send(COMPLETED)
    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    relay.send(
        {
            "kind": "r_preparation_failed",
            "message": "scripted controlled interrupt R preparation failed",
        }
    )
    relay.mark_done()
    relay.retire()


def run_controlled_interrupt_stdin_invalid_requirements(
    relay: ScriptedRelay,
) -> None:
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before invalid requirements",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect({"kind": "stdin", "data": "finish old before validation\n"})
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation finished before validation\n",
        }
    )
    relay.send(COMPLETED)
    relay.mark_done()
    relay.retire()


def run_controlled_and_standalone_interrupts(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(CONTROLLED_INTERRUPT_FIRST_RECEIVED_NAME)
    relay.make_checkpoint(CONTROLLED_INTERRUPT_SECOND_RECEIVED_NAME)
    relay.make_checkpoint(CONTROLLED_INTERRUPT_EVALUATION_RELEASE_NAME)
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation for overlapping interrupts",
        }
    )
    (relay.root / EVALUATING_NAME).touch()

    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.notify_checkpoint(CONTROLLED_INTERRUPT_FIRST_RECEIVED_NAME)
    relay.expect({"kind": "interrupt", "request_id": 1})
    relay.notify_checkpoint(CONTROLLED_INTERRUPT_SECOND_RECEIVED_NAME)
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 1})

    relay.wait_for_checkpoint(CONTROLLED_INTERRUPT_EVALUATION_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation finished after both interrupts\n",
        }
    )
    relay.complete()
    relay.retire()


def run_controlled_interrupt_still_active(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(INTERRUPT_ACTIVE_RELEASE_NAME)
    relay.make_checkpoint(INTERRUPT_ACKNOWLEDGED_NAME)
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation remains active\n",
        }
    )
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.notify_checkpoint(INTERRUPT_ACKNOWLEDGED_NAME)
    relay.wait_for_checkpoint(INTERRUPT_ACTIVE_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "old evaluation eventually finished\n",
        }
    )
    relay.complete()
    relay.retire()


def run_controlled_completion_then_interrupt(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(CONTROLLED_COMPLETION_RELEASE_NAME)
    relay.make_checkpoint(CONTROLLED_COMPLETION_SENT_NAME)
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "controlled cell completed before later interrupt",
        }
    )
    relay.wait_for_checkpoint(CONTROLLED_COMPLETION_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "controlled cell completed before later interrupt\n",
        }
    )
    relay.send(COMPLETED)
    relay.notify_checkpoint(CONTROLLED_COMPLETION_SENT_NAME)
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.mark_done()
    relay.retire()


def run_controlled_interrupt_with_waiting_poll(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(POLL_STDIN_RECEIVED_NAME)
    relay.make_checkpoint(INTERRUPT_RECEIVED_NAME)
    relay.make_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.make_checkpoint(INTERRUPT_ACTIVE_RELEASE_NAME)
    relay.ready()
    relay.expect(
        {
            "kind": "evaluate",
            "language": "r",
            "source": "waiter-owned evaluation",
        }
    )
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "stdin", "data": "poll ownership checkpoint\n"})
    relay.notify_checkpoint(POLL_STDIN_RECEIVED_NAME)
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.notify_checkpoint(INTERRUPT_RECEIVED_NAME)
    relay.wait_for_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "output owned by original waiter\n",
        }
    )
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.wait_for_checkpoint(INTERRUPT_ACTIVE_RELEASE_NAME)
    relay.send(
        {
            "kind": "console_output",
            "data": "original waiter evaluation finished\n",
        }
    )
    relay.complete()
    relay.retire()


def run_completion_before_interrupt_ack(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(POLL_STDIN_RECEIVED_NAME)
    relay.make_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.ready()
    relay.expect({"kind": "evaluate", "language": "r", "source": "first cell"})
    (relay.root / EVALUATING_NAME).touch()
    relay.expect({"kind": "stdin", "data": "waiter owns first response\n"})
    relay.notify_checkpoint(POLL_STDIN_RECEIVED_NAME)
    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.send({"kind": "console_output", "data": "first cell completed\n"})
    relay.send(COMPLETED)
    relay.wait_for_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.send({"kind": "interrupt_result", "request_id": 0})
    relay.expect({"kind": "evaluate", "language": "r", "source": "following cell"})
    relay.send({"kind": "console_output", "data": "following cell completed\n"})
    relay.complete()
    relay.retire()


def run_cancelled_interrupt_during_live_r_preparation(
    relay: ScriptedRelay,
) -> None:
    relay.make_checkpoint(PREPARATION_RECEIVED_NAME)
    relay.make_checkpoint(PREPARATION_RESULT_RELEASE_NAME)
    relay.make_checkpoint(PREPARATION_RESULT_SENT_NAME)
    relay.make_checkpoint(INTERRUPT_RECEIVED_NAME)
    relay.make_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.ready()

    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    library = command["library"]
    relay.notify_checkpoint(PREPARATION_RECEIVED_NAME)

    relay.expect({"kind": "interrupt", "request_id": 0})
    relay.notify_checkpoint(INTERRUPT_RECEIVED_NAME)
    relay.wait_for_checkpoint(INTERRUPT_ACK_RELEASE_NAME)
    relay.send({"kind": "interrupt_result", "request_id": 0})

    relay.wait_for_checkpoint(PREPARATION_RESULT_RELEASE_NAME)
    relay.send({"kind": "r_prepared", "library": library})
    relay.notify_checkpoint(PREPARATION_RESULT_SENT_NAME)
    relay.mark_done()
    relay.retire()


def run_serialized_cross_source_order(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    relay.send_batch(
        [
            {"kind": "stdout", "data": "stdout before completion\n"},
            {"kind": "stderr", "data": "stderr before completion\n"},
            COMPLETED,
            {"kind": "stdout", "data": "stdout after completion\n"},
            {"kind": "stderr", "data": "stderr after completion\n"},
            {
                "kind": "console_output",
                "data": "idle callback after completion\n",
            },
        ]
    )
    relay.wait_for_release()
    relay.send({"kind": "stdout", "data": "stdout after grace\n"})
    relay.send(RESOLVE_R)
    relay.expect(R_RESOLUTION_FAILED)
    relay.send(RESOLVE_PYTHON)
    relay.expect(PYTHON_RESOLUTION_FAILED)
    relay.send(RESOLVE_PYTHON_VERSION)
    relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
    relay.mark_done()
    relay.retire()


def run_shutdown(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.mark_done()
    relay.retire()


def run_shutdown_nonzero(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.retire()
    raise SystemExit(73)


def run_shutdown_status_137(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.retire()
    raise SystemExit(137)


def run_shutdown_nonzero_after_output(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.retire(
        before_close={
            "kind": "console_output",
            "data": "old generation retirement output\n",
        }
    )
    raise SystemExit(int(os.environ.get("MCP_CONSOLE_TEST_RELAY_EXIT_STATUS", "73")))


def run_blocked_live_r_resolver_shutdown(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.make_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.ready()
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.notify_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.wait_for_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.expect_no_command()
    relay.retire(command)
    relay.expect_no_command()


def run_late_r_prepared_retirement(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.make_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.make_checkpoint(PREPARATION_RECEIVED_NAME)
    relay.ready()
    command = relay.receive()
    if command.get("kind") == "shutdown":
        relay.retire(command)
        return

    assert command.get("kind") == "prepare_r", command
    library = command["library"]
    relay.notify_checkpoint(PREPARATION_RECEIVED_NAME)
    if library.endswith("candidate-two"):
        relay.send({"kind": "r_prepared", "library": library})
        relay.retire()
        return

    assert library.endswith("candidate-one"), library
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send({"kind": "shutdown_started"})
    relay.notify_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.wait_for_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.send_batch(
        [
            {"kind": "r_prepared", "library": library},
            {"kind": "stdout", "data": "drained old stdout\n"},
            {"kind": "stderr", "data": "drained old stderr\n"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 33},
        ]
    )


def run_pre_marker_r_prepared_replacement(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.make_checkpoint(PREPARATION_RECEIVED_NAME)
    relay.make_checkpoint(PREPARATION_RESULT_RELEASE_NAME)
    relay.make_checkpoint(PREPARATION_RESULT_SENT_NAME)
    relay.ready()
    command = relay.receive()
    if command.get("kind") == "shutdown":
        relay.retire(command)
        return

    assert command.get("kind") == "prepare_r", command
    library = command["library"]
    if library.endswith("candidate-three"):
        relay.send({"kind": "r_prepared", "library": library})
        relay.retire()
        return

    assert library.endswith("candidate-one"), library
    relay.notify_checkpoint(PREPARATION_RECEIVED_NAME)
    relay.wait_for_checkpoint(PREPARATION_RESULT_RELEASE_NAME)
    relay.send({"kind": "r_prepared", "library": library})
    relay.notify_checkpoint(PREPARATION_RESULT_SENT_NAME)
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send({"kind": "shutdown_started"})
    relay.notify_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.send({"kind": "stdout_closed"})
    relay.send({"kind": "stderr_closed"})
    relay.send({"kind": "worker_sideband_closed"})
    relay.send({"kind": "worker_exited", "code": 0})


def run_r_preparation_failure(relay: ScriptedRelay) -> None:
    relay.ready()
    command = relay.receive()
    if command.get("kind") == "shutdown":
        relay.retire(command)
        return

    assert command.get("kind") == "prepare_r", command
    relay.send(
        {
            "kind": "r_preparation_failed",
            "message": "scripted R preparation failed",
        }
    )
    relay.expect(EVALUATION)
    relay.complete()
    relay.retire()


def run_r_resolution_during_r_preparation(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(R_PREPARATION_RESOLVE_CHECKPOINT_NAME)
    relay.make_checkpoint(R_PREPARATION_RESOLVE_RELEASE_NAME)
    relay.ready()
    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    relay.send({"kind": "resolve_r", "packages": ["praise"]})
    relay.notify_checkpoint(R_PREPARATION_RESOLVE_CHECKPOINT_NAME)
    relay.wait_for_checkpoint(R_PREPARATION_RESOLVE_RELEASE_NAME)
    command = relay.receive()
    assert command.get("kind") == "shutdown", (
        f"R preparation resolver callback received a reply: {command!r}"
    )
    relay.retire(command)


def run_idle_r_resolution_owns_environment(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(IDLE_R_RESOLUTION_READY_NAME)
    relay.make_checkpoint(IDLE_R_RESOLUTION_RELEASE_NAME)
    relay.make_checkpoint(IDLE_R_EVALUATION_RECEIVED_NAME)
    relay.ready()
    request = {"kind": "resolve_r", "packages": ["praise"]}
    relay.send(request)
    resolved = relay.receive()
    assert resolved.get("kind") == "r_resolved", resolved
    library = resolved["library"]
    relay.notify_checkpoint(IDLE_R_RESOLUTION_READY_NAME)
    relay.expect(EVALUATION)
    relay.notify_checkpoint(IDLE_R_EVALUATION_RECEIVED_NAME)
    relay.wait_for_checkpoint(IDLE_R_RESOLUTION_RELEASE_NAME)
    relay.send({"kind": "r_activated", "library": library})
    relay.send(request)
    relay.expect({"kind": "r_resolved", "library": library})
    relay.send({"kind": "r_activated", "library": library})
    relay.complete()
    relay.retire()


def run_explicit_r_preparation_owns_environment(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(EXPLICIT_R_PREPARATION_CALLBACK_NAME)
    relay.make_checkpoint(EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME)
    relay.ready()
    relay.wait_for_checkpoint(EXPLICIT_R_PREPARATION_CALLBACK_NAME)
    relay.send({"kind": "resolve_r", "packages": ["praise"]})
    relay.expect(
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "R package resolution is unavailable during requirement preparation"
            ),
        }
    )
    relay.notify_checkpoint(EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME)
    command = relay.receive()
    assert command.get("kind") == "prepare_r", command
    library = command["library"]
    relay.send_batch(
        [
            {"kind": "r_prepared", "library": library},
            {"kind": "resolve_r", "packages": ["english"]},
        ]
    )
    relay.expect({"kind": "r_resolved", "library": library})
    relay.send({"kind": "r_activated", "library": library})
    relay.notify_checkpoint(EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME)
    relay.expect(EVALUATION)
    relay.complete()
    relay.retire()


def run_completion_before_r_activation(relay: ScriptedRelay) -> None:
    relay.ready()
    command = relay.receive()
    if command.get("kind") == "shutdown":
        relay.retire(command)
        return
    assert command == EVALUATION, command
    relay.send({"kind": "resolve_r", "packages": ["praise"]})
    resolved = relay.receive()
    assert resolved.get("kind") == "r_resolved", resolved
    relay.send(COMPLETED)
    relay.retire()


def run_cancelled_waiting_send(relay: ScriptedRelay) -> None:
    relay.make_checkpoint(PRELUDE_RELEASE_NAME)
    relay.make_checkpoint(PRELUDE_PROCESSED_NAME)
    relay.make_checkpoint(EVALUATION_OUTPUT_READY_NAME)
    relay.make_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.make_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.ready()

    readable, _, _ = select.select(
        [relay.checkpoints[PRELUDE_RELEASE_NAME], sys.stdin.buffer],
        [],
        [],
        WAIT_SECONDS,
    )
    assert readable, "test neither released the prelude nor shut down the relay"
    if sys.stdin.buffer in readable:
        relay.retire(relay.receive())
        return
    relay.wait_for_checkpoint(PRELUDE_RELEASE_NAME)

    relay.send_batch(
        [
            {"kind": "console_output", "data": "idle before image\n"},
            {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"},
            {"kind": "console_diagnostic", "data": "idle after image\n"},
            RESOLVE_PYTHON,
        ]
    )
    relay.expect(PYTHON_RESOLUTION_FAILED)
    relay.notify_checkpoint(PRELUDE_PROCESSED_NAME)

    relay.expect(EVALUATION)
    cell_prefix = "cell before image\n"
    relay.send_batch(
        [
            {"kind": "console_output", "data": cell_prefix},
            {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"},
            {
                "kind": "console_output",
                "data": "x" * (PENDING_TEXT_BUDGET + 7),
            },
            RESOLVE_PYTHON,
        ]
    )
    relay.expect(PYTHON_RESOLUTION_FAILED)
    relay.notify_checkpoint(EVALUATION_OUTPUT_READY_NAME)

    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send({"kind": "shutdown_started"})
    relay.notify_checkpoint(SHUTDOWN_RECEIVED_NAME)
    relay.wait_for_checkpoint(RETIREMENT_RELEASE_NAME)
    relay.send_batch(
        [
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ]
    )


def run_fatal(relay: ScriptedRelay) -> None:
    relay.wait_for_release()
    relay.send({"kind": "fatal", "message": "scripted relay failure"})
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send({"kind": "shutdown_started"})
    relay.send_batch(
        [
            {"kind": "stdout", "data": "drained after fatal failure\n"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 86},
        ]
    )


def run_fatal_exit(relay: ScriptedRelay) -> None:
    relay.ready()
    command = relay.receive()
    if command.get("kind") == "shutdown":
        relay.retire(command)
        return
    assert command == EVALUATION, command
    relay.wait_for_release()
    relay.send({"kind": "fatal", "message": "scripted relay failure"})
    command = relay.receive()
    assert command.get("kind") == "shutdown", command
    relay.send({"kind": "shutdown_started"})
    if os.environ[SCENARIO_ENV] == "fatal_sigterm":
        os.kill(os.getpid(), signal.SIGTERM)
    raise SystemExit(137)


def run_truncated(relay: ScriptedRelay) -> None:
    relay.wait_for_release()
    raw = b'{"kind":"console_output"'
    relay.record({"relay_raw": base64.b64encode(raw).decode("ascii")})
    write_all(1, raw)


def run_exit_zero(relay: ScriptedRelay) -> None:
    relay.unexpected_outcome({"kind": "worker_exited", "code": 0})


def run_exit_nonzero(relay: ScriptedRelay) -> None:
    relay.unexpected_outcome(
        {"kind": "worker_exited", "code": 33},
        [
            {"kind": "stdout", "data": "drained stdout\n"},
            {
                "kind": "stderr_bytes",
                "data": base64.b64encode(b"\xffdrained stderr\n").decode("ascii"),
            },
        ],
    )


def run_signaled(relay: ScriptedRelay) -> None:
    relay.unexpected_outcome({"kind": "worker_signaled", "signal": 15})


def run_startup_output(relay: ScriptedRelay) -> None:
    relay.send({"kind": "stdout", "data": "s" * (PENDING_TEXT_BUDGET + 7)})
    relay.send({"kind": "ready"})
    relay.expect(EVALUATION)
    relay.send({"kind": "console_output", "data": "cell output\n"})
    relay.send(COMPLETED)
    relay.retire()


def run_preview_raw_prelude(relay: ScriptedRelay) -> None:
    raw = b"idle head\n" + b"s" * 20000 + b"\xe2"
    relay.send({"kind": "stdout_bytes", "data": base64.b64encode(raw).decode("ascii")})
    relay.ready()
    relay.expect(EVALUATION)
    relay.send(
        {
            "kind": "console_output",
            "data": "cell head\n" + "x" * 32768 + "\ncell tail\n",
        }
    )
    relay.complete()
    relay.retire()


def run_preview_raw(relay: ScriptedRelay) -> None:
    relay.ready()
    relay.expect(EVALUATION)
    for chunk in (
        b"raw head\n\xe2",
        b"\x82\xac",
        b"\xff" * 20000,
        b"\xe2",
        b"\x82\xac raw tail\n",
    ):
        relay.send(
            {"kind": "stdout_bytes", "data": base64.b64encode(chunk).decode("ascii")}
        )
    relay.complete()
    relay.retire()


def run_preview_direct_allocations(relay: ScriptedRelay) -> None:
    relay.ready()
    while True:
        command = relay.receive()
        if command["kind"] == "shutdown":
            relay.retire(command)
            return
        assert command["kind"] == "evaluate", command
        kind = command["source"]
        assert kind in {"console_output", "stdout"}, command
        for _ in range(512):
            relay.send({"kind": kind, "data": "ab" * 8192})
        relay.send({"kind": kind, "data": "\nfinal diagnostic\n"})
        relay.complete()


def run_partial_utf8_polls(relay: ScriptedRelay) -> None:
    directory = Path(os.environ["MCP_CONSOLE_TEST_PREVIEW_DIRECTORY"])
    release = directory / "partial-release"
    processed = directory / "partial-processed"
    relay.ready()
    relay.expect(EVALUATION)
    for stream, data in (
        ("stdout", b"A\xe2"),
        ("stdout", b"\x82\xacB\xe2"),
        ("stderr", b"C\xf0\x9f"),
        ("stdout", b" D\xe2"),
    ):
        with release.open("rb", buffering=0) as checkpoint:
            assert checkpoint.read(1) == b"1"
        relay.send(
            {"kind": f"{stream}_bytes", "data": base64.b64encode(data).decode("ascii")}
        )
        relay.send(RESOLVE_PYTHON_VERSION)
        relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
        with processed.open("wb", buffering=0) as checkpoint:
            assert checkpoint.write(b"1") == 1
    with release.open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    relay.complete()
    relay.expect(EVALUATION)
    relay.send(
        {"kind": "stdout_bytes", "data": base64.b64encode(b"\x82\xac").decode("ascii")}
    )
    relay.complete()
    relay.retire()


def run_progress_polls(relay: ScriptedRelay) -> None:
    directory = Path(os.environ["MCP_CONSOLE_TEST_PREVIEW_DIRECTORY"])
    intervals = json.loads(
        Path(os.environ["MCP_CONSOLE_TEST_PROGRESS_INTERVALS"]).read_text()
    )
    relay.ready()
    relay.expect(EVALUATION)
    for events in intervals:
        with (directory / "progress-release").open("rb", buffering=0) as checkpoint:
            assert checkpoint.read(1) == b"1"
        for event in events:
            if event["kind"] == "native_stderr":
                write_all(2, base64.b64decode(event["data"]))
            else:
                relay.send(event)
        # Ordered callback receipt proves ingestion, including invisible bytes.
        relay.send(RESOLVE_PYTHON_VERSION)
        relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
        with (directory / "progress-processed").open("wb", buffering=0) as checkpoint:
            assert checkpoint.write(b"1") == 1
    with (directory / "progress-release").open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    relay.complete()
    relay.retire()


def run_ansi_projection(relay: ScriptedRelay) -> None:
    inputs = json.loads(
        Path(os.environ["MCP_CONSOLE_TEST_ANSI_INPUTS"]).read_text(encoding="utf-8")
    )
    relay.ready()
    while True:
        command = relay.receive()
        if command["kind"] == "shutdown":
            relay.retire(command)
            return
        assert command["kind"] == "evaluate", command
        fixture = inputs[command["source"]]
        if "sample" in fixture:
            raw = fixture["sample"].encode()
            fragmentations = [
                [raw],
                *([raw[:split], raw[split:]] for split in range(len(raw) + 1)),
                [raw[index : index + 1] for index in range(len(raw))],
            ]
            for chunks in fragmentations:
                for chunk in chunks:
                    relay.send(
                        {
                            "kind": "stdout_bytes",
                            "data": base64.b64encode(chunk).decode("ascii"),
                        }
                    )
        else:
            relay.send_batch(fixture["events"])
        relay.complete()


def run_ansi_diagnostics(relay: ScriptedRelay) -> None:
    directory = Path(os.environ["MCP_CONSOLE_TEST_PREVIEW_DIRECTORY"])
    inputs = json.loads(
        Path(os.environ["MCP_CONSOLE_TEST_ANSI_INPUTS"]).read_text(encoding="utf-8")
    )
    relay.ready()
    command = relay.receive()
    assert command["kind"] == "evaluate", command
    for message in inputs[command["source"]]["events"]:
        with (directory / "diagnostic-release").open("rb", buffering=0) as checkpoint:
            assert checkpoint.read(1) == b"1"
        if message["kind"] == "native_stderr":
            write_all(2, base64.b64decode(message["data"]))
        else:
            relay.send(message)
    with (directory / "diagnostic-release").open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    relay.complete()
    relay.retire()


def run_ansi_polls(relay: ScriptedRelay) -> None:
    directory = Path(os.environ["MCP_CONSOLE_TEST_PREVIEW_DIRECTORY"])
    relay.ready()
    relay.expect(EVALUATION)
    for data in (
        b"first\x1b[31",
        b"msecond\x1b]0;hidden",
        b"third\x1b[32m:\xe2",
        b"\x82\xac\x1b[0m\n",
    ):
        with (directory / "partial-release").open("rb", buffering=0) as checkpoint:
            assert checkpoint.read(1) == b"1"
        relay.send(
            {"kind": "stdout_bytes", "data": base64.b64encode(data).decode("ascii")}
        )
        # A response to this ordered request proves the preceding bytes reached
        # the tape; a write or fixture-start checkpoint would not prove it.
        relay.send(RESOLVE_PYTHON_VERSION)
        relay.expect(PYTHON_VERSION_RESOLUTION_FAILED)
        with (directory / "partial-processed").open("wb", buffering=0) as checkpoint:
            assert checkpoint.write(b"1") == 1
    with (directory / "partial-release").open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    relay.complete()
    relay.retire()


def run_partial_utf8_completion(relay: ScriptedRelay) -> None:
    relay.ready()
    for kind in ("stdout_bytes", "stderr_bytes"):
        for chunk in (b"\xe2", b"\x82\xac"):
            relay.expect(EVALUATION)
            relay.send({"kind": kind, "data": base64.b64encode(chunk).decode("ascii")})
            relay.complete()
    relay.retire()


def main() -> None:
    scenarios = {
        "ready": run_ready,
        "startup_output": run_startup_output,
        "preview_raw": run_preview_raw,
        "preview_direct_allocations": run_preview_direct_allocations,
        "partial_utf8_polls": run_partial_utf8_polls,
        "ansi_projection": run_ansi_projection,
        "progress_polls": run_progress_polls,
        "ansi_diagnostics": run_ansi_diagnostics,
        "ansi_polls": run_ansi_polls,
        "preview_raw_prelude": run_preview_raw_prelude,
        "partial_utf8_completion": run_partial_utf8_completion,
        "evaluate": run_evaluate,
        "raw_output": run_raw_output,
        "split_terminal_redraws": run_split_terminal_redraws,
        "independent_stream_redraws": run_independent_stream_redraws,
        "interleaved_stream_redraws": run_interleaved_stream_redraws,
        "raw_malformed_redraw": run_raw_malformed_redraw,
        "empty_raw_close_between_redraws": run_empty_raw_close_between_redraws,
        "stdout_close_between_utf8_fragments": run_raw_close_between_utf8_fragments,
        "stderr_close_between_utf8_fragments": run_raw_close_between_utf8_fragments,
        "stdin": run_stdin,
        "initial_requirements_stdin_idempotent": (
            run_initial_requirements_stdin_idempotent
        ),
        "live_r_requirements_then_evaluate": run_live_r_requirements_then_evaluate,
        "stdin_forwarding_failure": run_stdin_forwarding_failure,
        "interrupt": run_interrupt,
        "withheld_idle_input": run_withheld_idle_input,
        "controlled_restart_stdin": run_controlled_restart_stdin,
        "controlled_restart_stdin_only": run_controlled_restart_stdin_only,
        "controlled_restart_requirements": run_controlled_restart_requirements,
        "controlled_interrupt_stdin_evaluate": (
            run_controlled_interrupt_stdin_evaluate
        ),
        "controlled_interrupt_stdin_requirements_evaluate": (
            run_controlled_interrupt_stdin_requirements_evaluate
        ),
        "controlled_interrupt_stdin_requirement_failure": (
            run_controlled_interrupt_stdin_requirement_failure
        ),
        "controlled_interrupt_stdin_invalid_requirements": (
            run_controlled_interrupt_stdin_invalid_requirements
        ),
        "controlled_and_standalone_interrupts": (
            run_controlled_and_standalone_interrupts
        ),
        "controlled_interrupt_still_active": run_controlled_interrupt_still_active,
        "controlled_completion_then_interrupt": (
            run_controlled_completion_then_interrupt
        ),
        "controlled_interrupt_with_waiting_poll": (
            run_controlled_interrupt_with_waiting_poll
        ),
        "completion_before_interrupt_ack": run_completion_before_interrupt_ack,
        "cancelled_interrupt_during_live_r_preparation": (
            run_cancelled_interrupt_during_live_r_preparation
        ),
        "serialized_cross_source_order": run_serialized_cross_source_order,
        "failed_during_resolver_callback": failed_during_resolver_callback,
        "shutdown": run_shutdown,
        "shutdown_nonzero": run_shutdown_nonzero,
        "shutdown_status_137": run_shutdown_status_137,
        "shutdown_nonzero_after_output": run_shutdown_nonzero_after_output,
        "blocked_live_r_resolver_shutdown": run_blocked_live_r_resolver_shutdown,
        "late_r_prepared_retirement": run_late_r_prepared_retirement,
        "pre_marker_r_prepared_replacement": run_pre_marker_r_prepared_replacement,
        "r_preparation_failure": run_r_preparation_failure,
        "r_resolution_during_r_preparation": run_r_resolution_during_r_preparation,
        "idle_r_resolution_owns_environment": run_idle_r_resolution_owns_environment,
        "explicit_r_preparation_owns_environment": (
            run_explicit_r_preparation_owns_environment
        ),
        "completion_before_r_activation": run_completion_before_r_activation,
        "cancelled_waiting_send": run_cancelled_waiting_send,
        "fatal": run_fatal,
        "fatal_status_137": run_fatal_exit,
        "fatal_sigterm": run_fatal_exit,
        "truncated": run_truncated,
        "exit_zero": run_exit_zero,
        "exit_nonzero": run_exit_nonzero,
        "signaled": run_signaled,
    }
    scenario = os.environ[SCENARIO_ENV]
    assert scenario in scenarios, f"unknown scripted relay scenario: {scenario}"

    relay = ScriptedRelay()
    try:
        scenarios[scenario](relay)
    finally:
        relay.close()


if __name__ == "__main__":
    main()
