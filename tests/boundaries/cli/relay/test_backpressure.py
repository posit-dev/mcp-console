#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.native import SHARED_LIBRARY_FLAG
from support.native import LOADER_VARIABLE
from support.normalization import code
from support.capture import read_relay_ready, assert_worker_started
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@contextmanager
def stdout_backpressure_environment(
    interposer_source: str = "relay_stdout_backpressure.c",
) -> Iterator[tuple[Path, dict[str, str], FifoCheckpoint]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        interposer = root / "relay-stdout-backpressure.dylib"
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / interposer_source,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        blocked = FifoCheckpoint.create(root / "stdout-blocked")
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        environment[LOADER_VARIABLE] = str(interposer)
        environment["MCP_CONSOLE_TEST_STDOUT_BLOCKED"] = str(root / "stdout-blocked")
        try:
            yield root, environment, blocked
        finally:
            blocked.close()


@contextmanager
def backpressured_relay(
    binary: Path, mode: str
) -> Iterator[tuple[subprocess.Popen[str], dict[str, FifoCheckpoint], Events]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with stdout_backpressure_environment() as (root, environment, blocked):
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in ("worker-interrupted", "worker-shutdown", "worker-exit")
        }
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker" / "backpressured_output.py",
                mode,
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
            assert process.stdin is not None and process.stdout is not None
            assert read_relay_ready(process.stdout) == {"kind": "ready"}
            worker_pid = int((root / "worker-pid").read_text())
            worker_exit.watch_process(worker_pid)
            send(process, {"kind": "evaluate", "language": "r", "source": "output"})
            blocked.wait("relay stdout write returned EAGAIN")
            yield process, checkpoints, worker_exit
        finally:
            # The relay remains our waitable group leader until the bounded wait
            # in the test succeeds. Kill its direct worker too on an early failure.
            if process.returncode is None:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)
            worker_exit.close()
            for checkpoint in checkpoints.values():
                checkpoint.close()


def send(process: subprocess.Popen[str], command: dict[str, object]) -> None:
    assert process.stdin is not None
    process.stdin.write(json.dumps(command) + "\n")
    process.stdin.flush()


