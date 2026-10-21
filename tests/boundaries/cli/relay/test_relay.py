#!/usr/bin/env -S uv run --script

import json
import os
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    WORKER,
    requires,
)
from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    capture_process_identity,
    kill_processes,
    live_processes,
)
from support.native import SHARED_LIBRARY_FLAG
from support.native import LOADER_VARIABLE
from support.native import build_interposer
from support.normalization import code
from support.capture import read_relay_ready
from support.records import Transcript
from support.suites import run_this_suite


@requires(WORKER, NATIVE_FIXTURES)
def test_consumes_startup_environment_before_forwarding(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        observations = root / "relay-environment"
        payload = json.dumps(
            {"language": "python", "code": "password = 'startup-token'"}
        )
        transport = root / "startup.json"
        transport.write_text(payload)
        transport.chmod(0o600)
        environment = os.environ | {
            "TMPDIR": str(root),
            "MCP_CONSOLE_STARTUP_FILE": str(transport),
            "MCP_CONSOLE_TEST_STARTUP_ENVIRONMENT": str(observations),
            LOADER_VARIABLE: str(build_interposer(root, "relay_startup_environment")),
        }
        # The independent child consumes the file passed at the launch seam.
        # fmt: python
        source = code("""
            import os
            import stat
            from pathlib import Path

            transport = Path(os.environ.pop("MCP_CONSOLE_STARTUP_FILE"))
            assert stat.S_IMODE(transport.stat().st_mode) == 0o600
            Path(os.environ["TMPDIR"]).joinpath("forwarded").write_text(transport.read_text())
            transport.unlink()
            """)
        process = subprocess.Popen(
            [binary, "worker-relay", sys.executable, "-c", source],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        # Keep command input live until natural worker exit retires the relay.
        command_input = process.stdin
        process.stdin = None
        try:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, (stdout, stderr)
            assert stderr == "", stderr
            events = [json.loads(line) for line in stdout.splitlines()]
            assert {"kind": "worker_exited", "code": 0} in events, events
            assert (root / "forwarded").read_text() == payload
            assert not transport.exists()
            observed = observations.read_bytes()
            assert observed and set(observed) == {ord("0")}, observed
        finally:
            assert command_input is not None
            command_input.close()
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    return [{"relay_environment_consumed": True, "worker_payload_preserved": True}]


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_retires_while_descendant_keeps_refilling_output(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    transcript: Transcript = []
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        interposer = temporary / "relay-refill.dylib"
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
                fixtures / "native" / "relay_refill_interposer.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        for stream in ("stdout", "stderr", "sideband"):
            root = temporary / stream
            root.mkdir()
            checkpoints = {
                name: FifoCheckpoint.create(root / name)
                for name in (
                    "refill-request",
                    "refill-done",
                    "refill-observed",
                    "worker-exit",
                )
            }
            environment = os.environ.copy()
            environment["TMPDIR"] = str(root)
            environment[LOADER_VARIABLE] = str(interposer)
            environment["MCP_CONSOLE_TEST_REFILL_MATCH"] = (
                '{"kind":"console_output","data":"descendant output\\n"}\n'
                if stream == "sideband"
                else "descendant output\n"
            )
            for name in ("request", "done", "observed"):
                environment[f"MCP_CONSOLE_TEST_REFILL_{name.upper()}"] = str(
                    root / f"refill-{name}"
                )
            process = subprocess.Popen(
                [
                    binary,
                    "worker-relay",
                    sys.executable,
                    fixtures / "relay_worker" / "refilling_descendant.py",
                    stream,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                env=environment,
                start_new_session=True,
            )
            descendant = None
            try:
                assert process.stdin is not None and process.stdout is not None
                assert read_relay_ready(process.stdout) == {"kind": "ready"}
                process.stdin.write(
                    json.dumps({"kind": "stdin", "data": "start\n"}) + "\n"
                )
                process.stdin.flush()
                checkpoints["refill-observed"].wait(
                    "descendant output was read and refilled"
                )
                descendant = capture_process_identity(
                    int((root / "descendant-pid").read_text())
                )
                checkpoints["worker-exit"].release()
                # communicate drains output while enforcing a failure deadline.
                # Keep command input live: natural worker exit triggers retirement.
                command_input = process.stdin
                process.stdin = None
                try:
                    stdout, stderr = process.communicate(timeout=5)
                finally:
                    command_input.close()
                assert process.returncode == 0, stderr
                assert stderr == "", stderr
                events = [json.loads(line) for line in stdout.splitlines()]
                output_kind = "console_output" if stream == "sideband" else stream
                outputs = [event for event in events if event["kind"] == output_kind]
                assert outputs, events
                assert all(
                    event == {"kind": output_kind, "data": "descendant output\n"}
                    for event in outputs
                ), outputs
                terminal_events = [
                    event for event in events if event["kind"] != output_kind
                ]
                assert terminal_events == [
                    {"kind": "stdout_closed"},
                    {"kind": "stderr_closed"},
                    {"kind": "worker_sideband_closed"},
                    {"kind": "worker_exited", "code": 0},
                ], terminal_events
                assert events == outputs + terminal_events, events
                assert live_processes([descendant]) == [descendant[0]]
                # The deadline makes the repetition count incidental. Retain
                # its exact payload and the complete terminal protocol suffix.
                transcript.append(
                    {
                        "refilled_stream": stream,
                        "repeated_output": outputs[0],
                        "events": terminal_events,
                    }
                )
            finally:
                # Before descendant capture, no wait has reaped the group
                # leader. Retire that group before poll can release its PID.
                if descendant is None or process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                if descendant is not None:
                    kill_processes([descendant])
                process.communicate(timeout=10)
                for checkpoint in checkpoints.values():
                    checkpoint.close()
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_relay_protocol_is_independent_of_sandbox_launch(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    transcript: Transcript = []
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        command = execution.command(binary, "worker-relay", sys.executable, str(worker))
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=environment,
        )
        try:
            if execution == DIRECT:
                assert os.getpgid(process.pid) == os.getpgrp()
            assert process.stdin is not None and process.stdout is not None
            events = [read_relay_ready(process.stdout)]
            assert events == [{"kind": "ready"}], events
            evaluate = {"kind": "evaluate", "language": "r", "source": "echo hello"}
            process.stdin.write(json.dumps(evaluate) + "\n")
            process.stdin.flush()
            events.extend(
                json.loads(line)
                for line in read_lines(
                    process.stdout, 4, "cell start and evaluation completion"
                )
            )
            shutdown = {"kind": "shutdown", "grace_millis": 1000}
            stdout, stderr = process.communicate(
                json.dumps(shutdown) + "\n", timeout=10
            )
            events.extend(json.loads(line) for line in stdout.splitlines())
            assert process.returncode == 0, stderr
            assert stderr == "", stderr
            assert events == [
                {"kind": "ready"},
                {"kind": "cell_started"},
                {"kind": "console_output", "data": "zod: "},
                {"kind": "console_output", "data": "hello\n"},
                {"kind": "completed"},
                {"kind": "shutdown_started"},
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ], events
            transcript.append({"events": events})
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
