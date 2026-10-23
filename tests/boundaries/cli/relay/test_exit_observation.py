#!/usr/bin/env -S uv run --script

import json
import os
import re
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.processes import stop_process_group
from support.capture import assert_worker_started
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES)
def test_force_stop_settles_observation_before_reaping(binary: Path) -> Transcript:
    return observe_relay(binary, fail=False)


@requires(NATIVE_FIXTURES)
def test_observation_failure_still_stops_and_reaps_worker(binary: Path) -> Transcript:
    return observe_relay(binary, fail=True)


@requires(NATIVE_FIXTURES)
def test_observation_and_probe_failure_still_stops_and_reaps_worker(
    binary: Path,
) -> Transcript:
    return observe_relay(binary, fail=True, fail_probe=True)


def observe_relay(binary: Path, *, fail: bool, fail_probe: bool = False) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        worker_pid = root / "worker-pid"
        checkpoints = {
            name: FifoCheckpoint.create(root / name)
            for name in ("entered", "release", "killed")
        }
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(root, "child_exit_observation")),
            "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
            "MCP_CONSOLE_TEST_OBSERVER_PID": str(worker_pid),
            "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
            "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
            "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
        }
        if fail:
            environment["MCP_CONSOLE_TEST_OBSERVER_FAIL"] = "1"
        else:
            environment["MCP_CONSOLE_TEST_OBSERVER_EINTR"] = "1"
        if fail_probe:
            environment["MCP_CONSOLE_TEST_OBSERVER_PROBE_FAIL"] = "1"
        worker = root / "worker.py"
        worker.write_text(
            # fmt: python
            code(r"""
                import os
                import signal

                os.write(1, b"final worker output\n")
                os.write(int(os.environ["MCP_CONSOLE_SIDEBAND_WRITE_FD"]), b'{"kind":"ready"}\n')
                signal.pause()
                """)
        )
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, worker],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
            start_new_session=True,
        )
        try:
            assert process.stdin is not None and process.stdout is not None
            initial = [
                json.loads(line)
                for line in read_lines(
                    process.stdout, 3, "worker launch, ready and final output"
                )
            ]
            assert_worker_started(initial.pop(0))
            assert {event["kind"] for event in initial} == {"ready", "stdout"}, initial
            checkpoints["entered"].wait("direct-child observation entered")
            if fail:
                checkpoints["release"].release()
                checkpoints["killed"].wait("failed observation stops the worker")
            else:
                process.stdin.write('{"kind":"shutdown","grace_millis":0}\n')
                process.stdin.flush()
                checkpoints["killed"].wait(
                    "force stop terminates before observation settles"
                )
                checkpoints["release"].release()
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, stderr
            assert stderr == "", stderr
            assert not (root / "early-reap").exists(), (
                "reaped before observation settled"
            )
            events = [json.loads(line) for line in stdout.splitlines()]
            expected = [{"kind": "stdout_closed"}, {"kind": "stderr_closed"}]
            if not fail:
                expected.insert(0, {"kind": "shutdown_started"})
            if fail:
                events[2]["message"] = re.sub(
                    r"child process \d+", "child process PID", events[2]["message"]
                )
                message = "failed to observe child process PID exit: Input/output error (os error 5)"
                if fail_probe:
                    message += "; failed to read direct worker status: Input/output error (os error 5)"
                expected.append(
                    {
                        "kind": "fatal",
                        "message": message,
                    }
                )
            expected.extend(
                [
                    {"kind": "worker_sideband_closed"},
                    {"kind": "worker_signaled", "signal": signal.SIGKILL},
                ]
            )
            assert events == expected, events
            assert next(event for event in initial if event["kind"] == "stdout") == {
                "kind": "stdout",
                "data": "final worker output\n",
            }
            return [
                {
                    "observation": (
                        "failed with failed status probe"
                        if fail_probe
                        else "failed"
                        if fail
                        else "exited after EINTR"
                    ),
                    "events": events,
                }
            ]
        finally:
            try:
                with Events() as exits:
                    watched = None
                    if worker_pid.exists():
                        pid = int(worker_pid.read_text())
                        try:
                            exits.watch_process(pid)
                        except ProcessLookupError:
                            pass
                        else:
                            watched = pid
                    # The owned group can outlive the relay that leads it.
                    stop_process_group(process.pid)
                    if watched is not None:
                        assert exits.wait(10) == {watched}, (
                            "worker outlived fixture cleanup"
                        )
            finally:
                process.wait(timeout=10)
                for checkpoint in checkpoints.values():
                    checkpoint.close()


if __name__ == "__main__":
    run_this_suite(__file__)
