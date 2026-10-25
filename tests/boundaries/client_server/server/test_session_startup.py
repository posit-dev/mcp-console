"""Connection closure owns pending discovery for every logical session."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.discovery import discovery_environment
from support.execution import DIRECT
from support.normalization import normalize_process_diagnostic, normalize_recording
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, command, requires
from support.suites import run_this_suite
from boundaries.client_server.recording.test_boundaries import await_event
from boundaries.client_server.server.test_startup import wait_for_send_admission


@requires(command("rustc"))
def test_closed_input_cancels_named_discovery(binary: Path) -> Transcript:
    with discovery_environment() as (environment, discovery):
        hidden = discovery.executable.with_name("hidden-R")
        discovery.executable.rename(hidden)
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, DIRECT.serve(), environment) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="42")
            hidden.rename(discovery.executable)
            pending = client.start_send(
                session="alpha", python="raise AssertionError('cell executed')"
            )
            discovery.wait("named runtime discovery is blocked after default is ready")
            client.stdin.close()
            client.receive(pending)
            assert pending["result"]["isError"] is True, pending
            assert "cell executed" not in str(pending["result"]), pending
            transcript = client.finish()
        return json.loads(
            normalize_process_diagnostic(json.dumps(transcript)).replace(
                str(discovery.root), "<discovery>"
            )
        )


@requires(PROCESS_EVENTS, command("rustc"))
def test_cancelled_aggregate_poll_releases_named_startup_observation(
    binary: Path,
) -> Transcript:
    with discovery_environment() as (environment, discovery):
        root = discovery.root
        hidden = discovery.executable.with_name("hidden-R")
        discovery.executable.rename(hidden)
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, DIRECT.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="42")
            hidden.rename(discovery.executable)
            accepted = client.send(session="alpha", python="42", timeout_ms=0)
            assert accepted["isError"] is False, accepted
            discovery.wait("named discovery stays blocked throughout cancellation")
            for selection in ({"session": "alpha"}, {}):
                pending = client.start_send(**selection, timeout_ms=60_000)
                wait_for_send_admission(client, session="alpha")
                client.notify("notifications/cancelled", requestId=pending["id"])
                assert client.request("ping")["result"] == {}
                call = await_event(
                    client,
                    root,
                    "tool_call",
                    lambda event: event["request_id"] == pending["id"],
                )
                # The journal confirms handler cancellation while discovery is
                # still blocked, before the next poll attempts the released claim.
                cancelled = await_event(
                    client,
                    root,
                    "tool_result",
                    lambda event: event["call_id"] == call["call_id"],
                )
                assert cancelled["error"] == {
                    "code": -32603,
                    "message": "request cancelled; poll with an empty send for any admitted cell",
                }, cancelled
                observed = client.send(session="alpha", timeout_ms=0)
                assert observed["isError"] is False, observed
                assert observed["content"][0]["text"].endswith(
                    "[running; poll with an empty send]"
                ), observed
            return normalize_recording(client.finish(), root)


if __name__ == "__main__":
    run_this_suite(__file__)
