#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import ServerRelayClient
from support.assertions import tool_text as _tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.requirements import POSIX, requires
from support.records import Transcript
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_starts_and_reports_ready(binary: Path, execution: Execution) -> Transcript:
    client = ServerRelayClient(binary, "ready", execution=execution)
    assert _tool_text(client.send(control="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_evaluates_and_commits_operation_result(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "evaluate", execution=execution)
    assert _tool_text(client.send(r="42")) == "[done]"
    transcript = client.finish_active()
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") == "terminal_committed"
        for entry in transcript
    ), transcript
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
