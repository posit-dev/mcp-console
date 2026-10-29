"""Integrator selection shares the existing named-session owners with MCP."""

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
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.previews import recording_events
from support.records import Transcript, TranscriptWithCompanions
from support.requirements import POSIX, R, requires
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


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_selects_named_workers_and_releases_closed_capacity(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-selected-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint), "-c", "sessions.max=2"),
            selected_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="shared = 40")
            client.expect(
                "agent alpha\n",
                session="alpha",
                python="shared = 100; print('agent alpha')",
            )
            with ControlClient(endpoint) as control:
                listed = control.request(op="sessions")["result"]["sessions"]
                assert [(item["name"], item["state"]) for item in listed] == [
                    ("alpha", "idle"),
                    ("default", "idle"),
                ], listed
                # The history predates the first control selection of this worker.
                alpha = control.request(op="output", session="alpha")["result"]
                assert (
                    "".join(
                        item.get("text", "")
                        for item in alpha["events"]
                        if item["type"] == "output"
                    )
                    == "agent alpha\n"
                ), alpha
                assert alpha["evaluations"][0]["origin"] == "mcp", alpha
                default = control.request(op="output")["result"]
                assert "agent alpha" not in json.dumps(default), default
                with ControlClient(endpoint) as observer:
                    assert observer.request(
                        op="subscribe", session="alpha", format="events"
                    )["result"] == {"subscribed": True}
                    event = json.loads(observer.reader.readline(timeout=30))
                    assert event["session"] == "alpha" and event["result"] == alpha, (
                        event
                    )
                alpha_recording = control.request(op="recording", session="alpha")[
                    "result"
                ]
                default_recording = control.request(op="recording")["result"]
                assert alpha_recording["directory"] == default_recording["directory"]
                assert alpha_recording["qmd"] != default_recording["qmd"]
                assert "agent alpha" in Path(alpha_recording["qmd"]).read_text()
                assert "agent alpha" not in Path(default_recording["qmd"]).read_text()
                rejected = control.request(op="create", session="beta")
                assert "session limit reached" in rejected["error"]["message"], rejected
                private = control.request(
                    op="evaluate",
                    session="alpha",
                    language="python",
                    code="print('private alpha')",
                    execution="immediate",
                    record=False,
                )
                assert result_text(private) == "private alpha\n", private
                assert "private alpha" not in json.dumps(
                    control.request(op="output", session="alpha")
                )
                direct = control.request(
                    op="evaluate",
                    session="alpha",
                    language="python",
                    code="shared += 2; print(shared)",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(direct) == "102\n", direct
                client.expect("\n[idle]", session="alpha")
                client.expect("40\n", python="print(shared)")
                assert (
                    control.request(op="close", session="default")["error"]["code"]
                    == "invalid_request"
                )
                assert control.request(op="close", session="alpha")["result"][
                    "accepted"
                ]
                listed = control.request(op="sessions")["result"]["sessions"]
                assert (
                    listed[0]["name"] == "alpha" and listed[0]["state"] == "stopped"
                ), listed
                created = control.request(op="create", session="beta")["result"]
                assert created == {
                    "session": "beta",
                    "state": "stopped",
                    "worker_id": 0,
                }, created
                beta = control.request(
                    op="evaluate",
                    session="beta",
                    language="python",
                    code="shared = 200; print(shared)",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(beta) == "200\n", beta
                assert control.request(op="close", session="beta")["result"]["accepted"]
                reopened = control.request(op="create", session="alpha")["result"]
                assert reopened["state"] == "stopped", reopened
                resumed = control.request(
                    op="evaluate",
                    session="alpha",
                    language="python",
                    code="print('shared' in globals()); shared = 300",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(resumed).endswith("False\n"), resumed
                previous_worker = control.request(op="output", session="alpha")[
                    "result"
                ]["worker_id"]
                restarted = control.request(op="restart", session="alpha")["result"]
                assert (
                    restarted["accepted"] and restarted["worker_id"] > previous_worker
                ), restarted
                result = client.expect(
                    None, session="alpha", python="print('shared' in globals())"
                )
                assert "False\n" in "".join(
                    block.get("text", "") for block in result["content"]
                ), result
                client.expect("40\n", python="print(shared)")
                assert (
                    control.request(op="status", session="absent")["error"]["code"]
                    == "unknown_session"
                )
                return normalize_recording(client.finish(), root)


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_routes_input_interrupts_and_preparation_to_selected_session(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-routing-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            waiting = client.send(python="print(input('default: '))")
            assert tool_text(waiting).endswith("[waiting for stdin]"), waiting
            with ControlClient(endpoint) as control:
                created = control.request(
                    op="evaluate",
                    session="alpha",
                    language="python",
                    code="print('alpha started')",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(created) == "alpha started\n", created
                waiting = client.send(session="alpha", python="print(input('alpha: '))")
                assert tool_text(waiting).endswith("[waiting for stdin]"), waiting
                default = control.request(op="status")["result"]
                alpha = control.request(op="status", session="alpha")["result"]
                assert default["state"] == alpha["state"] == "input_requested"
                assert default["evaluation"]["id"] != alpha["evaluation"]["id"]
                listed = control.request(op="sessions")["result"]["sessions"]
                assert all(item["state"] == "waiting_for_stdin" for item in listed), (
                    listed
                )
                wrong = control.request(
                    op="stdin", evaluation_id=alpha["evaluation"]["id"], input="wrong\n"
                )
                assert wrong["error"]["code"] == "stale_evaluation", wrong
                supplied = control.request(
                    op="stdin",
                    session="alpha",
                    evaluation_id=alpha["evaluation"]["id"],
                    input="named reply\n",
                )
                assert supplied["result"] == {"accepted": True}, supplied
                client.expect("named reply\n", session="alpha")
                waiting = client.send(
                    session="alpha", python="print(input('interrupt alpha: '))"
                )
                assert tool_text(waiting).endswith("[waiting for stdin]"), waiting
                prepared = control.request(
                    op="prepare",
                    session="alpha",
                    requirements={"python": ["more-itertools"]},
                )["result"]
                assert prepared["status"] == "busy", prepared
                target = control.request(op="status", session="alpha")["result"][
                    "evaluation"
                ]["id"]
                assert control.request(
                    op="interrupt", session="alpha", evaluation_id=target
                )["result"] == {"accepted": True}
                interrupted = client.expect(None, session="alpha")
                assert "KeyboardInterrupt" in tool_text(interrupted), interrupted
                assert control.request(op="status")["result"] == default
                assert result_text(control.request(op="preview")) == ""
                control.request(
                    op="stdin",
                    evaluation_id=default["evaluation"]["id"],
                    input="default reply\n",
                )
                client.expect("default reply\n", session="default")
                requirements = control.request(op="requirements", session="alpha")[
                    "result"
                ]
                manifest = control.request(op="manifest", session="alpha")["result"]
                assert (
                    requirements["requirements"] == manifest == prepared["requirements"]
                )
                return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_invalid_named_evaluation_does_not_reserve_capacity(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-admission-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint), "-c", "sessions.max=2"),
            selected_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="pass")
            with ControlClient(endpoint) as control:
                rejected = control.request(
                    op="evaluate",
                    session="invalid",
                    language="python",
                    code="pass",
                    wait_ms=0,
                )
                assert rejected["error"]["code"] == "invalid_request", rejected
                listed = control.request(op="sessions")["result"]["sessions"]
                assert [item["name"] for item in listed] == ["default"], listed
                created = control.request(op="create", session="alpha")
                assert created["result"]["session"] == "alpha", created
                assert control.request(op="close", session="alpha")["result"][
                    "accepted"
                ]
                return normalize_recording(client.finish(), root)


if __name__ == "__main__":
    run_this_suite(__file__)
