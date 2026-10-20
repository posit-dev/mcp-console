#!/usr/bin/env -S uv run --script

import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import (
    capture_process_identity,
    live_processes,
    stop_process_group,
)
from support.capture import read_relay_ready, assert_worker_started
from support.records import Transcript
from support.relay_lifecycle import (
    LIFECYCLE_COMMANDS,
    assert_exit_tail,
    assert_failure_tail,
    exercise_shutdown_admission,
)
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@requires(WORKER, PROCESS_EVENTS)
def test_shutdown_ignores_buffered_commands_and_trailing_bytes(
    binary: Path,
) -> Transcript:
    return shutdown_admission(binary, "retirement_batch")


@requires(WORKER, PROCESS_EVENTS)
def test_shutdown_ignores_commands_sent_after_worker_receipt(
    binary: Path,
) -> Transcript:
    return shutdown_admission(binary, "retirement_receipt")


@requires(WORKER, PROCESS_EVENTS)
def test_sideband_eof_closes_admission_without_renewing_deadline(
    binary: Path,
) -> Transcript:
    return shutdown_admission(binary, "retirement_sideband")


@requires(WORKER, PROCESS_EVENTS)
def test_clean_controller_eof_retires_without_shutdown_acceptance(
    binary: Path,
) -> Transcript:
    return shutdown_admission(binary, "retirement_eof")


def shutdown_admission(binary: Path, scenario: str) -> Transcript:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/relay_worker/lifecycle.py"
    with (
        tempfile.TemporaryDirectory() as directory,
        Events() as exits,
        socket.socket() as listener,
    ):
        root = Path(directory)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(10)
        marker = root / "dispatched"
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, fixture, scenario],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(
                os.environ,
                TMPDIR=directory,
                MCP_CONSOLE_HOME=directory,
                TEST_WORKER_PID=str(root / "worker-pid"),
                TEST_WORKER_READY=f"127.0.0.1:{listener.getsockname()[1]}",
                TEST_DISPATCHED=str(marker),
            ),
            start_new_session=True,
        )
        try:
            with listener.accept()[0] as connection:
                connection.settimeout(5)
                with connection.makefile("rb") as checkpoint:
                    worker = capture_process_identity(int(checkpoint.readline()))
                    exits.watch_process(worker[0])
                    assert read_relay_ready(process.stdout) == {"kind": "ready"}

                    def wait_worker(timeout: float) -> None:
                        assert exits.wait(timeout) == {worker[0]}, (
                            "worker budget was renewed"
                        )

                    result = exercise_shutdown_admission(
                        process,
                        checkpoint,
                        marker,
                        scenario,
                        wait_worker,
                        {"kind": "worker_signaled", "signal": signal.SIGKILL},
                    )
                    assert live_processes([worker]) == [], (
                        "direct worker was not reaped"
                    )
                    return result
        finally:
            if process.poll() is None:
                stop_process_group(process.pid)
            process.communicate(timeout=10)


@requires(WORKER, PROCESS_EVENTS)
def test_fatal_precedes_sideband_closure(binary: Path) -> Transcript:
    events = lifecycle_events(binary, "invalid_sideband")
    return assert_failure_tail(
        events,
        "worker sideband read failed: unknown variant `broken`",
        {"kind": "worker_signaled", "signal": signal.SIGKILL},
    )


@requires(WORKER, PROCESS_EVENTS)
def test_stdin_write_failure_retires_worker(binary: Path) -> Transcript:
    events = lifecycle_events(binary, "closed_stdin")
    return assert_failure_tail(
        events,
        "worker stdin write failed:",
        {"kind": "worker_signaled", "signal": signal.SIGKILL},
    )


