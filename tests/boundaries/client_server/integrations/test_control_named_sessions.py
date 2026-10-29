"""The integrator socket retains its default-session scope beside named workers."""

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
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.previews import recording_events
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_control_keeps_default_state_and_recording_while_named_session_runs(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-named-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
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
            client.expect(python="shared = 40")
            # fmt: python
            named_source = code(f"""
                shared = 100
                print("alpha pending", flush=True)
                with open({str(release.path)!r}, "rb", buffering=0) as gate:
                    _ = gate.read(1)
                print(shared)
                """)
            pending = client.start_send(
                session="alpha", python=named_source, timeout_ms=60000
            )
            try:
                recordings = root / ".agents/console/sessions"
                wait_for_checkpoint(
                    lambda: next(
                        (
                            path
                            for path in recordings.glob(
                                "*/capture/sessions/*/cell-*.txt"
                            )
                            if path.read_text() == "alpha pending\n"
                        ),
                        None,
                    ),
                    "named output captured while the default session stays idle",
                    root=recordings,
                    recursive=True,
                    client=client,
                )
                with ControlClient(endpoint) as control:
                    status = control.request(op="status")
                    assert status["result"] == {
                        "state": "idle",
                        "idle": True,
                        "evaluation": None,
                    }, status
                    preview = control.request(op="preview")
                    assert result_text(preview) == "", preview
                    integrator_source = "shared += 2; print(shared)"
                    result = control.request(
                        op="evaluate", language="python", code=integrator_source
                    )
                    assert result_text(result) == "42\n", result
                    release.release()
                    client.receive(pending)
                    assert result_text(pending) == "alpha pending\n100\n", pending
                    client.expect("42\n", python="print(shared)")
                    client.expect("100\n", session="alpha", python="print(shared)")
                    transcript = normalize_recording(client.finish(), root)
            finally:
                release.release()

        (recording,) = recordings.iterdir()
        events = recording_events(recording)
        workers = {
            event.get("session", "default"): event["session_id"]
            for event in events
            if event["event"] == "worker_started"
        }
        assert workers.keys() == {"default", "alpha"}, workers
        for session, source, expected in (
            ("default", integrator_source, "42\n"),
            ("alpha", named_source, "alpha pending\n100\n"),
        ):
            (call,) = [
                event
                for event in events
                if event["event"] == "tool_call"
                and event["request"]["arguments"].get("python") == source
            ]
            assert call.get("origin") == (
                "integrator" if session == "default" else None
            ), call
            (cell,) = [
                event
                for event in events
                if event["event"] == "cell_started"
                and event["call_id"] == call["call_id"]
            ]
            assert cell["owner"]["session_id"] == workers[session], cell
            (output,) = [
                event
                for event in events
                if event["event"] == "cell_output"
                and event["call_id"] == call["call_id"]
            ]
            assert output["owner"] == cell["owner"], output
            assert (recording / output["path"]).read_text() == expected, output
        return TranscriptWithCompanions(
            transcript, {"control.yaml": control.transcript}
        )


if __name__ == "__main__":
    run_this_suite(__file__)
