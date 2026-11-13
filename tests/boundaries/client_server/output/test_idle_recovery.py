"""Cancelled idle polls retain pending input prompts until delivery."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server._harness import wait_for_marker
from boundaries.client_server.output.test_recovery import cancel_result, recovery_client
from support.checkpoints import release_fixture_checkpoint
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES, POSIX, PROCESS_EVENTS)
def test_cancelled_idle_input_poll_retains_prompt(binary: Path) -> Transcript:
    with recovery_client(binary) as (client, profile, reached, release):
        client.transcript.insert(
            0,
            {
                "scenario": "Cancelling an empty poll preserves an input prompt requested outside a cell.",
                "fixture": "The fake worker completes its cell before requesting input. A sideband receipt proves the server processed the prompt before the response gate pauses delivery.",
            },
        )
        profile.pause_results(False)
        requested = client.start_send(r="request input while idle")
        completed = wait_for_marker(
            reached.path.parent, "zod-idle-input-cell-completed", client
        )
        client.receive(requested)
        assert requested["result"] == {
            "content": [{"type": "text", "text": "[done]"}],
            "isError": False,
        }, requested
        release_fixture_checkpoint(
            completed.parent / "zod-release-idle-input-request", client=client
        )
        wait_for_marker(reached.path.parent, "zod-idle-input-request-processed", client)

        profile.pause_results(True)
        pending = client.start_send(timeout_ms=0)
        cancel_result(client, pending, reached, release)
        profile.pause_results(False)
        result = client.send(session="default", timeout_ms=0)
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": '[input requested: "idle> "]\n'
                    "[waiting for stdin]\n[waiting for stdin]",
                }
            ],
            "isError": False,
        }, result
        assert client.send(timeout_ms=0) == {
            "content": [{"type": "text", "text": "\n[waiting for stdin]"}],
            "isError": False,
        }
        client.expect("zod: echo\n", r="echo echo", stdin="continue\n")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
