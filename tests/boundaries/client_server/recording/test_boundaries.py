"""Recording boundaries exercised through MCP with a gated relay fixture."""

import base64
import json
import os
import sys
import tempfile
from contextlib import ExitStack, contextmanager, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_intervals import events
from support.previews import live_recording_events
from support.normalization import normalize_recording
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.execution import DIRECT
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite


@contextmanager
def capture_relay(binary: Path, mode: str):
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        gates = {
            name: stack.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "release",
                "drained",
                "evaluating",
                "shutdown",
                "post_sent",
                "launch_sent",
                "acquire",
                "kill-reached-1",
                "kill-release-1",
            )
        }
        fixture = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/recording_intervals.py"
        )
        environment = {
            **os.environ,
            "TMPDIR": directory,
            "CAPTURE_GATES": directory,
            "CAPTURE_SCENARIO": mode,
        }
        if mode == "late_entry":
            environment.update(
                {
                    LOADER_VARIABLE: str(
                        build_interposer(root, "relay_writer_retirement")
                    ),
                    "MCP_CONSOLE_TEST_WRITER_ROOT": directory,
                    "MCP_CONSOLE_TEST_WRITER_MODE": "aborted_frame",
                }
            )
        with McpClient(
            binary,
            DIRECT.serve("--worker", str(binary), "--relay", str(fixture)),
            environment,
            root,
        ) as client:
            client.transcript.append(
                {"fixture": f"Gated relay: {mode.replace('_', ' ')}."}
            )
            client.initialize_and_list_tools()
            try:
                yield client, root, gates
            finally:
                gates["release"].release()
                gates["acquire"].release()
                gates["drained"].release()
                gates["kill-release-1"].release()


def recording(root: Path) -> Path:
    (path,) = (root / ".agents/console/sessions").iterdir()
    return path


def await_event(
    client: McpClient, root: Path, kind: str, predicate=lambda event: True
) -> dict:
    def observed():
        paths = list((root / ".agents/console/sessions").glob("*/capture/events.jsonl"))
        if not paths:
            return None
        # Appends can be observed midway through their final JSONL frame.
        lines = paths[0].read_bytes().splitlines(keepends=True)
        recorded = (json.loads(line) for line in lines if line.endswith(b"\n"))
        return next(
            (
                event
                for event in recorded
                if event["event"] == kind and predicate(event)
            ),
            None,
        )

    return wait_for_checkpoint(
        observed, f"recorded {kind}", root=root, client=client, recursive=True
    )


@requires(POSIX)
def test_invalid_completion_does_not_confirm_cell_end(binary: Path) -> Transcript:
    with capture_relay(binary, "invalid_completion") as (client, root, gates):
        prompt = client.send(r="invalid completion", timeout_ms=60000)
        assert (
            tool_text(prompt) == '[input requested: "pending> "]\n[waiting for stdin]'
        ), prompt
        gates["release"].release()
        await_event(
            client, root, "worker_started", lambda event: event["session_id"] == 2
        )
        result = client.send(session="default", timeout_ms=60000)
        text = result["content"][0]["text"]
        assert result["isError"], result
        assert "worker completed with an outstanding input request" in text, text
        transcript = client.finish()
        ended = [
            event for event in events(recording(root)) if event["event"] == "cell_ended"
        ]
        assert len(ended) == 1, ended
        assert ended[0]["completion_confirmed"] is False, ended
        return normalize_recording(transcript, root)


@requires(POSIX)
def test_completion_requires_cell_entry(binary: Path) -> Transcript:
    with capture_relay(binary, "missing_entry") as (client, root, gates):
        failed = client.start_send(r="complete without entry", timeout_ms=60000)
        gates["release"].release()
        client.receive(failed)
        result = failed["result"]
        assert result["isError"], result
        assert (
            "worker completed a cell before reporting its start"
            in result["content"][0]["text"]
        )
        transcript = client.finish()
        retained = recording(root)
        capture = events(retained)
        assert not any(e["event"] in {"cell_started", "cell_ended"} for e in capture)
        assert not list((retained / "capture/sessions").glob("*/cell-*.txt"))
        assert [e["session_id"] for e in capture if e["event"] == "worker_started"] == [
            1,
            2,
        ]
        return normalize_recording(transcript, root)


