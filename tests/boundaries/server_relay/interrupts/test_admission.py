#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    EVALUATING_NAME,
    INTERRUPT_ACK_RELEASE_NAME,
    POLL_STDIN_RECEIVED_NAME,
    ServerRelayClient,
)
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_completion_before_interrupt_ack_preserves_waiter_and_following_cell(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(
        binary, "completion_before_interrupt_ack", execution=execution
    )
    client.send(r="first cell", timeout_ms=0)
    client._wait_for(EVALUATING_NAME)
    poll_received = FifoCheckpoint.attach(
        client.relay_root() / POLL_STDIN_RECEIVED_NAME
    )
    ack_release = FifoCheckpoint.attach(
        client.relay_root() / INTERRUPT_ACK_RELEASE_NAME
    )
    released = False
    finished = False
    try:
        waiting = client.client.start_send(
            stdin="waiter owns first response\n", timeout_ms=5_000
        )
        poll_received.wait("the first cell has a response owner")
        following = client.client.start_send(
            control="interrupt", r="following cell", timeout_ms=5_000
        )
        # The real terminal receipt settles execution while acknowledgment is
        # still withheld. Only the original send collects this response.
        client.client.receive(waiting)
        assert tool_text(waiting["result"]) == "first cell completed\n"
        assert "result" not in following, following
        ack_release.release()
        released = True
        client.client.receive(following)
        assert tool_text(following["result"]) == "following cell completed\n[done]"
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            ack_release.release()
        poll_received.close()
        ack_release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {"kind": "evaluate", "language": "r", "source": "first cell"},
        {"kind": "stdin", "data": "waiter owns first response\n"},
        {"kind": "interrupt", "request_id": 0},
        {"kind": "evaluate", "language": "r", "source": "following cell"},
    ], commands
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
