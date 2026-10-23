#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli.relay.test_backpressure import send, stdout_backpressure_environment
from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.capture import read_relay_ready
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite

MEBIBYTE = 1024 * 1024
READY = b'{"kind":"ready"}\n'


def output_frame(index: int, size: int) -> str:
    empty = '{"kind":"console_output","data":""}\n'
    prefix = f"{index:04d}:"
    data = prefix + "x" * (size - len(empty) - len(prefix))
    return (
        json.dumps({"kind": "console_output", "data": data}, separators=(",", ":"))
        + "\n"
    )


@dataclass
class QueuedRelay:
    process: subprocess.Popen[str]
    checkpoints: dict[str, FifoCheckpoint]
    worker_exit: Events
    consumed_bytes: int
    consumed_frames: int


@contextmanager
def queued_relay(
    binary: Path,
    first_size: int,
    frame_size: int,
    frame_count: int,
    completion: str,
) -> Iterator[QueuedRelay]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with stdout_backpressure_environment("relay_queue_backpressure.c") as (
        root,
        environment,
        stdout_blocked,
    ):
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in (
                "queue-wait",
                "continue-output",
                "worker-interrupted",
                "worker-shutdown",
                "worker-exit",
            )
        }
        environment["MCP_CONSOLE_TEST_QUEUE_WAIT"] = str(root / "queue-wait")
        counts = root / "queue-counts"
        environment["MCP_CONSOLE_TEST_QUEUE_COUNTERS"] = str(counts)
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker" / "queued_output.py",
                str(first_size),
                str(frame_size),
                str(frame_count),
                completion,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        worker_exit = Events()
        try:
            assert process.stdout is not None
            assert read_relay_ready(process.stdout) == {"kind": "ready"}
            worker_pid = int((root / "worker-pid").read_text())
            worker_exit.watch_process(worker_pid)
            send(
                process,
                {"kind": "evaluate", "language": "r", "source": "queued output"},
            )
            stdout_blocked.wait("downstream stdout returned EAGAIN")
            checkpoints["continue-output"].release()
            checkpoints["queue-wait"].wait("sideband reader waiting for event capacity")
            consumed_bytes, consumed_frames = map(int, counts.read_text().split())
            yield QueuedRelay(
                process,
                checkpoints,
                worker_exit,
                consumed_bytes - len(READY),
                consumed_frames - 1,
            )
        finally:
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
            worker_exit.close()
            for checkpoint in checkpoints.values():
                checkpoint.close()


def interrupt_and_shutdown(relay: QueuedRelay, first_size: int) -> Transcript:
    send(relay.process, {"kind": "interrupt", "request_id": 1})
    relay.checkpoints["worker-interrupted"].wait(
        "worker accepted SIGINT while queue is full"
    )
    send(relay.process, {"kind": "shutdown", "grace_millis": 1000})
    relay.checkpoints["worker-shutdown"].wait(
        "worker accepted shutdown while queue is full"
    )
    observed = relay.worker_exit.wait(3)
    assert len(observed) == 1, observed
    assert relay.process.wait(timeout=5) == 1
    stdout, stderr = relay.process.communicate(timeout=5)
    assert stdout and output_frame(0, first_size).startswith(stdout), stdout
    assert (
        stderr
        == "relay stdout write failed: relay stdout retirement deadline expired\n"
    ), stderr
    return [
        {
            "stdout_prefix": {
                "event": "console_output",
                "data_prefix": "0000:",
                "repeated_character": "x",
            },
            "exit_code": relay.process.returncode,
            "stderr": stderr,
        }
    ]


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_backpressures_worker_when_relay_event_bytes_are_full(
    binary: Path,
) -> Transcript:
    with queued_relay(binary, MEBIBYTE, MEBIBYTE, 32, "shutdown") as relay:
        assert relay.consumed_bytes <= 8 * MEBIBYTE + MEBIBYTE + 8192, relay
        assert relay.consumed_frames == 9, relay
        return interrupt_and_shutdown(relay, MEBIBYTE)


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_backpressures_worker_when_relay_event_count_is_full(
    binary: Path,
) -> Transcript:
    with queued_relay(binary, MEBIBYTE, 8192, 1024, "shutdown") as relay:
        assert relay.consumed_bytes <= MEBIBYTE + 512 * 8192 + 8192, relay
        assert relay.consumed_frames == 513, relay
        return interrupt_and_shutdown(relay, MEBIBYTE)


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_preserves_oversized_frames_when_relay_output_resumes(
    binary: Path,
) -> Transcript:
    size = 8 * MEBIBYTE + 4096
    with queued_relay(binary, size, size, 3, "natural") as relay:
        assert relay.consumed_bytes <= 2 * size + 8192, relay
        assert relay.consumed_frames == 2, relay
        assert relay.process.stdout is not None
        frames = read_lines(relay.process.stdout, 3, "all complete oversized frames")
        assert frames == [output_frame(index, size).rstrip("\n") for index in range(3)]
        relay.checkpoints["worker-exit"].release()
        assert relay.process.wait(timeout=5) == 0
        stdout, stderr = relay.process.communicate(timeout=5)
        assert relay.process.returncode == 0, stderr
        assert stderr == "", stderr
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events == [
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        return [
            {
                "output_frames": [
                    {
                        "index": index,
                        "encoded_bytes": size,
                        "data_prefix": f"{index:04d}:",
                        "repeated_character": "x",
                    }
                    for index in range(3)
                ],
                "events": events,
                "exit_code": relay.process.returncode,
                "stderr": stderr,
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
