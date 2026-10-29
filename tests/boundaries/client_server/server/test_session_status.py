#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server._harness import expose_idle_input_request
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, requires
from support.suites import run_this_suite


@requires(PROCESS_EVENTS)
@executions(RUNTIME)
def test_lists_input_requested_outside_a_cell(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {**os.environ, "TMPDIR": temporary}
        with McpClient(
            binary, execution.serve("--worker", str(zod)), environment, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Session listing reports an idle worker's input request.",
                    "fixture": "The fake worker acknowledges cell completion before requesting input; its sideband receipt proves the server processed that request.",
                }
            )
            client.initialize_and_list_tools()
            expose_idle_input_request(client, root)
            waiting = client.send(timeout_ms=0)
            assert waiting["structuredContent"] == {
                "sessions": [{"session": "default", "status": "waiting_for_stdin"}]
            }, waiting
            assert last_result_text(client) == (
                '[session: "default"; status: waiting_for_stdin]\n\n[waiting for stdin]'
            ), waiting
            client.expect("zod: echo\n", r="echo echo", stdin="continue\n")
            result = client.send(timeout_ms=0)
            assert (
                last_result_text(client)
                == '[session: "default"; status: idle]\n\n[idle]'
            ), result
            assert result["structuredContent"] == {
                "sessions": [{"session": "default", "status": "idle"}]
            }, result
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
