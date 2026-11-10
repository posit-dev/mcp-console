#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.recording.test_process import recording_events
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT
from support.native import LOADER_VARIABLE, build_interposer
from support.previews import OMISSION, assert_preview
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite

BEHIND = "recording writer stalled; retained files may be missing or incomplete"


@contextmanager
def stalled_writer(
    binary: Path, observation: dict[str, str]
) -> Iterator[tuple[McpClient, Path, FifoCheckpoint, FifoCheckpoint]]:
    """Hold the first matching recording write until the release checkpoint."""
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with (
            closing(FifoCheckpoint.create(root / "observed")) as observed,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                DIRECT.serve(
                    "--worker",
                    str(Path(__file__).resolve().parents[3] / "fixtures/zod"),
                ),
                {
                    **os.environ,
                    LOADER_VARIABLE: str(
                        build_interposer(root, "recording_observation")
                    ),
                    **observation,
                    "MCP_CONSOLE_TEST_JOURNAL_OBSERVED": str(observed.path),
                    "MCP_CONSOLE_TEST_JOURNAL_RELEASE": str(release.path),
                },
                root,
                response_timeout=15,
            ) as client,
        ):
            client.initialize_and_list_tools()
            yield client, root, observed, release


def response_text(entry: dict) -> str:
    return "".join(
        block["text"] for block in entry["result"]["content"] if block["type"] == "text"
    )


def completed_calls(root: Path) -> list[dict]:
    (recording,) = (root / ".agents/console/sessions").iterdir()
    events = recording_events(recording)
    calls = [e["call_id"] for e in events if e["event"] == "tool_call"]
    results = [e["call_id"] for e in events if e["event"] == "tool_result"]
    assert calls == results == [1, 2, 3], (calls, results)
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
    with stalled_writer(binary, {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-001.txt"}) as (
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
    with stalled_writer(binary, {"MCP_CONSOLE_TEST_OBSERVED_FILE": "/cell-002.txt"}) as (
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


if __name__ == "__main__":
    run_this_suite(__file__)
