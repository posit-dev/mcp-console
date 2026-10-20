#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.capture import read_relay_ready
from support.records import Transcript
from support.relay_commands import (
    SHUTDOWN,
    WORKER_COMMANDS,
    assert_forwarding,
    command_batch,
)
from support.requirements import POSIX, WORKER, requires
from support.suites import run_this_suite


# The fixture exchanges inherited Unix descriptors; WindowsRelay covers handles.
@requires(POSIX, WORKER)
def test_forwards_worker_commands_and_keeps_controls_local(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        marker = Path(directory) / "dispatched"
        process = subprocess.Popen(
            [
                binary,
                "worker-relay",
                sys.executable,
                fixtures / "relay_worker/commands.py",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=dict(
                os.environ,
                TMPDIR=directory,
                MCP_CONSOLE_HOME=directory,
                TEST_DISPATCHED=str(marker),
            ),
        )
        try:
            assert read_relay_ready(process.stdout) == {"kind": "ready"}
            process.stdin.write(command_batch())
            process.stdin.flush()
            receipts = [
                json.loads(line)
                for line in read_lines(
                    process.stdout,
                    len(WORKER_COMMANDS) + 1,
                    "worker receipts and interrupt acknowledgment",
                )
            ]
            output, errors = process.communicate(
                json.dumps(SHUTDOWN).encode() + b"\n", timeout=10
            )
            assert process.returncode == 0 and errors == b"", (
                process.returncode,
                errors,
            )
            tail = [json.loads(line) for line in output.splitlines()]
            return assert_forwarding(marker, receipts, tail)
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=10)


if __name__ == "__main__":
    run_this_suite(__file__)
