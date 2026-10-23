#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.allocations import AllocationProfile
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import host_process_id
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.suites import run_this_suite


def finish_after_eof(binary: Path, execution: Execution, *, cancel: bool) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with (
            closing(FifoCheckpoint.create(root / "result-reached")) as reached,
            closing(FifoCheckpoint.create(root / "result-release")) as release,
            closing(AllocationProfile(root)) as profile,
            Events() as exits,
            McpClient(
                binary,
                execution.serve("--worker", str(worker)),
                {
                    **os.environ,
                    **profile.environment,
                    "TMPDIR": str(root),
                    "ZOD_REPORT_PID": "1",
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(release.path),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            worker_pid = host_process_id(
                int(wait_for_worker_file(root, "zod-worker-pid", client).read_text()),
                client.process.pid,
            )
            exits.watch_process(worker_pid)
            profile.pause_results(True)
            try:
                pending = client.start_send(r="echo held response")
                reached.wait("result owns delivery before journaling")
                if cancel:
                    client.notify("notifications/cancelled", requestId=pending["id"])
                    assert client.request("ping")["result"] == {}
                client.stdin.close()
                # Worker retirement proves the server observed MCP EOF while
                # this handler's result was still held outside the SDK queue.
                assert exits.wait(5) == {worker_pid}
            finally:
                profile.pause_results(False)
                release.release()
            if cancel:
                assert pending.keys() == {"id", "send"}, pending
            else:
                client.receive(pending)
                assert last_tool_text(client) == "zod: held response\n"
            return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_cancelled_result_is_not_written_after_eof(
    binary: Path, execution: Execution
) -> Transcript:
    return finish_after_eof(binary, execution, cancel=True)


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_accepted_result_is_written_after_eof(
    binary: Path, execution: Execution
) -> Transcript:
    return finish_after_eof(binary, execution, cancel=False)


if __name__ == "__main__":
    run_this_suite(__file__)
