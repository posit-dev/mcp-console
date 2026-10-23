"""Portable relay lifecycle scenarios, observed through the public JSONL seam."""

import json
from collections.abc import Callable
from pathlib import Path
import subprocess
from typing import BinaryIO

from support.relay_commands import WORKER_COMMANDS

LIFECYCLE_COMMANDS = {
    "invalid_sideband": {"kind": "evaluate", "language": "r", "source": "42"},
    "closed_stdin": {"kind": "stdin", "data": "hello"},
    "exit_tail": {"kind": "evaluate", "language": "r", "source": "42"},
    "exit_invalid": {"kind": "evaluate", "language": "r", "source": "42"},
}


def exercise_shutdown_admission(
    process: subprocess.Popen[bytes],
    checkpoint: BinaryIO,
    marker: Path,
    scenario: str,
    wait_worker: Callable[[float], None],
    outcome: dict,
    *,
    shutdown_on_sideband_eof: bool = False,
) -> list[dict]:
    """Keep input open and the worker alive until the relay enforces its budget."""
    first = {"kind": "shutdown", "grace_millis": 1000}
    trailing = b"".join(
        json.dumps(command).encode() + b"\n"
        for command in [
            *WORKER_COMMANDS,
            {"kind": "stdin", "data": "ignored"},
            {"kind": "interrupt", "request_id": 999},
            {"kind": "shutdown", "grace_millis": 30000},
        ]
    )
    if scenario == "retirement_eof":
        process.stdin.close()
        assert checkpoint.readline() == b"shutdown\n", "clean EOF did not retire worker"
        expected = [{"kind": "shutdown"}]
        acceptance = []
    elif scenario == "retirement_sideband":
        initial = {"kind": "evaluate", "language": "r", "source": "close sideband"}
        process.stdin.write(json.dumps(initial).encode() + b"\n")
        process.stdin.flush()
        # The worker closes only its sideband output and remains alive. Stdin
        # EOF proves the supervisor has begun retirement before late input.
        assert checkpoint.readline() == b"stdin closed\n", "stdin admission stayed open"
        process.stdin.write(trailing)
        process.stdin.flush()
        expected = [initial]
        acceptance = []
    else:
        # Native delivery retains the accepted request's identity. Ignored
        # trailing interrupts must not produce another result.
        process.stdin.write(b'{"kind":"interrupt","request_id":731}\n')
        process.stdin.flush()
        assert json.loads(process.stdout.readline()) == {
            "kind": "interrupt_result",
            "request_id": 731,
        }
        batch = json.dumps(first).encode() + b"\n"
        if scenario == "retirement_batch":
            batch += trailing + b'{"kind":"broken"}\n{"kind":'
        process.stdin.write(batch)
        process.stdin.flush()
        assert checkpoint.readline() == b"shutdown\n", "worker did not receive shutdown"
        if scenario == "retirement_receipt":
            process.stdin.write(trailing)
            process.stdin.flush()
        expected = [{"kind": "shutdown"}]
        acceptance = [{"kind": "shutdown_started"}]

    # A process checkpoint, not a sleep or timestamp, proves forced retirement.
    # The 30-second repeated allowance cannot satisfy this bounded exit wait.
    wait_worker(3)
    process.wait(timeout=5)
    command_input = process.stdin
    process.stdin = None
    try:
        output, errors = process.communicate(timeout=5)
    finally:
        command_input.close()
    assert process.returncode == 0 and errors == b"", (process.returncode, errors)
    forwarded = [json.loads(line) for line in marker.read_bytes().splitlines()]
    # Windows sends a cooperative shutdown on sideband EOF; Unix waits for the
    # existing grace without writing another command to that closed transport.
    if scenario == "retirement_sideband" and shutdown_on_sideband_eof:
        expected.append({"kind": "shutdown"})
    assert forwarded == expected, forwarded
    assert marker.with_suffix(".stdin").read_bytes() == b""
    events = [json.loads(line) for line in output.splitlines()]
    assert events == [
        *acceptance,
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        {"kind": "worker_sideband_closed"},
        outcome,
    ], events
    return [{"forwarded": forwarded, "stdin": "", "events": events}]


def assert_failure_tail(
    events: list[dict], diagnostic: str, outcome: dict
) -> list[dict]:
    assert len(events) == 5, events
    fatal = events[2]
    assert fatal["kind"] == "fatal" and diagnostic in fatal["message"], events
    assert events == [
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        fatal,
        {"kind": "worker_sideband_closed"},
        outcome,
    ], events
    return events


def assert_exit_tail(events: list[dict], *, invalid: bool = False) -> dict:
    output = [
        {"kind": "console_output", "data": f"{index:04}"} for index in range(1024)
    ]
    assert events[:1024] == output, events
    if invalid:
        tail = assert_failure_tail(
            events[1024:],
            "worker sideband read failed: unknown variant `broken`",
            {"kind": "worker_exited", "code": 0},
        )
    else:
        tail = [
            {"kind": "image", "data": "eA==", "mime_type": "image/png"},
            {"kind": "completed"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ]
        assert events[1024:] == tail, events
    # Compact only after asserting every payload and the complete terminal tail.
    return {
        "output_count": len(output),
        "first": output[0],
        "last": output[-1],
        "tail": tail,
    }
