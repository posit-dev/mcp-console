#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.previews import cell_output_path
from support.assertions import large_output, last_tool_text
from support.previews import (
    TEXT_BUDGET,
    assert_preview,
    cell_text,
    normalize_preview_paths,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import SHARED_LIBRARY_FLAG
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.suites import run_this_suite

PENDING_TEXT_BUDGET = 8 * 1024 * 1024
CELL_OUTPUT_RETENTION_LIMIT = 1024 * 1024 * 1024


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_bounds_mcp_preview_and_retains_raw_stream_after_overflow(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        workspace = Path(temporary_directory)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            current_directory=workspace,
            umask=0,
        )
        client.initialize_and_list_tools()

        client.send(r="overflow console output")
        overflow = client.transcript[-1]
        output = last_tool_text(client)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        relative_output = Path("capture/sessions/001/cell-001.txt")
        public_output = (
            f".agents/console/sessions/{session.name}/{relative_output.as_posix()}"
        )
        omitted = assert_preview(output, "x" * (PENDING_TEXT_BUDGET + 7))
        assert f"raw log: {public_output}" in output

        output_path = session / relative_output
        assert output_path.read_text(encoding="utf-8") == "x" * (
            PENDING_TEXT_BUDGET + 7
        )
        assert output_path.stat().st_mode & 0o777 == 0o400, output_path

        events = [
            json.loads(line)
            for line in (session / "capture" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        cell_output = next(event for event in events if event["event"] == "cell_output")
        assert {
            key: cell_output[key]
            for key in (
                "call_id",
                "path",
                "retained_bytes",
                "inline_omitted_bytes",
                "discarded_bytes",
                "retention_limit_bytes",
            )
        } == {
            "call_id": 1,
            "path": relative_output.as_posix(),
            "retained_bytes": PENDING_TEXT_BUDGET + 7,
            "inline_omitted_bytes": omitted,
            "discarded_bytes": 0,
            "retention_limit_bytes": CELL_OUTPUT_RETENTION_LIMIT,
        }, cell_output
        markdown = (session / "transcript.md").read_text(encoding="utf-8")
        assert f"[Retained text output for call 1](<{relative_output}>)" in markdown
        assert relative_output.as_posix() not in (
            session / "sessions/session-001.qmd"
        ).read_text(encoding="utf-8")

        overflow["result"]["content"][0]["text"] = output.replace(
            session.name, "<run ID>"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        assert (cell_output_path(session, 2)).read_text(
            encoding="utf-8"
        ) == "zod: echo\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_orders_failure_and_replacement_output(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary_directory:
        interposer = Path(temporary_directory) / "relay-stdout-read.dylib"
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                interposer,
                fixtures / "native" / "relay_stdout_read_interposer.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_RELAY_READ_DYLIB"] = str(interposer)
        # Hold the final raw read until retirement joins it. The worker waits
        # for that read before sending the protocol violation.
        environment["MCP_CONSOLE_TEST_RELAY_READ_MATCH"] = "zod stdout tail\n"
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "retirement_read_relay"),
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()

            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            client.send(r="violate protocol after stdout")
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert len(result["content"]) == 1, result
            output = result["content"][0]["text"]
            assert len(output.encode("utf-8")) <= TEXT_BUDGET
            raw = large_output("zod old stdout\n") + "zod stdout tail\n"
            notices = [
                "[worker sent an unexpected ready message]",
                "[worker terminated by signal 9]",
                "[worker stopped: in-memory state lost]",
                "[starting new worker]",
                "[idle]",
            ]
            assert cell_text(client, 2) == raw
            assert all(output.count(notice) == 1 for notice in notices), repr(output)
            assert [output.index(notice) for notice in notices] == sorted(
                output.index(notice) for notice in notices
            ), repr(output)
            assert output.endswith("\n".join(notices)), output[-500:]
            assert_preview(output.removesuffix("\n".join(notices)), raw)
            normalize_preview_paths(client)

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_raw_output_during_forced_stop(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"force stop after raw {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert len(result["content"]) == 1, result
        output = result["content"][0]["text"]
        raw = f"zod retiring {stream}: �"
        notices = [
            "[worker sent an unexpected ready message]",
            "[worker terminated by signal 9]",
            "[worker stopped: in-memory state lost]",
            "[starting new worker]",
            "[idle]",
        ]
        assert output.count(raw) == 1, repr(output)
        assert all(output.count(notice) == 1 for notice in notices), repr(output)
        assert [output.index(notice) for notice in notices] == sorted(
            output.index(notice) for notice in notices
        ), repr(output)
        remainder = output.replace(raw, "")
        for notice in notices:
            remainder = remainder.replace(notice, "")
        assert not remainder.replace("\n", ""), repr(output)
        result["content"][0]["text"] = (
            f"{raw}\n<cross-source position follows serialized observation>\n"
            + "\n".join(notices)
        )

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
