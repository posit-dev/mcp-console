#!/usr/bin/env -S uv run --script

import json
import base64
import os
import select
import sys
import tempfile
from contextlib import ExitStack, closing
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import (
    FifoCheckpoint,
    wait_for_worker_file,
    wait_for_checkpoint,
)
from support.client import McpClient
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.previews import session_directory
from support.processes import host_process_id
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    PTHREAD_MUTEX_INTERPOSITION,
    R,
    SQL,
    command,
    requires,
)
from support.suites import run_this_suite
from boundaries.client_server.output.test_recovery import recovery_client


def recording_events(recording: Path) -> list[dict]:
    return [
        json.loads(line)
        for line in (recording / "capture/events.jsonl").read_text().splitlines()
    ]


@requires(NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION, PROCESS_EVENTS)
@executions(DIRECT)
def test_retains_the_send_that_exceeds_capture_backlog(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        workspace = Path(directory)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "release"))
        )
        exits = resources.enter_context(Events())
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {
                **os.environ,
                LOADER_VARIABLE: str(
                    build_interposer(workspace, "recording_receive_checkpoint")
                ),
                "MCP_CONSOLE_TEST_RECORDING_REACHED": str(reached.path),
                "MCP_CONSOLE_TEST_RECORDING_RELEASE": str(release.path),
                "TMPDIR": str(workspace),
                "ZOD_REPORT_PID": "1",
            },
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            worker_pid = host_process_id(
                int(
                    wait_for_worker_file(
                        workspace, "zod-worker-pid", client
                    ).read_text()
                ),
                client.process.pid,
            )
            exits.watch_process(worker_pid)
            held = client.start_send(r="echo held capture")
            try:
                reached.wait("capture queue head has not completed", client=client)
                client.notify("notifications/cancelled", requestId=held["id"])
                for index in range(63):
                    queued = client.start_send(r=f"echo queued {index}")
                    client.notify("notifications/cancelled", requestId=queued["id"])
                assert client.request("ping")["result"] == {}
                overflow = client.start_send(r="echo overload receipt")
                assert client.stderr.readline(timeout=5) == (
                    "mcp-console: recording capture backlog exceeded (64 calls); closing MCP input\n"
                )
                client.stdin.close()
            finally:
                release.release()
            assert exits.wait(10) == {worker_pid}
            client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        events = recording_events(recording)
        calls = [e for e in events if e["event"] == "tool_call"]
        assert [e["call_id"] for e in calls] == list(range(1, 66))
        received = [
            e
            for e in events
            if e["event"] == "protocol"
            and e["direction"] == "received"
            and e["message"].get("method") == "tools/call"
        ]
        assert [e["message"]["id"] for e in received] == [
            e["id"] for e in client.transcript if "send" in e
        ]
        assert received[-1]["message"]["params"] == {
            "name": "send",
            "arguments": overflow["send"],
        }
        assert events[-1]["event"] == "recording_ended"
        return [
            *client.transcript[:3],
            {"capture": "overload-triggering send retained before finalization"},
        ]