def retirement_result(
    process: subprocess.Popen[str], worker_exit: Events
) -> Transcript:
    observed = worker_exit.wait(3)
    assert len(observed) == 1, "direct worker did not exit"
    # Do not read stdout until process exit: draining it would release the bug.
    exit_code = process.wait(timeout=5)
    stdout, stderr = process.communicate(timeout=5)
    assert exit_code == 1, (exit_code, stderr)
    assert stderr == (
        "relay stdout write failed: relay stdout retirement deadline expired\n"
    ), stderr
    output = {"kind": "console_output", "data": "blocked output\n"}
    frame = json.dumps(output, separators=(",", ":")) + "\n"
    assert stdout, stdout
    repeated = frame * (len(stdout) // len(frame) + 1)
    assert stdout == repeated[: len(stdout)], stdout
    # Pipe capacity determines both the repetition count and partial-frame
    # boundary. Preserve the exact repeated event after validating every byte.
    return [
        {
            "stdout_prefix": {"repeated_event": output},
            "exit_code": exit_code,
            "stderr": stderr,
        }
    ]


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_starts_worker_shutdown_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "shutdown") as (
        process,
        checkpoints,
        worker_exit,
    ):
        send(process, {"kind": "interrupt", "request_id": 1})
        checkpoints["worker-interrupted"].wait("worker accepted SIGINT")
        send(process, {"kind": "shutdown", "grace_millis": 1000})
        checkpoints["worker-shutdown"].wait("worker accepted shutdown", timeout=3)
        return retirement_result(process, worker_exit)


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_finishes_natural_worker_exit_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "natural") as (process, checkpoints, worker_exit):
        checkpoints["worker-exit"].release()
        return retirement_result(process, worker_exit)


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_retires_when_backpressure_exhausts_supervisor_event_reserve(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "shutdown") as (
        process,
        checkpoints,
        worker_exit,
    ):
        for request_id in range(1, 17):
            send(process, {"kind": "interrupt", "request_id": request_id})
            checkpoints["worker-interrupted"].wait(
                f"worker accepted SIGINT {request_id}"
            )
        send(process, {"kind": "interrupt", "request_id": 17})
        observed = worker_exit.wait(5)
        assert len(observed) == 1, observed
        assert process.wait(timeout=5) == 1
        stdout, stderr = process.communicate(timeout=5)
        assert stderr == "relay supervisor event queue capacity exceeded\n", stderr
        output = {"kind": "console_output", "data": "blocked output\n"}
        frame = json.dumps(output, separators=(",", ":")) + "\n"
        assert stdout
        assert stdout == (frame * (len(stdout) // len(frame) + 1))[: len(stdout)]
        return [
            {
                "submitted_interrupts": 17,
                "acknowledged_interrupts": 16,
                "stdout_prefix": {"repeated_event": output},
                "exit_code": process.returncode,
                "stderr": stderr,
            }
        ]


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_resumes_relay_output_after_downstream_backpressure(
    binary: Path,
) -> Transcript:
    with backpressured_relay(binary, "natural") as (process, checkpoints, worker_exit):
        assert process.stdout is not None
        lines = read_lines(process.stdout, 4096, "every buffered worker output record")
        output = {"kind": "console_output", "data": "blocked output\n"}
        assert all(json.loads(line) == output for line in lines), lines
        checkpoints["worker-exit"].release()
        observed = worker_exit.wait(3)
        assert len(observed) == 1, observed
        assert process.wait(timeout=5) == 0
        stdout, stderr = process.communicate(timeout=5)
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
                "repeated_output": output,
                "output_count": len(lines),
                "events": events,
                "exit_code": process.returncode,
                "stderr": stderr,
            }
        ]


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_finishes_startup_failure_while_relay_stdout_is_backpressured(
    binary: Path,
) -> Transcript:
    with stdout_backpressure_environment() as (root, environment, blocked):
        reader, writer = os.pipe()
        with os.fdopen(reader, "rb") as output, os.fdopen(writer, "wb") as destination:
            os.set_blocking(writer, False)
            preloaded = 0
            try:
                while True:
                    preloaded += os.write(writer, b"x")
            except BlockingIOError:
                pass
            os.set_blocking(writer, True)
            process = subprocess.Popen(
                [binary, "worker-relay", root / "missing-worker"],
                stdin=subprocess.DEVNULL,
                stdout=destination,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            destination.close()
            try:
                blocked.wait("startup Fatal write returned EAGAIN")
                exit_code = process.wait(timeout=5)
                _, stderr = process.communicate(timeout=5)
                assert exit_code == 1, (exit_code, stderr)
                assert stderr == (
                    "failed to launch worker: No such file or directory (os error 2); "
                    "additionally relay stdout write failed: "
                    "relay stdout retirement deadline expired\n"
                ), stderr
                # The preloaded bytes belong to the fixture. A full pipe leaves
                # no room for any part of the relay's startup Fatal frame.
                assert output.read() == b"x" * preloaded
                return [{"exit_code": exit_code, "stdout": "", "stderr": stderr}]
            finally:
                if process.returncode is None:
                    process.kill()
                process.communicate(timeout=10)


@contextmanager
def retirement_clock_environment(
    after_frame: dict[str, object],
) -> Iterator[tuple[Path, dict[str, str], FifoCheckpoint]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        interposer = root / "completed-output.dylib"
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / "relay_completed_output.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        completed = FifoCheckpoint.create(root / "output-complete")
        environment = os.environ.copy()
        environment[LOADER_VARIABLE] = str(interposer)
        environment["MCP_CONSOLE_TEST_OUTPUT_COMPLETE"] = str(completed.path)
        environment["MCP_CONSOLE_TEST_CLOCK_AFTER_FRAME"] = (
            json.dumps(after_frame, separators=(",", ":")) + "\n"
        )
        try:
            yield root, environment, completed
        finally:
            completed.close()


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_succeeds_when_deadline_passes_after_final_output(binary: Path) -> Transcript:
    with retirement_clock_environment({"kind": "worker_exited", "code": 0}) as (
        root,
        environment,
        completed,
    ):
        # fmt: python
        worker = code(r"""
            import os
            import sys

            os.write(int(os.environ["MCP_CONSOLE_SIDEBAND_WRITE_FD"]), b'{"kind":"ready"}\n')
            with open(os.environ["TEST_WORKER_READY"], "wb", buffering=0) as ready:
                ready.write(b"1")
            sys.stdin.buffer.read()
            """)
        ready = FifoCheckpoint.create(root / "worker-ready")
        environment["TEST_WORKER_READY"] = str(ready.path)
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, "-c", worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            ready.wait("worker initialized before controller EOF")
            stdout, stderr = process.communicate(timeout=10)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
            ready.close()
        completed.wait("retirement clock advanced after final output")
        events = [json.loads(line) for line in stdout.splitlines()]
        assert_worker_started(events.pop(0))
        assert events == [
            {"kind": "ready"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        assert process.returncode == 0, stderr
        assert stderr == "", stderr
        return [{"events": events, "exit_code": process.returncode, "stderr": stderr}]


@requires(WORKER, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_writes_regular_file_after_retirement_deadline(binary: Path) -> Transcript:
    with retirement_clock_environment({"kind": "stdout_closed"}) as (
        root,
        environment,
        completed,
    ):
        # fmt: python
        worker = code(r"""
            import os
            import sys

            os.write(int(os.environ["MCP_CONSOLE_SIDEBAND_WRITE_FD"]), b'{"kind":"ready"}\n')
            with open(os.environ["TEST_WORKER_READY"], "wb", buffering=0) as ready:
                ready.write(b"1")
            sys.stdin.buffer.read()
            """)
        destination = root / "relay.jsonl"
        ready = FifoCheckpoint.create(root / "worker-ready")
        environment["TEST_WORKER_READY"] = str(ready.path)
        with destination.open("w") as output:
            process = subprocess.Popen(
                [binary, "worker-relay", sys.executable, "-c", worker],
                stdin=subprocess.PIPE,
                stdout=output,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
            )
            try:
                ready.wait("worker initialized before controller EOF")
                _, stderr = process.communicate(timeout=10)
            finally:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=10)
                ready.close()
        completed.wait("retirement clock advanced before regular-file output")
        assert process.returncode == 0, stderr
        assert stderr == "", stderr
        events = [json.loads(line) for line in destination.read_text().splitlines()]
        assert_worker_started(events.pop(0))
        assert events == [
            {"kind": "ready"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        return [{"events": events, "exit_code": process.returncode, "stderr": stderr}]


if __name__ == "__main__":
    run_this_suite(__file__)
