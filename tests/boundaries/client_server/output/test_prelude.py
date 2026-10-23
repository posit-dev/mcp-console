#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server._harness import (
    expose_idle_sideband_output,
    submit_prompted_stdin,
    wait_for_marker,
)
from support.progress import without_elapsed_result
from support.assertions import last_tool_text
from support.checkpoints import release_fixture_checkpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import stop_process
from support.records import Transcript
from support.requirements import PROCESS_EVENTS, requires
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_demarcates_idle_prelude_across_cell_outcomes(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        passed = False
        try:
            client.initialize_and_list_tools()

            expose_idle_sideband_output(client, temporary_path, "success")
            client.send(r="echo echo")
            assert last_tool_text(client) == (
                """zod background sideband
[output produced while idle]
zod: echo
"""
            )

            expose_idle_sideband_output(client, temporary_path, "timeout")
            timed_out = client.start_send(
                r="output then complete after release",
                timeout_ms=0,
            )
            pending = wait_for_marker(
                temporary_path,
                "zod-cell-output-pending",
                client,
            )
            client.receive(timed_out)
            assert without_elapsed_result(timed_out["result"]) == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod background sideband\n\n"
                            "[running; poll with an empty send]"
                        ),
                    }
                ],
                "isError": False,
            }, timed_out

            release_fixture_checkpoint(
                pending.parent / "zod-release-cell-output", client=client
            )
            processed = wait_for_marker(
                temporary_path,
                "zod-cell-output-processed",
                client,
            )
            client.send(timeout_ms=0)
            assert without_elapsed_result(client.transcript[-1]["result"]) == {
                "content": [
                    {
                        "type": "text",
                        "text": "zod cell output before completion\n\n"
                        "[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, client.transcript[-1]

            (processed.parent / "zod-release-evaluation").touch()
            client.send()
            assert last_tool_text(client) == (
                "zod: output then complete after release\n"
            ), repr(last_tool_text(client))

            expose_idle_sideband_output(client, temporary_path, "input")
            client.send(r="request input")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                '[input requested: "zod> "]\n'
                "[waiting for stdin]"
            )
            submit_prompted_stdin(
                client,
                temporary_path,
                "answer\n",
                "zod-prompted-input-processed",
                "zod stdin: answer\n",
            )

            expose_idle_sideband_output(client, temporary_path, "language-error")
            client.send(r="language error")
            assert last_tool_text(client) == (
                "zod background sideband\n"
                "[output produced while idle]\n"
                "zod language error\n"
            )

            expose_idle_sideband_output(client, temporary_path, "replacement")
            client.send(r="exit unexpectedly")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