@requires(WORKER, PROCESS_EVENTS)
def test_drains_sideband_after_exit(binary: Path) -> Transcript:
    return [assert_exit_tail(lifecycle_events(binary, "exit_tail"))]


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_retains_failure_discovered_during_exit_drain(binary: Path) -> Transcript:
    return [assert_exit_tail(lifecycle_events(binary, "exit_invalid"), invalid=True)]


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_setup_failure_reaps_child_before_terminal_publication(
    binary: Path,
) -> Transcript:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/relay_worker/lifecycle.py"
    with tempfile.TemporaryDirectory() as directory, Events() as exits:
        root = Path(directory)
        gates = {
            name: FifoCheckpoint.create(root / name)
            for name in ("entered", "release", "ready")
        }
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, fixture, "setup_failure"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env={
                **os.environ,
                LOADER_VARIABLE: str(build_interposer(root, "relay_setup_failure")),
                "TMPDIR": directory,
                "MCP_CONSOLE_HOME": directory,
                "TEST_WORKER_PID": str(root / "worker-pid"),
                "TEST_WORKER_READY": str(root / "ready"),
                "MCP_CONSOLE_TEST_SETUP_ENTERED": str(root / "entered"),
                "MCP_CONSOLE_TEST_SETUP_RELEASE": str(root / "release"),
            },
            start_new_session=True,
        )
        try:
            gates["entered"].wait("relay setup reached post-spawn pipe creation")
            gates["ready"].wait("worker wrote raw and semantic setup output")
            worker = capture_process_identity(int((root / "worker-pid").read_text()))
            exits.watch_process(worker[0])
            gates["release"].release()
            assert exits.wait(10) == {worker[0]}, "setup failure did not retire worker"
            output, errors = process.communicate(timeout=10)
            assert process.returncode == 0 and errors == b"", (
                process.returncode,
                errors,
            )
            assert live_processes([worker]) == [], "setup failure did not reap worker"
            events = [json.loads(line) for line in output.splitlines()]
            assert_worker_started(events.pop(0))
            # Unstarted raw readers still drain; the unstarted sideband reader
            # contributes no Ready or other semantic frames.
            assert events[:2] == [
                {"kind": "stdout", "data": "setup stdout\n"},
                {"kind": "stderr", "data": "setup stderr\n"},
            ], events
            assert_failure_tail(
                events[2:],
                "failed to create worker stdin cancellation pipe: Input/output error (os error 5)",
                {"kind": "worker_signaled", "signal": signal.SIGKILL},
            )
            return events
        finally:
            if process.poll() is None:
                stop_process_group(process.pid)
            process.communicate(timeout=10)
            for gate in gates.values():
                gate.close()


def lifecycle_events(binary: Path, scenario: str) -> list[dict]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/relay_worker/lifecycle.py"
    with tempfile.TemporaryDirectory() as directory, Events() as exits:
        pid_path = Path(directory) / "worker-pid"
        environment = dict(
            os.environ,
            TMPDIR=directory,
            MCP_CONSOLE_HOME=directory,
            TEST_WORKER_PID=str(pid_path),
        )
        if scenario == "exit_invalid":
            environment[LOADER_VARIABLE] = str(
                build_interposer(Path(directory), "relay_drain_failure")
            )
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, fixture, scenario],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=environment,
            start_new_session=True,
        )
        try:
            assert read_relay_ready(process.stdout) == {"kind": "ready"}
            worker = capture_process_identity(int(pid_path.read_text()))
            exits.watch_process(worker[0])
            process.stdin.write(
                json.dumps(LIFECYCLE_COMMANDS[scenario]).encode() + b"\n"
            )
            process.stdin.flush()
            # Keep input open and downstream output unread until direct-worker
            # exit. Retirement must still deliver the complete sideband tail.
            assert exits.wait(10) == {worker[0]}, "worker did not retire"
            command_input = process.stdin
            process.stdin = None
            try:
                output, errors = process.communicate(timeout=10)
            finally:
                command_input.close()
            assert process.returncode == 0 and errors == b"", (
                process.returncode,
                errors,
            )
            assert live_processes([worker]) == [], "direct worker was not reaped"
            return [json.loads(line) for line in output.splitlines()]
        finally:
            if process.poll() is None:
                stop_process_group(process.pid)
            process.communicate(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
