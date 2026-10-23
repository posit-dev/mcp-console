#!/usr/bin/env -S uv run --script

import base64
import json
import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.previews import cell_output_path
from support.progress import without_elapsed
from support.allocations import AllocationProfile
from support.assertions import last_tool_text
from support.previews import (
    CONTROL_OMISSION,
    OMISSION,
    assert_preview,
    normalize_preview_paths,
    recording_events,
    session_directory,
)
from support.client import McpClient
from support.checkpoints import (
    FifoCheckpoint,
    release_fixture_checkpoint,
    wait_for_worker_file,
    wait_for_checkpoint,
)
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import startup_r_package
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite
from boundaries.client_server._harness import ZodFixtureControl

TEXT_BUDGET = 8 * 1024


def check_preview(binary: Path, execution: Execution, scenario: str) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=Path(temporary),
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r=scenario)
        result = client.transcript[-1]["result"]
        text = "".join(
            block["text"] for block in result["content"] if block["type"] == "text"
        )
        assert len(text.encode("utf-8")) <= TEXT_BUDGET, len(text.encode("utf-8"))
        assert text.startswith("preview head\n"), text[:300]
        assert "preview tail: final diagnostic\n" in text, text[-300:]
        assert [block["type"] for block in result["content"]] == [
            "text",
            "image",
            "text",
        ]
        assert result["content"][-1]["text"] == "after final image\n"
        assert not result.get("isError", False), result
        session = next((Path(temporary) / ".agents/console/sessions").iterdir())
        raw = (cell_output_path(session, 1)).read_bytes()
        if scenario == "preview huge line":
            body = "x" * (16 * 1024 * 1024)
        elif scenario == "preview tiny events":
            body = "ab" * 12000
        else:
            assert scenario == "preview redraw", scenario
            body = ("\r" + "x" * 2048) * 5000 + "\rprogress final"
        emitted = "preview head\n" + body + "\npreview tail: final diagnostic\n"
        assert raw == (emitted + "after final image\n").encode("utf-8")
        if scenario == "preview redraw":
            assert (
                text
                == """preview head
progress final
preview tail: final diagnostic
after final image
"""
            )
        else:
            assert_preview(result["content"][0]["text"], emitted)
            assert (
                f"raw log: .agents/console/sessions/{session.name}/capture/sessions/001/cell-001.txt"
                in text
            )
        for block in result["content"]:
            if block["type"] == "text":
                block["text"] = block["text"].replace(session.name, "<run ID>")
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        client.send(r="echo fresh")
        assert last_tool_text(client) == "zod: fresh\n"
        return client.finish()


