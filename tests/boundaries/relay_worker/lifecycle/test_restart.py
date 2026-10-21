#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.relay_worker._harness import RelayWorkerClient
from support.assertions import tool_text as _tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.requirements import POSIX, requires
from support.records import Transcript
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restarts_session(binary: Path, execution: Execution) -> Transcript:
    client = RelayWorkerClient(
        binary,
        capture_stdin_close=True,
        capture_worker_sideband_close=True,
        execution=execution,
    )
    # fmt: r
    before_restart = code(r"""
        restart_marker <- "old generation"
        cat("before restart\n")
        """)
    assert _tool_text(client.send(r=before_restart)) == "before restart\n"
    old_path, old_capture = client._open_capture()

    assert _tool_text(client.send(control="restart")) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    # fmt: r
    after_restart = code(r"""
        stopifnot(!exists("restart_marker", inherits = FALSE))
        cat("after restart\n")
        """)
    assert _tool_text(client.send(r=after_restart)) == "after restart\n"

    transcript = client._finish_replacement(old_path, old_capture)
    assert {"stdin": {"closed": True}} in transcript, transcript
    assert {"worker_sideband": {"closed": True}} in transcript, transcript
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