@requires(NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION, PROCESS_EVENTS)
@executions(DIRECT)
def test_settles_cancelled_capture_before_finalization(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        workspace = Path(directory)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(workspace / "release"))
        )
        exits = resources.enter_context(Events())
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(
                build_interposer(workspace, "recording_receive_checkpoint")
            ),
            "MCP_CONSOLE_TEST_RECORDING_REACHED": str(reached.path),
            "MCP_CONSOLE_TEST_RECORDING_RELEASE": str(release.path),
            "TMPDIR": str(workspace),
            "ZOD_REPORT_PID": "1",
        }
        with McpClient(
            binary, execution.serve("--worker", str(worker)), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            worker_pid = host_process_id(
                int(
                    wait_for_worker_file(
                        workspace, "zod-worker-pid", client
                    ).read_text()
                ),
                client.process.pid,
            )
            exits.watch_process(worker_pid)
            try:
                held = client.start_send(r="echo held capture")
                reached.wait("call 2 captured but its recording task has not completed")
                queued = [
                    client.start_send(r=f"echo cancelled queued source {index}")
                    for index in range(16)
                ]
                client.notify("notifications/cancelled", requestId=held["id"])
                for cancelled in queued:
                    client.notify("notifications/cancelled", requestId=cancelled["id"])
                assert client.request("ping")["result"] == {}
                client.stdin.close()
                assert exits.wait(5) == {worker_pid}
            finally:
                release.release()
            client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        calls = [event for event in events if event["event"] == "tool_call"]
        assert [event["call_id"] for event in calls] == list(range(1, 19)), calls
        assert [event["request"]["arguments"]["r"] for event in calls[2:]] == [
            f"echo cancelled queued source {index}" for index in range(16)
        ], calls
        results = [event for event in events if event["event"] == "tool_result"]
        assert sorted(event["call_id"] for event in results) == list(range(1, 19)), (
            results
        )
        assert events[-1]["event"] == "recording_ended", events[-1]
        return [
            *client.transcript[:3],
            {"capture": "all received calls retained before finalization"},
        ]


@requires(NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION)
@executions(DIRECT)
def test_retains_received_send_while_an_earlier_response_finishes(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        workspace = Path(directory)
        gates = {
            name: resources.enter_context(
                closing(FifoCheckpoint.create(workspace / name))
            )
            for name in (
                "cell-entered",
                "cell-release",
                "recording-reached",
                "recording-release",
            )
        }
        environment = dict(
            os.environ,
            **{
                LOADER_VARIABLE: str(
                    build_interposer(workspace, "recording_receive_checkpoint")
                ),
                "MCP_CONSOLE_TEST_RECORDING_CALL_ID": "3",
                "MCP_CONSOLE_TEST_RECORDING_REACHED": str(
                    gates["recording-reached"].path
                ),
                "MCP_CONSOLE_TEST_RECORDING_RELEASE": str(
                    gates["recording-release"].path
                ),
            },
        )
        with McpClient(
            binary,
            execution.serve(
                "-c", "languages=[python]", "-c", "python=" + json.dumps(sys.executable)
            ),
            environment,
            workspace,
        ) as client:
            try:
                client.initialize_and_list_tools()
                # Worker readiness precedes first-use dependency preparation.
                # Complete a public evaluation before measuring the capture race.
                client.expect(python="pass")
                client.response_timeout = 10
                first = client.start_send(
                    # fmt: python
                    python=code(f"""
                        from pathlib import Path

                        with Path({str(gates["cell-entered"].path)!r}).open('wb', buffering=0) as gate:
                            gate.write(b'1')
                        with Path({str(gates["cell-release"].path)!r}).open('rb', buffering=0) as gate:
                            assert gate.read(1) == b'1'
                        print('first response')
                        """),
                )
                gates["cell-entered"].wait("first cell awaits release")
                second = client.start_send(python="21 * 2")
                gates["recording-reached"].wait(
                    "second request retained before dispatch"
                )
                gates["cell-release"].release()
                client.receive(first)
                assert first["result"] == {
                    "content": [{"type": "text", "text": "first response\n"}],
                    "isError": False,
                }
                # A received frame can precede completion of its local write.
                # Establish delivery before releasing the second handler.
                (recording,) = (workspace / ".agents/console/sessions").iterdir()
                wait_for_checkpoint(
                    lambda: (
                        recording
                        if any(
                            event["event"] == "delivery"
                            and event["call_id"] == 2
                            and event["delivered"]
                            for event in recording_events(recording)
                        )
                        else None
                    ),
                    "first response transport write settled",
                    root=recording,
                    recursive=True,
                    client=client,
                )
                gates["recording-release"].release()
                client.receive(second)
                assert second["result"] == {
                    "content": [{"type": "text", "text": "42\n"}],
                    "isError": False,
                }, second
                recorded = json.dumps(client.finish())
                for path in (workspace, workspace.resolve()):
                    recorded = recorded.replace(
                        json.dumps(str(path))[1:-1], "<workspace>"
                    )
                return json.loads(recorded)
            finally:
                gates["cell-release"].release()
                gates["recording-release"].release()


@requires(NATIVE_FIXTURES)
def test_recording_send_keeps_following_cancellation_and_ping_live(
    binary: Path,
) -> Transcript:
    with recovery_client(binary) as (client, profile, reached, release):
        first = client.start_send(control="interrupt")
        reached.wait("result journal writer owns the recorder")
        try:
            assert client.request("ping")["result"] == {}
            queued = client.start_send(r="echo cancelled cell must not run")
            controlled = client.start_send(
                control="restart", r="echo cancelled restart must not run"
            )
            client.notify("notifications/cancelled", requestId=queued["id"])
            client.notify("notifications/cancelled", requestId=controlled["id"])
            client.response_timeout = 2
            assert client.request("ping")["result"] == {}
        finally:
            client.response_timeout = 600
            profile.pause_results(False)
            release.release()
        client.receive(first)
        assert first["result"] == {
            "content": [{"type": "text", "text": "\n[idle]"}],
            "isError": False,
        }, first
        client.expect("zod: after\n", r="echo after")
        recording = session_directory(client)
        transcript = client.transcript
        events = recording_events(recording)
        calls = [event for event in events if event["event"] == "tool_call"]
        assert [event["request_id"] for event in calls] == [
            entry["id"] for entry in transcript if "send" in entry
        ]
        assert (
            len([event for event in events if event["event"] == "worker_started"]) == 1
        )
        assert (
            recording / "capture/sessions/001/cell-002.txt"
        ).read_text() == "zod: after\n"
        assert [
            event["call_id"] for event in events if event["event"] == "cell_started"
        ] == [1, 5]
        assert not (recording / "capture/sessions/001/cell-003.txt").exists()
        received = [
            event["message"]
            for event in events
            if event["event"] == "protocol" and event["direction"] == "received"
        ]
        positions = {
            message["id"]: i for i, message in enumerate(received) if "id" in message
        }
        assert positions[queued["id"]] < positions[controlled["id"]]
        cancellations = [
            message["params"]["requestId"]
            for message in received
            if message.get("method") == "notifications/cancelled"
        ]
        assert cancellations == [queued["id"], controlled["id"]]
        return client.finish()


@requires(NATIVE_FIXTURES, R, SQL, command("ir"))
@executions(DIRECT)
def test_first_rejected_send_publishes_the_observed_prelude(
    binary: Path, execution: Execution
) -> Transcript:
    from support.r import r_test_environment, reference_plots
    from boundaries.client_server.recording.test_journal import (
        STARTUP_PLOT,
        configure_r_startup,
    )

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment, rscript = r_test_environment()
        (expected_image,) = reference_plots(
            rscript, environment, STARTUP_PLOT, width=4, height=3, dpi=100, pages=1
        )
        configure_r_startup(
            root,
            environment,
            rscript,
            'cat("retained startup text\\n")\n' + STARTUP_PLOT,
        )
        with closing(FifoCheckpoint.create(root / "observed")) as observed:
            environment.update(
                {
                    "RETICULATE_PYTHON": sys.executable,
                    LOADER_VARIABLE: str(
                        build_interposer(root, "recording_observation")
                    ),
                    "MCP_CONSOLE_TEST_OBSERVED_EVENT": "image_retained",
                    "MCP_CONSOLE_TEST_JOURNAL_OBSERVED": str(observed.path),
                }
            )
            with McpClient(binary, execution.serve(), environment, root) as client:
                client.initialize_and_list_tools()
                observed.wait(
                    "server recorded startup text and image",
                    timeout=600,
                    client=client,
                )
                assert client.request("ping")["result"] == {}
                assert not (root / ".agents/console/sessions").exists()
                before_send = datetime.now().astimezone()
                rejected = client.send(r="must not execute", python="must not execute")
                assert rejected["isError"] is True
                (recording,) = (root / ".agents/console/sessions").iterdir()
                published = recording_events(recording)
                prelude = [
                    e
                    for e in published
                    if e["event"]
                    in {
                        "recording_started",
                        "worker_started",
                        "worker_ready",
                        "image_retained",
                    }
                ]
                assert [e["event"] for e in prelude] == [
                    "recording_started",
                    "worker_started",
                    "worker_ready",
                    "image_retained",
                ]
                assert all(
                    datetime.fromisoformat(e["at"]) < before_send for e in prelude
                )
                activation = next(
                    e for e in published if e["event"] == "recording_activated"
                )
                assert datetime.fromisoformat(activation["at"]) >= before_send
                assert (
                    recording / "capture/sessions/001/startup.txt"
                ).read_text() == "retained startup text\n"
                image = next(e for e in prelude if e["event"] == "image_retained")
                assert (recording / image["path"]).read_bytes() == expected_image
                assert not client.send()["isError"]
                assert not client.send(stdin="unused input")["isError"]
                transcript = client.finish()
            events = recording_events(recording)
            assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
            received = [
                e["message"]
                for e in events
                if e["event"] == "protocol" and e["direction"] == "received"
            ]
            assert [m["method"] for m in received] == [
                "initialize",
                "notifications/initialized",
                "tools/list",
                "ping",
                "tools/call",
                "tools/call",
                "tools/call",
            ]
            assert [m["params"] for m in received if m["method"] == "tools/call"] == [
                {"tool_call_id": i} for i in range(1, 4)
            ]
            assembled = [
                e["message"]
                for e in events
                if e["event"] == "protocol" and e["direction"] == "assembled"
            ]
            assert [
                m["result"]
                for m in assembled
                if m.get("id")
                in {e["request_id"] for e in events if e["event"] == "tool_call"}
            ] == [{"tool_result_call_id": i} for i in range(1, 4)]
            assert events[-1]["event"] == "recording_ended"
            assert all(e["delivered"] for e in events if e["event"] == "delivery")
            for entry in transcript:
                for block in entry.get("result", {}).get("content", []):
                    if block["type"] == "image":
                        assert base64.b64decode(block["data"]) == expected_image
                        block["data"] = "<verified startup PNG>"
            return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_sessions_follow_actual_worker_lifetimes(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        control = root / "startup-control"
        control.write_text("ready")
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, "ZOD_STARTUP_CONTROL": str(control)},
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="echo first")
            client.send(control="interrupt")
            client.send(control="restart", requirements={"action": "get"})
            client.send(r="echo still first")
            (recording,) = (root / ".agents/console/sessions").iterdir()
            assert (
                len(
                    [
                        e
                        for e in recording_events(recording)
                        if e["event"] == "worker_started"
                    ]
                )
                == 1
            )
            client.send(control="restart")
            client.send(r="echo second")
            client.send(r="exit unexpectedly")
            client.send(r="echo third")
            client.finish()
        events = recording_events(recording)
        for transition in ("worker_started", "worker_ready", "worker_ended"):
            assert [e["session_id"] for e in events if e["event"] == transition] == [
                1,
                2,
                3,
            ], (transition, events)
        assert not any(e["event"] == "startup_failed" for e in events)
        assert len(list((root / ".agents/console/sessions").iterdir())) == 1
        return client.transcript


@requires(NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION, PROCESS_EVENTS)
@executions(DIRECT)
def test_finalization_waits_for_result_task_completion(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as resources:
        workspace = Path(directory)
        reached, release, finalized = [
            resources.enter_context(closing(FifoCheckpoint.create(workspace / name)))
            for name in ("reached", "release", "finalized")
        ]
        exits = resources.enter_context(Events())
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(
                build_interposer(workspace, "recording_receive_checkpoint")
            ),
            "MCP_CONSOLE_TEST_RECORDING_REACHED": str(reached.path),
            "MCP_CONSOLE_TEST_RECORDING_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_RECORDING_FINALIZED": str(finalized.path),
            "MCP_CONSOLE_TEST_HOLD_RESULT": "1",
            "TMPDIR": str(workspace),
            "ZOD_REPORT_PID": "1",
        }
        with McpClient(
            binary, execution.serve("--worker", str(worker)), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            worker_pid = host_process_id(
                int(
                    wait_for_worker_file(
                        workspace, "zod-worker-pid", client
                    ).read_text()
                ),
                client.process.pid,
            )
            exits.watch_process(worker_pid)
            try:
                held = client.start_send(r="echo completed result")
                reached.wait(
                    "result task paused after releasing its journal mutex",
                    client=client,
                )
                client.notify("notifications/cancelled", requestId=held["id"])
                assert client.request("ping")["result"] == {}
                client.stdin.close()
                assert exits.wait(5) == {worker_pid}
                # Cancellation removed delivery ownership, and worker retirement
                # is proven. Keep observing beyond the one-second delivery deadline;
                # expiry must not release the result-completion barrier.
                assert not select.select([finalized.descriptor], [], [], 3)[0], (
                    "recording finalized while result task was still active"
                )
            finally:
                release.release()
            client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        events = recording_events(recording)
        assert [e["call_id"] for e in events if e["event"] == "tool_result"] == [1, 2]
        assert events[-1]["event"] == "recording_ended"
        return [
            *client.transcript[:3],
            {
                "capture": "result task settles before finalization after cancellation and EOF"
            },
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
