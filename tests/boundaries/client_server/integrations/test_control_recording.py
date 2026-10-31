"""Integrator projection discovery follows recording and worker ownership."""

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from boundaries.client_server.python.test_without_r import selected_environment
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT)
def test_discovers_current_projections_without_consuming_output(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-export-") as directory:
        root = Path(directory).resolve()
        (root / "python3").symlink_to(sys.executable)
        project_recordings = root / ".agents/console/sessions"
        project_recordings.mkdir(parents=True)
        endpoint = root / "control.sock"
        with (
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                execution.serve("--control-socket", str(endpoint)),
                selected_environment(root),
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="visible_source = 42")
            with (
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                first = control.request(op="recording")["result"]
                recording = Path(first["directory"])
                assert (
                    recording.is_absolute() and recording.parent == project_recordings
                )
                assert Path(first["markdown"]) == recording / "transcript.md"
                assert Path(first["qmd"]) == recording / "sessions/session-001.qmd"
                assert first["worker_id"] == 1
                assert "visible_source = 42" in Path(first["qmd"]).read_text()
                hidden = control.request(
                    op="evaluate",
                    language="python",
                    code="hidden_inspection = 1",
                    record=False,
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(hidden) == "[done]", hidden
                assert "hidden_inspection" not in Path(first["qmd"]).read_text()
                assert "hidden_inspection" not in Path(first["markdown"]).read_text()

                observer.request(op="subscribe", format="events")
                json.loads(observer.reader.readline(timeout=30))
                # fmt: python
                source = code(f"""
                    print("pending export output", flush=True)
                    with open({str(release.path)!r}, "rb", buffering=0) as gate:
                        gate.read(1)
                    """)
                client.send(python=source, timeout_ms=0)
                while True:
                    snapshot = json.loads(observer.reader.readline(timeout=30))[
                        "result"
                    ]
                    if any(
                        "pending export output" in event.get("text", "")
                        for event in snapshot["events"]
                    ):
                        break
                assert control.request(op="recording")["result"] == first
                release.release()
                collected = client.expect(None)
                assert "pending export output" in "".join(
                    block.get("text", "") for block in collected["content"]
                )

                control.request(op="restart")
                replacement = control.request(op="recording")["result"]
                assert replacement == {**first, "qmd": None, "worker_id": 2}
                client.expect(None)
                client.expect(python="replacement_source = 43")
                latest = control.request(op="recording")["result"]
                assert Path(latest["qmd"]) == recording / "sessions/session-002.qmd"
                assert "visible_source" not in Path(latest["qmd"]).read_text()
                assert "replacement_source = 43" in Path(latest["qmd"]).read_text()
                assert "visible_source" in Path(latest["markdown"]).read_text()
                recorded = normalize_recording(client.finish(), root)
            # The run ID is incidental; verify the actual paths before normalizing.
            companion = normalize_recording(control.transcript, root)
            return TranscriptWithCompanions(recorded, {"control.yaml": companion})


if __name__ == "__main__":
    run_this_suite(__file__)
