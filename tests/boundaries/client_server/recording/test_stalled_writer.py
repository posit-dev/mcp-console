#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from boundaries.client_server.recording.test_process import recording_events
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.client import McpClient
from support.execution import DIRECT
from support.native import LOADER_VARIABLE, build_interposer
from support.previews import OMISSION, TEXT_BUDGET, assert_preview
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite

BEHIND = "recording writer stalled; retained files may be missing or incomplete"


@contextmanager
def stalled_writer(
    binary: Path, observation: dict[str, str], *arguments: str
) -> Iterator[tuple[McpClient, Path, FifoCheckpoint, FifoCheckpoint]]:
    """Hold the first matching recording write until the release checkpoint."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "startup-control").write_text("ready")
        with (
            closing(FifoCheckpoint.create(root / "observed")) as observed,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                DIRECT.serve(
                    "--worker",
                    str(Path(__file__).resolve().parents[3] / "fixtures/zod"),
                    *arguments,
                ),
                {
                    **os.environ,
                    LOADER_VARIABLE: str(
                        build_interposer(root, "recording_observation")
                    ),
                    **observation,
                    "ZOD_STARTUP_CONTROL": str(root / "startup-control"),
                    "ZOD_STARTUP_STARTED": str(root / "startup-started"),
                    "ZOD_STARTUP_RELEASE": str(root / "startup-release"),
                    "MCP_CONSOLE_TEST_JOURNAL_OBSERVED": str(observed.path),
                    "MCP_CONSOLE_TEST_JOURNAL_RELEASE": str(release.path),
                },
                root,
                response_timeout=15,
                # The stalled writer cannot record a delivery.
                recording_receipts=False,
            ) as client,
        ):
            client.initialize_and_list_tools()
            yield client, root, observed, release


def response_text(entry: dict) -> str:
    return "".join(
        block["text"] for block in entry["result"]["content"] if block["type"] == "text"
    )


def completed_calls(root: Path, count: int = 3) -> list[dict]:
    (recording,) = (root / ".agents/console/sessions").iterdir()
    events = recording_events(recording)
    calls = [e["call_id"] for e in events if e["event"] == "tool_call"]
    results = [e["call_id"] for e in events if e["event"] == "tool_result"]
    assert calls == results == list(range(1, count + 1)), (calls, results)
    assert events[-1]["event"] == "recording_ended"
    return events


@requires(NATIVE_FIXTURES, POSIX)
def test_stalled_result_write_does_not_hold_replies(binary: Path) -> Transcript:
    with stalled_writer(binary, {"MCP_CONSOLE_TEST_OBSERVED_EVENT": "tool_result"}) as (
        client,
        root,
        observed,
        release,
    ):
        first = client.start_send(r="echo first")
        try:
            observed.wait("tool_result journal write is stalled", client=client)
            # The journal still lacks this result. Its reply, a poll, and a new
            # cell must all complete without the recording writer.
            client.receive(first)
            assert response_text(first) == "zod: first\n", first
            client.expect("\n[idle]")
            client.expect("zod: second\n", r="echo second")
        finally:
            release.release()
        transcript = client.finish()
        completed_calls(root)
        return transcript


@requires(NATIVE_FIXTURES, POSIX)
def test_stalled_output_file_write_does_not_hold_replies(binary: Path) -> Transcript:
    # Journal lines quote the same text, so select the cell file by its path.
    with stalled_writer(
        binary, {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-001.txt"}
    ) as (
        client,
        root,
        observed,
        release,
    ):
        first = client.start_send(r="echo first")
        try:
            observed.wait("cell output file write is stalled", client=client)
            client.receive(first)
            assert response_text(first) == "zod: first\n", first
            client.expect("\n[idle]")
            client.expect("zod: second\n", r="echo second")
        finally:
            release.release()
        transcript = client.finish()
        events = completed_calls(root)
        (recording,) = (root / ".agents/console/sessions").iterdir()
        outputs = recording / "capture/sessions/001"
        assert (outputs / "cell-001.txt").read_text() == "zod: first\n"
        assert (outputs / "cell-002.txt").read_text() == "zod: second\n"
        assert [
            (e["path"], e["retained_bytes"], e["discarded_bytes"])
            for e in events
            if e["event"] == "cell_output"
        ] == [
            ("capture/sessions/001/cell-001.txt", 11, 0),
            ("capture/sessions/001/cell-002.txt", 12, 0),
        ]
        return transcript


@requires(NATIVE_FIXTURES, POSIX)
def test_oversized_reply_reports_a_stalled_recording_writer(binary: Path) -> Transcript:
    emitted = (
        "preview head\n"
        + "ab" * 12000
        + "\npreview tail: final diagnostic\nafter final image\n"
    )
    with stalled_writer(
        binary, {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-002.txt"}
    ) as (
        client,
        root,
        observed,
        release,
    ):
        client.expect("zod: ready\n", r="echo ready")
        oversized = client.start_send(r="preview tiny events")
        try:
            observed.wait("cell output file write is stalled", client=client)
            # The omission marker cites a file. Its reply waits a bounded time
            # for the writer, then names the path and says what it cannot promise.
            client.receive(oversized)
            text = response_text(oversized)
            assert_preview(text, emitted)
            (recording,) = (root / ".agents/console/sessions").iterdir()
            log = (
                f".agents/console/sessions/{recording.name}"
                "/capture/sessions/001/cell-002.txt"
            )
            (marker,) = OMISSION.finditer(text)
            assert marker[0].endswith(f"; raw log: {log}; {BEHIND}]\n"), marker[0]
            # Ordinary replies do not wait for the stalled writer at all.
            client.expect("zod: later\n", r="echo later")
        finally:
            release.release()
        transcript = client.finish()
        completed_calls(root)
        assert (recording / "capture/sessions/001/cell-002.txt").read_text() == emitted
        return [
            *transcript[:3],
            {
                "oversized reply": "bounded wait; raw log path and stalled writer named",
                "ordinary reply": "returned while the writer was stalled",
                "raw log after release": "complete",
            },
        ]


@requires(NATIVE_FIXTURES, POSIX)
def test_phase_notice_overflow_reports_a_stalled_recording_writer(
    binary: Path,
) -> Transcript:
    notices = (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 86]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
    )
    phase = "[phase: replacement]\n"
    terminal = "[worker starting]"
    # The reply fits the text budget without its phase notice and exceeds it
    # by one byte with it.
    emitted = "x" * (TEXT_BUDGET - len(notices + phase + terminal)) + "\n"
    with stalled_writer(
        binary, {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-002.txt"}
    ) as (
        client,
        root,
        observed,
        release,
    ):
        client.expect("zod: ready\n", r="echo ready")
        (root / "startup-control").write_text("block")
        failed = client.start_send(r=f"exit after output: {emitted}", timeout_ms=1_000)
        try:
            observed.wait("cell output file write is stalled", client=client)
            wait_for_path(
                root / "startup-started",
                "replacement worker awaits ready",
                client=client,
            )
            client.receive(failed)
            text = response_text(failed)
            assert text.endswith(notices + phase + terminal), text
            (recording,) = (root / ".agents/console/sessions").iterdir()
            log = (
                f".agents/console/sessions/{recording.name}"
                "/capture/sessions/001/cell-002.txt"
            )
            (marker,) = OMISSION.finditer(text)
            assert marker[0].endswith(f"; raw log: {log}; {BEHIND}]\n"), marker[0]
        finally:
            release.release()
            (root / "startup-release").touch()
        # The poll returns once the replacement worker is ready.
        client.send()
        assert "phase:" not in response_text(client.transcript[-1])
        client.expect("zod: replaced\n", r="echo replaced")
        transcript = client.finish()
        completed_calls(root, 4)
        assert (recording / "capture/sessions/001/cell-002.txt").read_text() == emitted
        return [
            *transcript[:3],
            {
                "phase-bearing reply": "bounded wait; raw log path and stalled writer named",
                "raw log after release": "complete",
            },
        ]


@requires(NATIVE_FIXTURES, POSIX)
def test_preview_subscription_follows_the_recording_writer(binary: Path) -> Transcript:
    # A Unix socket path must be short. Zod publishes its markers in TMPDIR.
    with (
        tempfile.TemporaryDirectory(dir="/tmp", prefix="console-stalled-") as scratch,
        stalled_writer(
            binary,
            {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-002.txt", "TMPDIR": scratch},
            "--control-socket",
            str(Path(scratch) / "control.sock"),
        ) as (client, root, observed, release),
    ):
        client.expect("zod: ready\n", r="echo ready")
        oversized = client.start_send(r="preview tiny events")
        try:
            observed.wait("cell output file write is stalled", client=client)
            # This reply's bounded wait finds the writer stalled.
            client.receive(oversized)
            assert BEHIND in response_text(oversized), oversized
            (recording,) = (root / ".agents/console/sessions").iterdir()
            log = (
                f".agents/console/sessions/{recording.name}"
                "/capture/sessions/001/cell-003.txt"
            )
            emitted = "x" * (2 * TEXT_BUDGET) + "\n"
            held = client.start_send(r=f"hold after output: {emitted}")
            wait_for_path(
                Path(scratch) / "zod-output-held",
                "pending oversized output",
                client=client,
            )
            with ControlClient(Path(scratch) / "control.sock") as observer:
                observer.request(op="subscribe")

                def marker() -> str:
                    event = json.loads(observer.reader.readline(timeout=30))
                    assert event["event"] == "preview", event
                    (marker,) = OMISSION.finditer(result_text(event))
                    return marker[0]

                assert marker().endswith(f"; raw log: {log}; {BEHIND}]\n")
                # The worker emits nothing more. The writer catching up is the
                # only change, and the subscription must replace its snapshot.
                release.release()
                assert marker().endswith(f"; raw log: {log}]\n")
            (Path(scratch) / "zod-release-evaluation").touch()
            client.receive(held)
        finally:
            release.release()
            (Path(scratch) / "zod-release-evaluation").touch()
        transcript = client.finish()
        completed_calls(root)
        assert (recording / "capture/sessions/001/cell-003.txt").read_text() == emitted
        return [
            *transcript[:3],
            {
                "subscription while the writer is stalled": "raw log path and stalled writer named",
                "subscription after the writer catches up": "replaced without new output; raw log path only",
            },
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
