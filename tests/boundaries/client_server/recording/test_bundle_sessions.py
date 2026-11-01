"""Portable reports preserve journal notes and logical session ownership."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from boundaries.client_server.python.test_startup import selected_python
from boundaries.client_server.recording.test_intervals import events
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, Execution, executions
from support.normalization import normalize_recording
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@executions(RUNTIME)
def test_bundles_keep_notes_without_source_and_in_call_order(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary,
            execution.serve(),
            selected_python(root, Path(sys.executable)),
            root,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "worker acquired before the journal note")
            client.expect(note="First record the question.")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            bundle = recording / "sessions/session-001"
            for filename in ("session.qmd", "session.md"):
                document = (bundle / filename).read_text()
                assert document.count("First record the question.") == 1, document
            assert len(client.bundle_notices) == 1, client.bundle_notices
            client.expect(
                "measurement\n",
                note="Then measure the result.",
                python="print('measurement')",
            )
            client.expect(note="Finally explain the measurement.")
            for filename in ("session.qmd", "session.md"):
                document = (bundle / filename).read_text()
                fragments = [
                    "First record the question.",
                    "Then measure the result.",
                    "print('measurement')",
                    "Finally explain the measurement.",
                ]
                offsets = [document.index(fragment) for fragment in fragments]
                assert offsets == sorted(offsets), document
                assert all(document.count(fragment) == 1 for fragment in fragments)
            return normalize_recording(client.finish(), root)


@executions(RUNTIME)
def test_named_bundles_keep_pending_notes_and_rejected_results(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary,
            execution.serve(),
            selected_python(root, Path(sys.executable)),
            root,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "default worker ready before named workers")
            client.expect(note="Beta's question before acquisition.", session="beta")
            for session in ("alpha", "beta"):
                client.expect(
                    f"{session}\n", python=f"print('{session}')", session=session
                )
            rejected = client.send(requirements={}, session="alpha")
            assert rejected["isError"], rejected
            error = rejected["content"][0]["text"]
            transcript = client.finish()

        (recording,) = (root / ".agents/console/sessions").iterdir()
        workers = {
            event.get("session", "default"): event["session_id"]
            for event in events(recording)
            if event["event"] == "worker_started"
        }
        for session in ("alpha", "beta"):
            bundle = recording / f"sessions/session-{workers[session]:03}"
            report = (bundle / "session.md").read_text()
            assert (error in report) is (session == "alpha"), report
            for filename in ("session.qmd", "session.md"):
                document = (bundle / filename).read_text()
                assert ("Beta's question before acquisition." in document) is (
                    session == "beta"
                ), document
                assert f"print('{session}')" in document, document
                other = "beta" if session == "alpha" else "alpha"
                assert f"print('{other}')" not in document, document
        return normalize_recording(transcript, root)


@requires(POSIX)
@executions(DIRECT)
def test_control_created_bundle_is_announced_to_mcp(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-bundle-") as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            selected_python(root, Path(sys.executable)),
            root,
        ) as client:
            client.initialize_and_list_tools()
            with ControlClient(endpoint) as control:
                result = control.request(
                    op="evaluate",
                    language="python",
                    code="print('created through control')",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result["result"]["execution_error"] is None, result
                projection = control.request(op="recording")["result"]
                bundle = Path(projection["qmd"]).parent
                assert "created through control" in (bundle / "session.md").read_text()
                assert client.bundle_notices == []
            client.expect("after control\n", python="print('after control')")
            assert len(client.bundle_notices) == 1, client.bundle_notices
            assert str(bundle) in client.bundle_notices[0], client.bundle_notices
            return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT)
def test_hidden_integrator_cells_stay_out_of_bundles(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-bundle-") as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            selected_python(root, Path(sys.executable)),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("visible\n", python="print('visible')")
            with ControlClient(endpoint) as control:
                hidden = control.request(
                    op="evaluate",
                    language="python",
                    code="print('hidden measurement')",
                    record=False,
                    busy="wait",
                    wait_ms=30000,
                )
                assert hidden["result"]["execution_error"] is None, hidden
                projection = control.request(op="recording")["result"]
                bundle = Path(projection["qmd"]).parent
                assert bundle.name == "session-001", projection
                for filename in ("session.qmd", "session.md"):
                    document = (bundle / filename).read_text()
                    assert "print('visible')" in document, document
                    assert "hidden measurement" not in document, document
                report = (bundle / "session.md").read_text()
                assert "## Cell 002" not in report, report
            return normalize_recording(client.finish(), root)


if __name__ == "__main__":
    run_this_suite(__file__)
