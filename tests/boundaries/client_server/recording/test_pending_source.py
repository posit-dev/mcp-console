"""Source ownership when a response precedes actual worker launch."""

import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.previews import live_recording_events
from support.execution import DIRECT, Execution, executions
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT)
def test_retains_source_after_an_early_response(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        workspace = Path(directory)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "release"))
        )
        environment = {
            **os.environ,
            "MCP_CONSOLE_TEST_REAL_BINARY": str(binary),
            "MCP_CONSOLE_TEST_RELAY_REACHED": str(reached.path),
            "MCP_CONSOLE_TEST_RELAY_RELEASE": str(release.path),
        }
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "delayed_relay"),
            ),
            environment,
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "get"})
            try:
                result = client.send(r="echo delayed source", timeout_ms=0)
                reached.wait("evaluation admitted before relay and worker launch")
                assert not result.get("isError"), result
                (recording,) = (workspace / ".agents/console/sessions").iterdir()
                events = live_recording_events(recording)
                assert not any(
                    event["event"] == "worker_started" for event in events
                ), events
            finally:
                release.release()
            client.expect("zod: delayed source\n")
            source = recording / "sessions/session-001.qmd"
            assert source.is_file(), "accepted source was lost after its early response"
            assert "echo delayed source" in source.read_text()
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