def outcome_less_retirement(binary: Path, *, interval: str) -> Transcript:
    with capture_relay(binary, f"lost_{interval}_owner") as (client, root, gates):
        if interval == "post":
            assert tool_text(client.send(r="finish a cell")) == "executed cell\n"
        failed = client.start_send(r="lose relay", timeout_ms=60000)
        gates["post_sent"].wait("last old-worker output sent", client=client)
        relative = (
            "capture/sessions/001/cell-001-post.txt"
            if interval == "post"
            else "capture/sessions/001/startup.txt"
        )
        await_event(client, root, "output_text", lambda e: e["path"] == relative)
        log = recording(root) / relative
        assert log.read_text() == "before relay loss\n"
        assert log.stat().st_mode & 0o200
        gates["release"].release()
        gates["launch_sent"].wait(
            "replacement launch after old I/O retired", client=client
        )
        assert log.stat().st_mode & 0o222 == 0
        await_event(
            client,
            root,
            "process_output",
            lambda e: base64.b64decode(e["data"]).endswith(b"\n"),
        )
        diagnostic = b"".join(
            base64.b64decode(e["data"])
            for e in live_recording_events(recording(root))
            if e["event"] == "process_output"
        )
        assert diagnostic == b"replacement launch diagnostic\n", diagnostic
        gates["acquire"].release()
        client.receive(failed)
        assert failed["result"]["isError"], failed
        assert (
            "replacement launch diagnostic\n" in failed["result"]["content"][0]["text"]
        )
        transcript = client.finish()
        retained = recording(root)
        capture = events(retained)
        admitted = [e["call_id"] for e in capture if e["event"] == "source_admitted"]
        settled = [e["call_id"] for e in capture if e["event"] == "source_finished"]
        assert settled == admitted, (admitted, settled)
        assert log.read_text() == "before relay loss\n"
        assert not any(
            e["event"] == "worker_ended" and e["session_id"] == 1 for e in capture
        )
        owners = []
        for event in capture:
            if event["event"] != "tool_result":
                continue
            for part in event["parts"]:
                block = event["result"]["content"][part["block"]]
                if block["type"] == "text":
                    text = block["text"].encode()[part["start"] : part["end"]].decode()
                    if "replacement launch diagnostic" in text:
                        owners.append(part["owner"])
        assert owners == [None], owners
        return normalize_recording(transcript, root)


@requires(POSIX)
def test_relay_loss_finalizes_startup_before_replacement(binary: Path) -> Transcript:
    return outcome_less_retirement(binary, interval="startup")


@requires(POSIX)
def test_relay_loss_finalizes_post_cell_before_replacement(binary: Path) -> Transcript:
    return outcome_less_retirement(binary, interval="post")


@requires(NATIVE_FIXTURES, POSIX)
def test_cell_entry_during_retirement_retains_ownership(binary: Path) -> Transcript:
    with capture_relay(binary, "late_entry") as (client, root, gates):
        active = client.start_send(r="enter after retirement", timeout_ms=60000)
        gates["evaluating"].wait("evaluation command received", client=client)
        restart = client.start_send(control="restart", timeout_ms=60000)
        gates["shutdown"].wait("shutdown command received", client=client)
        gates["kill-reached-1"].wait(
            "ordered retirement completed before launcher signal", client=client
        )
        gates["release"].release()
        await_event(client, root, "worker_ended")
        gates["kill-release-1"].release()
        client.receive_many([active, restart])
        assert "entered during retirement\n" in "".join(
            entry["result"]["content"][0]["text"] for entry in (active, restart)
        ), (active, restart)
        transcript = client.finish()
        retained = recording(root)
        assert (
            retained / "capture/sessions/001/cell-001.txt"
        ).read_text() == "entered during retirement\n"
        boundaries = [
            event
            for event in events(retained)
            if event["event"] in {"cell_started", "cell_ended"}
        ]
        assert [event["event"] for event in boundaries] == [
            "cell_started",
            "cell_ended",
        ], boundaries
        assert boundaries[-1]["completion_confirmed"] is True, boundaries
        assert [
            event["call_id"]
            for event in events(retained)
            if event["event"] == "source_finished"
        ] == [1]
        return normalize_recording(transcript, root)


@requires(POSIX)
def test_worker_exit_finalizes_and_clears_interval_owner(binary: Path) -> Transcript:
    with capture_relay(binary, "ended_owner") as (client, root, gates):
        client.send(r="complete before shutdown", timeout_ms=60000)
        gates["post_sent"].wait("post-cell output sent", client=client)
        # Observe worker exit while its relay remains live. Holding a native
        # retirement signal here would consume the launcher's cleanup deadline.
        await_event(client, root, "worker_ended")
        post = recording(root) / "capture/sessions/001/cell-001-post.txt"
        assert post.read_text() == "post-cell text\n"
        assert post.stat().st_mode & 0o222 == 0
        gates["release"].release()
        event = await_event(client, root, "process_output")
        assert base64.b64decode(event["data"]) == b"after worker exit\n", event
        gates["drained"].release()
        transcript = client.finish()
        assert post.read_text() == "post-cell text\n"
        wire = [
            json.loads(line)
            for line in (root / "relay-wire.jsonl").read_text().splitlines()
        ]
        emitted = [entry["relay"]["kind"] for entry in wire if "relay" in entry]
        assert emitted[-4:] == [
            "stdout_closed",
            "stderr_closed",
            "worker_sideband_closed",
            "worker_exited",
        ], emitted
        return normalize_recording(transcript, root)


@requires(POSIX)
def test_process_output_omission_references_retained_journal(
    binary: Path,
) -> Transcript:
    with capture_relay(binary, "process_output") as (client, root, gates):
        active = client.start_send(r="silent cell", timeout_ms=60000)
        await_event(
            client,
            root,
            "process_output",
            lambda event: base64.b64decode(event["data"]).endswith(b"process tail\n"),
        )
        gates["release"].release()
        client.receive(active)
        result = active["result"]
        text = tool_text(result)
        assert "[output omitted:" in text, text
        assert "capture/events.jsonl" in text, text
        assert "unavailable" not in text, text
        transcript = client.finish()
        raw = b"".join(
            base64.b64decode(event["data"])
            for event in events(recording(root))
            if event["event"] == "process_output"
        )
        assert raw == b"process head\n" + b"p" * 32768 + b"\nprocess tail\n", len(raw)
        return normalize_recording(transcript, root)


if __name__ == "__main__":
    run_this_suite(__file__)