@requires(POSIX, NATIVE_FIXTURES)
@executions(DIRECT, SANDBOXED)
def test_summarizes_empty_cell_after_oversized_startup_output(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        raw = "startup head\n" + "s" * 32768 + "\nstartup tail\n"
        with (
            closing(FifoCheckpoint.create(root / "startup-reached")) as reached,
            closing(FifoCheckpoint.create(root / "startup-release")) as release,
            closing(FifoCheckpoint.create(root / "worker-ready")) as ready,
        ):
            # fmt: r
            source = code(f"""
                cat("startup head\\n", strrep("s", 32768L), "\\nstartup tail\\n", sep = "")
                ready <- fifo(
                  {
                    json.dumps(str(reached.path))
                  },
                  "wb",
                  blocking = TRUE
                )
                writeBin(charToRaw("1"), ready)
                close(ready)
                gate <- fifo(
                  {
                    json.dumps(str(release.path))
                  },
                  "rb",
                  blocking = TRUE
                )
                readBin(gate, "raw", 1L)
                """)
            with startup_r_package(root, source) as environment:
                environment["RETICULATE_PYTHON"] = sys.executable
                environment.update(
                    {
                        LOADER_VARIABLE: str(
                            build_interposer(root, "recording_observation")
                        ),
                        "MCP_CONSOLE_TEST_OBSERVED_TEXT": "\nstartup tail\n",
                        "MCP_CONSOLE_TEST_JOURNAL_OBSERVED": str(ready.path),
                    }
                )
                args = (
                    execution.serve("--writable-root", str(root))
                    if execution == SANDBOXED
                    else execution.serve()
                )
                with McpClient(binary, args, environment, root) as client:
                    try:
                        client.initialize_and_list_tools()
                        reached.wait("startup output emitted", timeout=60)
                        sessions = root / ".agents/console/sessions"
                        assert not sessions.exists()
                        ready.wait(
                            "startup output drained before the first send",
                            timeout=60,
                            client=client,
                        )
                        assert not sessions.exists()
                        release.release()
                        result = client.send(python="pass")
                        assert not result["isError"], result
                        log = wait_for_checkpoint(
                            lambda: next(
                                (
                                    path
                                    for path in sessions.glob(
                                        "*/capture/sessions/001/startup.txt"
                                    )
                                    if path.read_text() == raw
                                ),
                                None,
                            ),
                            "startup output recorded outside a cell",
                            root=sessions,
                            recursive=True,
                            client=client,
                        )
                        text = last_tool_text(client)
                        assert len(text.encode()) <= TEXT_BUDGET, text
                        assert text.startswith("startup head\n") and text.endswith(
                            "startup tail\n"
                        ), text
                        session = log.parents[3]
                        assert (cell_output_path(session, 1)).read_bytes() == b""
                        assert (
                            str(
                                Path(".agents/console/sessions")
                                / session.name
                                / "capture/sessions/001/startup.txt"
                            )
                            in text
                        ), text
                        client.expect("42\n", python="42")
                        client.finish()
                    finally:
                        release.release()
        return [{"startup_preview_and_empty_cell_retained": True}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_keeps_actual_tail_and_image_after_huge_line(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview huge line")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_keeps_actual_tail_after_tiny_event_flood(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview tiny events")


@requires(NATIVE_FIXTURES)
def test_bounds_metadata_for_alternating_tiny_events(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        closing(AllocationProfile(Path(temporary))) as profile,
        McpClient(
            binary,
            DIRECT.serve("--worker", str(worker)),
            {**os.environ, **profile.environment},
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="echo ready")
        profile.start()
        client.send(r="preview alternating bytes")
        text = last_tool_text(client)
        assert client.request("ping")["result"] == {}
        _, largest = profile.stop()
        assert text == "ab" * 4096
        # This fits the text budget. Retaining one part per event would allocate
        # a large vector and repeatedly scan it before any text is omitted.
        assert largest <= 128 * 1024, largest
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_edits_mcp_unicode_preview_without_changing_raw_stream(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        for call_id, (scenario, edits, projected) in enumerate(
            (
                (
                    "preview unicode suffix",
                    "\b🙂\b\r\n",
                    ("a€🙂b" * 10000)[:-1] + "\r\n",
                ),
                (
                    "preview unicode replacement",
                    "\b" * 8192 + "\r\bfinal 🙂\bframe\r\n",
                    "final frame\r\n",
                ),
            )
        ):
            result = client.send(r=scenario)
            assert not result["isError"], result
            assert len(result["content"]) == 1, result
            text = result["content"][0]["text"]
            projected = (
                "preview head\n" + projected + "preview tail: final diagnostic\n"
            )
            if call_id == 0:
                assert_preview(text, projected)
            else:
                assert text == projected, text
            raw = (
                "preview head\n"
                + "a€🙂b" * 10000
                + edits
                + "preview tail: final diagnostic\n"
            ).encode()
            assert (
                cell_output_path(session_directory(client), 1 + 2 * call_id)
            ).read_bytes() == raw
            assert client.send()["content"] == [{"type": "text", "text": "\n[idle]"}]
        normalize_preview_paths(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_records_raw_unicode_newlines_and_literal_mcp_preview(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        result = client.send(r="preview unicode lines")
        assert not result["isError"], result
        assert len(result["content"]) == 1, result
        text = result["content"][0]["text"]
        line = "line €🙂 " + "x" * 48 + "\r\n"
        raw = "preview head\n" + line * 512 + "preview tail 🙂\r\n"
        session = session_directory(client)
        recorded_raw = (cell_output_path(session, 1)).read_bytes()
        assert recorded_raw == raw.encode("utf-8")
        assert len(recorded_raw) == 32288
        assert recorded_raw.count(b"\r\n") == 513
        assert recorded_raw.count(b"\n") == 514
        assert_preview(text, raw)
        assert text.endswith("preview tail 🙂\r\n"), repr(text[-100:])
        assert "�" not in text
        assert text.count("\r\n") > 1
        assert (
            f"raw log: .agents/console/sessions/{session.name}/capture/sessions/001/cell-001.txt"
            in text
        )
        normalize_preview_paths(client)
        assert result["content"][0]["text"] == text.replace(session.name, "<run ID>")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_compacts_redraws_before_preview_limits(
    binary: Path, execution: Execution
) -> Transcript:
    return check_preview(binary, execution, "preview redraw")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_compacts_ansi_redraws_and_discards_oversized_controls(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        result = client.send(r="preview ansi redraw")
        assert [block["type"] for block in result["content"]] == [
            "text",
            "image",
            "text",
        ]
        assert result["content"][0]["text"] == (
            "preview head\nprogress final\npreview tail: final diagnostic\n"
        ), result
        assert result["content"][-1]["text"] == "after final image\n", result
        assert (
            sum(
                len(block["text"].encode())
                for block in result["content"]
                if block["type"] == "text"
            )
            <= TEXT_BUDGET
        )
        raw = (
            "preview head\n"
            + "x" * 32768
            + "\x1b[2K"
            + "\x1b["
            + "0;" * 100000
            + "2K"
            + "\x1b]52;c;"
            + "hidden\n" * 100000
            + "\x1b\\"
            + ("\r\x1b[2K\x1b[32m" + "x" * 2048 + "\x1b[0m") * 5000
            + "\r\x1b[K\x1b[32mprogress final\x1b[0m\npreview tail: final diagnostic\n"
            + "\x1b]unterminated before imageafter final image\n"
        ).encode()
        assert (cell_output_path(session_directory(client), 1)).read_bytes() == raw
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_raw_and_rendered_counts_for_invalid_utf8(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "server_relay/scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "preview_raw",
            },
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="42")
        text = last_tool_text(client)
        raw = b"raw head\n" + "€".encode() + b"\xff" * 20000 + "€ raw tail\n".encode()
        assert_preview(text, raw.decode("utf-8", errors="replace"))
        assert client.temporary_directory is not None
        session = next(
            (
                Path(client.temporary_directory.name) / ".agents/console/sessions"
            ).iterdir()
        )
        assert (cell_output_path(session, 1)).read_bytes() == raw
        normalize_preview_paths(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_keeps_tail_when_recording_is_disabled(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        (workspace / ".agents").mkdir()
        (workspace / ".agents/console").mkdir()
        (workspace / ".agents/console/sessions").write_text("occupied")
        with McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="preview huge line")
            text = "".join(
                block["text"]
                for block in client.transcript[-1]["result"]["content"]
                if block["type"] == "text"
            )
            assert len(text.encode()) <= TEXT_BUDGET
            assert text.startswith("preview head\n")
            assert text.endswith("preview tail: final diagnostic\nafter final image\n")
            assert "no retained log" in text and "omitted text unavailable" in text
            assert "cell-001.txt" not in text
            transcript, stderr = client.finish_with_standard_error()
            assert stderr.count("transcript recording disabled:") == 1, stderr
            return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_active_prompt_and_state_after_text_flood(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control, ExitStack() as resources:
        control.configure(environment)
        client = resources.enter_context(
            McpClient(binary, execution.serve("--worker", str(worker)), environment)
        )
        client.initialize_and_list_tools()
        client.send(r="preview prompt")
        control.connect(client)
        text = last_tool_text(client)
        assert len(text.encode()) <= TEXT_BUDGET
        assert text.startswith("prompt output head\n")
        assert "prompt output tail\n" in text
        assert 'input requested: "prompt head ' in text and ' prompt tail> "]' in text
        assert text.endswith("[waiting for stdin]")
        prompt = text[text.index("[input requested:") :].removesuffix(
            "[waiting for stdin]"
        )
        assert_preview(
            prompt,
            '[input requested: "prompt head ' + "p" * 20000 + ' prompt tail> "]\n',
            pattern=CONTROL_OMISSION,
        )
        normalize_preview_paths(client)
        client.send(stdin="answer\n", timeout_ms=0)
        assert last_tool_text(client) == "\n[waiting for stdin]"
        control.send_control(0, "read_preview_input")
        control.wait_for(0, "preview_input_processed")
        client.send()
        assert last_tool_text(client) == "received answer\n", last_tool_text(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_image_overflow_preserves_later_text_and_fitting_image(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        client.send(r="preview image limit")
        content = client.transcript[-1]["result"]["content"]
        assert [block["type"] for block in content] == ["text", "image", "text"]
        session = session_directory(client)
        capture = recording_events(session)
        omitted, artifact = [e for e in capture if e["event"] == "image_retained"]
        path = Path(".agents/console/sessions") / session.name / omitted["path"]
        assert content[0]["text"] == (
            "before oversized image\n\n"
            f"[output omitted: 1 image (8388612 encoded bytes); retained image: {path}]\n"
            "before accepted image\n"
        )
        assert content[-1]["text"] == "after accepted image\n"
        owner = {"session_id": 1, "cell_id": 1, "interval": "cell"}
        assert omitted["owner"] == artifact["owner"] == owner
        assert omitted["bytes"] == 6 * 1024 * 1024 + 3
        assert omitted["mime_type"] == artifact["mime_type"] == "image/png"
        assert omitted["sequence"] < artifact["sequence"]
        assert not any(e["event"] == "image_omitted" for e in capture)
        image = session / artifact["path"]
        assert sorted((session / "capture/sessions").glob("*/*.png")) == [
            session / omitted["path"],
            image,
        ]
        assert image.read_bytes() == base64.b64decode(content[1]["data"])
        (recorded_image,) = [
            block
            for event in capture
            if event["event"] == "tool_result"
            for block in event["result"]["content"]
            if block["type"] == "image"
        ]
        assert recorded_image["path"] == artifact["path"]
        assert recorded_image["artifactId"] == artifact["artifact_id"]
        normalize_preview_paths(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_validates_images_before_omitting_them(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with McpClient(binary, execution.serve("--worker", str(worker))) as client:
        client.initialize_and_list_tools()
        result = client.send(r="preview invalid oversized image")
        assert result["isError"], result
        assert result["content"] == [
            {
                "type": "text",
                "text": "before invalid image\n"
                "[worker returned invalid base64 image data: Invalid symbol 63, offset 8388610.]\n"
                "[worker terminated by signal 9]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n[idle]",
            }
        ], result
        assert (
            list((session_directory(client) / "capture/sessions").glob("*/*.png")) == []
        )
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_combines_old_worker_and_replacement_cell_under_one_budget(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(worker)),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        # Finish shared discovery and lazy worker startup before replacement.
        client.send(requirements={"action": "get"})
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"
        client.send(r="overflow cell output file", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        release = wait_for_worker_file(
            Path(temporary), "zod-release-spooled-output", client
        )
        with closing(
            FifoCheckpoint.attach(release.with_name("zod-spooled-output-processed"))
        ) as processed:
            release_fixture_checkpoint(release, client=client)
            processed.wait(
                "old worker output observed", timeout=client.response_timeout
            )
        client.send(control="restart", r="preview huge line")
        result = client.transcript[-1]["result"]
        text = "".join(
            block["text"] for block in result["content"] if block["type"] == "text"
        )
        assert len(text.encode()) <= TEXT_BUDGET
        assert text.startswith("x")
        assert (
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]"
        ) in text
        assert "preview tail: final diagnostic\n" in text
        assert text.endswith("after final image\n[done]")
        assert text.count("[output omitted:") == 1
        assert (
            f"retained logs: .agents/console/sessions/{session_directory(client).name}/capture/sessions/"
            in text
        )
        assert not result.get("isError", False), result
        normalize_preview_paths(client)
        client.send()
        assert last_tool_text(client) == "\n[idle]"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_keeps_partial_idle_utf8_out_of_cell_omission_counts(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "server_relay/scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "preview_raw_prelude",
            },
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="42")
        text = last_tool_text(client)
        idle = (b"idle head\n" + b"s" * 20000 + b"\xe2").decode(errors="replace")
        cell = "cell head\n" + "x" * 32768 + "\ncell tail\n"
        (marker,) = OMISSION.finditer(text)
        head = text[: marker.start()]
        boundary, tail = text[marker.end() :].split("\n[…]\n")
        assert boundary == ""
        assert idle.startswith(head) and cell.endswith(tail)
        idle_omitted = len(idle.encode()) - len(head.encode())
        cell_omitted = len(cell.encode()) - len(tail.encode())
        assert int(marker[1]) == idle_omitted + cell_omitted
        assert "retained logs:" in marker[0]
        session = session_directory(client)
        assert (
            f".agents/console/sessions/{session.name}/capture/sessions/001/"
            in marker[0]
        )
        assert (session / "capture/sessions/001/startup.txt").read_text(
            errors="replace"
        ) == idle
        assert (cell_output_path(session, 1)).read_text() == cell
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        cell_summaries = [event for event in events if event["event"] == "cell_output"]
        assert cell_summaries[-1]["inline_omitted_bytes"] == cell_omitted
        normalize_preview_paths(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
