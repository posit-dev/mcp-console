"""Immediate requests rejected during restart retain a retryable admission result."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server._harness import wait_for_marker
from boundaries.client_server.integrations.test_control import ControlClient
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, PROCESS_EVENTS, requires
from support.suites import run_this_suite


@requires(POSIX, PROCESS_EVENTS)
@executions(DIRECT, SANDBOXED)
def test_immediate_requests_are_busy_until_replacement_is_ready(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-restart-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        startup_control = root / "startup-control"
        startup_release = root / "startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "ZOD_STARTUP_CONTROL": str(startup_control),
            "ZOD_STARTUP_RELEASE": str(startup_release),
        }
        with McpClient(
            binary,
            execution.serve("--worker", str(zod), "--control-socket", str(endpoint)),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("zod: ready\n", r="echo ready")
            startup_control.write_text("block", encoding="utf-8")
            restart = client.start_send(control="restart")
            try:
                # The replacement's explicit readiness gate keeps lifecycle in
                # Restarting while the admission locks are available to callers.
                wait_for_marker(root, "zod-replacement-waiting-ready", client)
                with ControlClient(endpoint) as control:
                    state = control.request(op="status")["result"]
                    assert state == {
                        "state": "restarting",
                        "idle": False,
                        "evaluation": None,
                    }, state
                    for language, source in (
                        ("r", "stop('must not be dispatched')"),
                        ("python", "raise AssertionError('must not be dispatched')"),
                    ):
                        rejected = control.request(
                            op="evaluate",
                            language=language,
                            execution="immediate",
                            code=source,
                            record=False,
                        )
                        assert rejected["error"] == {
                            "code": "busy",
                            "message": "runner is busy; code was not run",
                        }, rejected
                    startup_release.touch()
                    client.receive(restart)
                    client.expect("zod: usable\n", r="echo usable")
                    transcript = normalize_recording(client.finish(), root)
                return TranscriptWithCompanions(
                    transcript, {"control.yaml": control.transcript}
                )
            finally:
                startup_release.touch()


if __name__ == "__main__":
    run_this_suite(__file__)
