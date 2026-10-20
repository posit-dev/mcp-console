#!/usr/bin/env -S uv run --script

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
from support.native import LOADER_VARIABLE, build_interposer
from support.capture import assert_worker_started
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, WORKER, requires
from support.suites import run_this_suite


@contextmanager
def pipe_relay(
    binary: Path, mode: str, *, faults: bool = False
) -> Iterator[tuple[subprocess.Popen[str], Path, FifoCheckpoint, FifoCheckpoint]]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        blocked = FifoCheckpoint.create(root / "writer-blocked")
        release = FifoCheckpoint.create(root / "worker-exit")
        interposer = build_interposer(root, "sideband_pipe_io")
        environment[LOADER_VARIABLE] = str(interposer)
        environment["MCP_CONSOLE_TEST_PIPE_FAULTS"] = "1" if faults else "0"
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker" / "pipe_peer.py",
                mode,
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        try:
            assert process.stdin and process.stdout and process.stderr
            assert_worker_started(receive(process))
            assert receive(process) == {"kind": "ready"}
            send(process, {"kind": "stdin", "data": "start\n"})
            yield process, root, blocked, release
        finally:
            # The waitable relay owns this group until the test reaps it. Its
            # deliberately retained descendant is explicitly ours to retire.
            holder = root / "holder-pid"
            if holder.exists():
                os.kill(int(holder.read_text()), signal.SIGKILL)
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            blocked.close()
            release.close()


def send(process: subprocess.Popen[str], message: dict[str, object]) -> None:
    process.stdin.write(json.dumps(message) + "\n")
    process.stdin.flush()


def receive(process: subprocess.Popen[str]) -> dict[str, object]:
    return json.loads(read_lines(process.stdout, 1, "relay event")[0])


@requires(WORKER, NATIVE_FIXTURES)
def test_two_pipe_exchange_survives_short_and_interrupted_io(
    binary: Path,
) -> Transcript:
    with pipe_relay(binary, "exchange", faults=True) as (process, root, _, _):
        source = "precise 👩🏽‍💻" * 8192
        send(process, {"kind": "evaluate", "language": "r", "source": source})
        assert [
            json.loads(line)
            for line in read_lines(process.stdout, 2, "echo and completion")
        ] == [{"kind": "console_output", "data": source}, {"kind": "completed"}]
        send(process, {"kind": "shutdown", "grace_millis": 1000})
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and stderr == "", (process.returncode, stderr)
        for checkpoint in (
            "write-eintr",
            "write-eagain",
            "short-write",
            "read-eintr",
            "read-eagain",
            "poll-eintr",
        ):
            assert (root / checkpoint).exists(), checkpoint
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events == [
            {"kind": "shutdown_started"},
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        return events


@requires(WORKER, NATIVE_FIXTURES)
def test_cancels_full_pipe_with_both_endpoints_retained(binary: Path) -> Transcript:
    with pipe_relay(binary, "retained") as (process, root, blocked, release):
        send(
            process,
            {"kind": "evaluate", "language": "r", "source": "x" * (2 * 1024 * 1024)},
        )
        blocked.wait("sideband pipe has no writable capacity")
        release.release()
        # Reading relay output or killing the descendant cannot release the
        # sideband writer: it must observe cancellation and join on its own.
        assert process.wait(timeout=3) == 0
        os.kill(int((root / "holder-pid").read_text()), 0)
        stdout, stderr = process.communicate(timeout=5)
        assert stderr == "", stderr
        events = [json.loads(line) for line in stdout.splitlines()]
        assert events == [
            {"kind": "stdout_closed"},
            {"kind": "stderr_closed"},
            {"kind": "worker_sideband_closed"},
            {"kind": "worker_exited", "code": 0},
        ], events
        return events


@requires(WORKER, NATIVE_FIXTURES)
def test_reports_epipe_without_sigpipe_termination(binary: Path) -> Transcript:
    with pipe_relay(binary, "closed") as (process, _, _, _):
        assert receive(process) == {"kind": "console_output", "data": "reader closed\n"}
        send(process, {"kind": "evaluate", "language": "r", "source": "closed"})
        assert process.wait(timeout=5) == 0
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and stderr == "", (process.returncode, stderr)
        events = [json.loads(line) for line in stdout.splitlines()]
        assert {
            "kind": "fatal",
            "message": "worker sideband write failed: Broken pipe (os error 32)",
        } in events, events
        return events


@requires(WORKER, NATIVE_FIXTURES)
def test_reports_eof_in_partial_frame(binary: Path) -> Transcript:
    with pipe_relay(binary, "partial") as (process, _, _, _):
        assert process.wait(timeout=5) == 0
        stdout, stderr = process.communicate(timeout=5)
        assert process.returncode == 0 and stderr == "", (process.returncode, stderr)
        events = [json.loads(line) for line in stdout.splitlines()]
        assert {
            "kind": "fatal",
            "message": "worker sideband read failed: worker sideband closed midway through a frame",
        } in events, events
        return events


if __name__ == "__main__":
    run_this_suite(__file__)
