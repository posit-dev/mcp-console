#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.previews import cell_output_path
from support.progress import without_elapsed
from support.assertions import last_tool_text
from support.checkpoints import (
    FifoCheckpoint,
    release_fixture_checkpoint,
    wait_for_worker_file,
)
from support.previews import (
    OMISSION,
    TEXT_BUDGET,
    assert_preview,
    compact_previews,
    normalize_preview_paths,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite
from support.requirements import POSIX, requires

PENDING_TEXT_BUDGET = 8 * 1024 * 1024


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_separates_startup_omissions_from_retained_cell_text(
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
                str(fixtures / "server_relay" / "scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "startup_output",
            },
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="42")
        output = last_tool_text(client)
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        path = (
            f".agents/console/sessions/{session.name}/capture/sessions/001/cell-001.txt"
        )
        assert (workspace / path).read_bytes() == b"cell output\n"
        assert len(output.encode()) <= TEXT_BUDGET
        assert output.endswith("cell output\n"), output[-1000:]
        marker = OMISSION.search(output)
        assert marker is not None
        assert "capture/sessions/001/startup.txt" in marker[0]
        assert (session / "capture/sessions/001/startup.txt").read_text() == "s" * (
            PENDING_TEXT_BUDGET + 7
        )
        assert path not in marker[0]
        startup_preview = output.removesuffix("cell output\n")
        assert_preview(startup_preview, "s" * (PENDING_TEXT_BUDGET + 7))
        client.transcript[-1]["result"]["content"][0]["text"] = output
        normalize_preview_paths(client)
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_partial_retention_and_later_unretained_output(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    file_limit = 3 * PENDING_TEXT_BUDGET + 3
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        launcher = workspace / "limited-server"
        # Limit regular-file writes using the OS, while leaving enough room for
        # the journal and both projections of the two bounded tool responses.
        launcher.write_text(
            f"#!{sys.executable}\n"
            # fmt: python
            + code(f"""
                import os
                import resource
                import signal
                import sys

                signal.signal(signal.SIGXFSZ, signal.SIG_IGN)
                resource.setrlimit(resource.RLIMIT_FSIZE, ({file_limit}, {file_limit}))
                os.execv({str(binary)!r}, [{str(binary)!r}, *sys.argv[1:]])
                """),
            encoding="utf-8",
        )
        launcher.chmod(0o755)
        with McpClient(
            launcher,
            execution.serve("--worker", str(fixtures / "zod")),
            {**os.environ, "TMPDIR": temporary},
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            # Finish shared discovery and lazy worker startup before retention.
            client.send(requirements={"action": "get"})
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            client.send(r="overflow cell output file", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            release = wait_for_worker_file(
                workspace, "zod-release-spooled-output", client
            )
            with closing(
                FifoCheckpoint.attach(release.with_name("zod-spooled-output-processed"))
            ) as processed:
                # Keep each batch pending until the server has processed it.
                # Intermediate polls would reset the inline budget and split counts.
                release_fixture_checkpoint(release, client=client)
                processed.wait(timeout=client.response_timeout)
                client.send(timeout_ms=0)
                first = client.transcript[-1]
                first_text = last_tool_text(client)
                release_fixture_checkpoint(release, client=client)
                processed.wait(timeout=client.response_timeout)
                client.send(timeout_ms=0)
                second = client.transcript[-1]
                second_text = last_tool_text(client)

            session = next((workspace / ".agents/console" / "sessions").iterdir())
            path = cell_output_path(session, 3)
            assert path.read_bytes() == b"x" * file_limit
            assert f"stopped after {file_limit} retained bytes" in first_text
            assert "later text is not retained in this file" in first_text
            assert "logs contain prefixes" in first_text
            assert "later raw text unavailable" in second_text
            assert first_text.endswith("\n[running; poll with an empty send]")
            # File-failure notices are separate from the emitted byte stream.
            failure_start = first_text.index("[cell output file ")
            failure_end = first_text.index("]\n", failure_start) + 2
            first_payload = (
                first_text[:failure_start].removesuffix("\n") + first_text[failure_end:]
            )
            first_payload = without_elapsed(first_payload).removesuffix(
                "\n[running; poll with an empty send]"
            )
            first_omitted = assert_preview(
                first_payload, "x" * (3 * PENDING_TEXT_BUDGET + 7)
            )
            second_omitted = assert_preview(
                second_text, "y" * (PENDING_TEXT_BUDGET + 7)
            )
            for entry, text in ((first, first_text), (second, second_text)):
                entry["result"]["content"][0]["text"] = text.replace(
                    session.name, "<run ID>"
                )

            client.send(r="echo after failure")
            assert last_tool_text(client) == "zod: after failure\n"
            assert (
                cell_output_path(session, 6)
            ).read_bytes() == b"zod: after failure\n"
            events = [
                json.loads(line)
                for line in (session / "capture/events.jsonl").read_text().splitlines()
            ]
            output_event = next(
                event for event in events if event["event"] == "cell_output"
            )
            assert output_event["retained_bytes"] == file_limit, output_event
            assert output_event["discarded_bytes"] == PENDING_TEXT_BUDGET + 11, (
                output_event
            )
            assert (
                output_event["inline_omitted_bytes"] == first_omitted + second_omitted
            ), output_event
            markdown = (session / "transcript.md").read_text()
            assert (
                f"{PENDING_TEXT_BUDGET + 11} raw bytes not retained in this file"
                in markdown
            )
            assert "permanently discarded" not in markdown
            compact_previews(client, "x", "y", "z", "s", "p", "ab")
            transcript, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
            return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_omitted_bytes_retained_at_the_file_limit(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(fixtures / "zod")),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        client.send(r="overflow cell retention limit", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        release = wait_for_worker_file(
            Path(temporary), "zod-release-retention-output", client
        )
        with closing(
            FifoCheckpoint.attach(release.with_name("zod-retention-completed"))
        ) as completed:
            release_fixture_checkpoint(release, client=client)
            # Keep all output pending until the server acknowledges completion;
            # intermediate polls would reset the inline budget and split counts.
            completed.wait(timeout=client.response_timeout)
        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        path = (
            f".agents/console/sessions/{session.name}/capture/sessions/001/cell-002.txt"
        )
        limit = 1024 * 1024 * 1024
        assert (workspace / path).stat().st_size == limit, (
            (workspace / path).stat().st_size,
            output[-1500:],
        )
        with (workspace / path).open("rb") as retained:
            block = b"x" * (1024 * 1024)
            for _ in range(1024):
                assert retained.read(len(block)) == block
            assert retained.read(1) == b""
        assert len(output.encode()) <= TEXT_BUDGET
        assert "tail\n" in output, output[-1500:]
        assert "logs contain prefixes; later raw text unavailable" in output
        assert f"cell output retention limit reached at {limit} bytes" in output
        omitted = sum(int(match[1]) for match in OMISSION.finditer(output))
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        summary = next(
            event
            for event in events
            if event["event"] == "cell_output" and event["call_id"] == 2
        )
        assert summary["retained_bytes"] == limit, summary
        assert summary["discarded_bytes"] == 5, summary
        assert summary["inline_omitted_bytes"] == omitted, summary
        client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
            session.name, "<run ID>"
        )
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
