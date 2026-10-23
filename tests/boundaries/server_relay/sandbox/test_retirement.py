#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import ServerRelayClient
from support.execution import SANDBOXED
from support.records import Transcript
from support.requirements import SANDBOX, requires, POSIX
from support.suites import run_this_suite


@requires(POSIX)
@requires(SANDBOX)
def test_rejects_unsolicited_status_137_after_fatal(binary: Path) -> Transcript:
    client = ServerRelayClient(binary, "fatal_status_137", execution=SANDBOXED)
    failed = client.client.start_send(r="42")
    transcript = client.release_terminal_failure(failed, "scripted relay failure")
    output = failed["result"]["content"][0]["text"]
    assert "worker launcher exited with status 137" in output, output
    assert "[starting new worker]" not in output, output
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
