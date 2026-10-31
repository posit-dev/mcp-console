"""Journal latency must not change the worker addressed by a standalone note."""

import os
import signal
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_intervals import events
from support.allocations import AllocationProfile
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT
from support.normalization import normalize_recording
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, requires


@requires(NATIVE_FIXTURES)
def test_standalone_note_keeps_received_worker_during_delayed_capture(
    binary: Path,
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    note = "The first worker has completed its baseline measurement."
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (
            closing(FifoCheckpoint.create(root / "result-reached")) as reached,
            closing(FifoCheckpoint.create(root / "result-release")) as release,
            closing(FifoCheckpoint.create(root / "dispatcher-exited")) as retired,
            closing(AllocationProfile(root)) as profile,
            McpClient(
                binary,
                DIRECT.serve("--worker", str(worker)),
                {
                    **os.environ,
                    **profile.environment,
                    "TMPDIR": str(root),
                    "ZOD_REPORT_PID": "1",
                    "MCP_CONSOLE_TEST_RESULT_REACHED": str(reached.path),
                    "MCP_CONSOLE_TEST_RESULT_RELEASE": str(release.path),
                    "MCP_CONSOLE_TEST_DISPATCHER_EXITED": str(retired.path),
                    "MCP_CONSOLE_TEST_DISPATCHER_ARMED": str(root / "armed"),
                },
                root,
            ) as client,
        ):
            try:
                client.initialize_and_list_tools()
                client.expect("zod: original\n", r="echo original")
                original_pid = int((root / "zod-worker-pid").read_text())
                profile.pause_results(True)
                pending = client.start_send(control="interrupt")
                reached.wait("result writer holds the journal before note receipt")
                annotation = client.start_send(note=note)
                # The ping follows the note on the same MCP transport. Receipt
                # therefore precedes retirement even though capture is blocked.
                assert client.request("ping")["result"] == {}
                (root / "armed").touch()
                os.kill(original_pid, signal.SIGKILL)
                retired.wait(
                    "worker retirement queued while note capture is blocked",
                    client=client,
                )
                profile.pause_results(False)
                release.release()
                client.receive(pending)
                assert tool_text(pending["result"]) == "\n[idle]", pending
                client.receive(annotation)
                assert tool_text(annotation["result"]) == "[done]", annotation
                failure = client.send()
                assert failure["isError"], failure
                client.expect(
                    "[starting new worker]\nzod: replacement\n",
                    r="echo replacement",
                )
                transcript = client.finish()
            finally:
                profile.pause_results(False)
                release.release()

        (recording,) = (root / ".agents/console/sessions").iterdir()
        capture = events(recording)
        received = next(
            event
            for event in capture
            if event["event"] == "protocol"
            and event["direction"] == "received"
            and event["message"].get("id") == annotation["id"]
        )
        retired = next(
            event
            for event in capture
            if event["event"] == "worker_ended" and event["session_id"] == 1
        )
        captured = next(
            event
            for event in capture
            if event["event"] == "tool_call"
            and event["request"]["arguments"].get("note") == note
        )
        assert received["sequence"] < retired["sequence"] < captured["sequence"], (
            capture
        )
        assert captured["note_target"] == {"kind": "worker", "session_id": 1}, captured
        original = (recording / "sessions/session-001.qmd").read_text()
        replacement = (recording / "sessions/session-002.qmd").read_text()
        assert original.count(note) == 1, original
        assert note not in replacement, replacement
        return normalize_recording(transcript, root)
