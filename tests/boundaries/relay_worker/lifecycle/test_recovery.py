#!/usr/bin/env -S uv run --script

import json
import os
import select
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.relay_worker._harness import RelayWorkerClient
from support.assertions import tool_text as _tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.capture import assert_worker_started
from support.records import Transcript
from support.requirements import POSIX, WORKER, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_closes_each_pipe_direction_on_restart(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = RelayWorkerClient(
        binary,
        worker_command=(str(worker),),
        capture_stdin_close=True,
        capture_worker_sideband_close=True,
        execution=execution,
    )
    # Observe both pipe closures with the protocol fixture. R finalizers can
    # exhaust the shutdown grace, which permits forced retirement without a
    # sideband acknowledgment; the restart case checks R state separately.
    assert _tool_text(client.send(r="echo ready")) == "zod: ready\n"
    old_path, old_capture = client._open_capture()
    result = _tool_text(client.send(control="restart"))
    assert result == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    ), result
    assert _tool_text(client.send(r="echo replacement ready")) == (
        "zod: replacement ready\n"
    )
    transcript = client._finish_replacement(old_path, old_capture)
    assert {"stdin": {"closed": True}} in transcript, transcript
    assert {"worker_sideband": {"closed": True}} in transcript, transcript
    return transcript


@requires(POSIX)
@requires(WORKER)
def test_closes_pipes_with_unread_shutdown(
    binary: Path,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        process = subprocess.Popen(
            [binary, "worker-relay", zod],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # select observes the pipe, so readline must not hide the following
            # event in a TextIOWrapper's read-ahead buffer.
            bufsize=0,
            start_new_session=True,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None

        def receive() -> dict[str, object]:
            readable, _, _ = select.select([process.stdout], [], [], 10)
            assert readable, "worker relay did not emit an event"
            line = process.stdout.readline()
            assert line, "worker relay closed its event stream"
            event = json.loads(line)
            assert isinstance(event, dict), event
            return event

        def send(message: dict[str, object]) -> None:
            process.stdin.write(
                (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            )
            process.stdin.flush()

        passed = False
        try:
            assert_worker_started(receive())
            events = [receive()]
            assert events == [{"kind": "ready"}], events
            send(
                {
                    "kind": "evaluate",
                    "language": "r",
                    "source": "close sideband with unread shutdown",
                }
            )
            events.append(receive())
            assert events[-1] == {"kind": "cell_started"}, events
            events.append(receive())
            assert events[-1] == {
                "kind": "console_output",
                "data": "zod waiting for shutdown\n",
            }, events
            send({"kind": "shutdown", "grace_millis": 5000})

            deadline = time.monotonic() + 10
            while process.poll() is None:
                remaining = deadline - time.monotonic()
                assert remaining > 0, events
                readable, _, _ = select.select([process.stdout], [], [], remaining)
                assert readable, events
                line = process.stdout.readline()
                if line:
                    event = json.loads(line)
                    assert isinstance(event, dict), event
                    events.append(event)
            events.extend(json.loads(line) for line in process.stdout)
            standard_error = process.stderr.read().decode("utf-8")

            assert process.returncode == 0, standard_error
            assert standard_error == ""
            assert not any(event.get("kind") == "fatal" for event in events), events
            assert events[-2:] == [
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ], events
            passed = True
            return events
        finally:
            if not passed:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.wait(timeout=5)
            process.stdin.close()
            process.stdout.close()
            process.stderr.close()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_recovers_after_worker_segfault(
    binary: Path, execution: Execution
) -> Transcript:
    # Disable R's fatal-signal UI so the native fault terminates the worker directly.
    client = RelayWorkerClient(
        binary,
        capture_worker_sideband_close=True,
        disable_r_segv_handler=True,
        execution=execution,
    )
    # fmt: r
    before_crash = code(r"""
        crash_marker <- "old generation"
        cat("before crash\n")
        """)
    assert _tool_text(client.send(r=before_crash)) == "before crash\n"
    old_path, old_capture = client._open_capture()

    # fmt: python
    crash = code(r"""
        import ctypes

        ctypes.string_at(0)
        """)
    result = client.send(python=crash)
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 245]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    ), repr(result["content"][0]["text"])

    # fmt: r
    after_crash = code(r"""
        stopifnot(!exists("crash_marker", inherits = FALSE))
        cat("after crash\n")
        """)
    assert _tool_text(client.send(r=after_crash)) == "after crash\n"

    transcript = client._finish_replacement(old_path, old_capture)
    assert {"worker_sideband": {"closed": True}} in transcript
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
