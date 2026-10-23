#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.previews import cell_output_path
from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.previews import OMISSION, TEXT_BUDGET, assert_preview
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_names_readable_logs_once_with_long_unicode_paths(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        workspace = root / "workspace"
        workspace.mkdir()
        for compact in (False, True):
            recording = (
                root.joinpath(*(["recording-" + "é" * 70] * 4))
                if compact
                else root / "recording"
            )
            recording.mkdir(parents=True)
            with McpClient(
                binary,
                execution.serve(),
                {**os.environ, "MCP_CONSOLE_HOME": str(recording)},
                workspace,
                record_in_project=False,
                use_home_configuration=True,
            ) as client:
                client.initialize_and_list_tools()
                client.expect("normal 🙂", python="print('normal 🙂', end='')")
                client.send(python="print('a€🙂b' * 4000, end='')")
                text = last_tool_text(client)
                assert_preview(text, "a€🙂b" * 4000)
                session = next((recording / "sessions").iterdir())
                path = cell_output_path(session, 2)
                assert path.read_text() == "a€🙂b" * 4000
                (marker,) = OMISSION.finditer(text)
                _, advertised_path = marker[0].split("; raw log: ", 1)
                advertised_path = advertised_path.removesuffix("]\n")
                if compact:
                    suffix = " (relative to Console recording directory)"
                    assert advertised_path.endswith(suffix)
                    assert recording / advertised_path.removesuffix(suffix) == path
                else:
                    assert Path(advertised_path) == path
                assert text.count(str(recording)) == int(not compact)
                client.send()
                assert last_tool_text(client) == "\n[idle]"
                client.finish()
    return [
        {
            "normal_text_exact": True,
            "long_unicode_log_readable": True,
            "utf8_head_tail_and_omission_exact": True,
            "poll_consumed_notice": True,
        }
    ]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_does_not_offer_logs_after_recording_failures(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    for failure in ("cell file creation", "recording disabled"):
        with McpClient(binary, execution.serve("--worker", str(worker))) as client:
            client.initialize_and_list_tools()
            client.expect("zod: ready\n", r="echo ready")
            assert client.temporary_directory is not None
            sessions = (
                Path(client.temporary_directory.name) / ".agents/console/sessions"
            )
            session = next(sessions.iterdir())
            if failure == "cell file creation":
                (session / "capture/sessions/001/cell-002.txt").mkdir()
            else:
                (session / "capture/sessions/001/cell-002-001.png").mkdir()
                client.send(r="emit image")
            client.send(r="preview huge line")
            text = "".join(
                block["text"]
                for block in client.transcript[-1]["result"]["content"]
                if block["type"] == "text"
            )
            assert len(text.encode()) <= TEXT_BUDGET
            (marker,) = OMISSION.finditer(text)
            assert marker[0].endswith("; no retained log; omitted text unavailable]\n")
            preview = text
            if failure == "cell file creation":
                diagnostic, preview = text.split("\n", 1)
                assert diagnostic.startswith("[cell output file was not created:")
            emitted = (
                "preview head\n"
                + "x" * (16 * 1024 * 1024)
                + "\npreview tail: final diagnostic\nafter final image\n"
            )
            assert_preview(preview, emitted)
            cell_id = 2 if failure == "cell file creation" else 3
            assert not (
                session / f"capture/sessions/001/cell-{cell_id:03}.txt"
            ).is_file()
            client.send()
            assert last_tool_text(client) == "\n[idle]"
            _, stderr = client.finish_with_standard_error()
            assert stderr.count("transcript recording disabled:") == int(
                failure == "recording disabled"
            )
    return [
        {
            "failed_cell_file_not_offered": True,
            "disabled_recording_not_offered": True,
            "head_tail_and_budget_preserved": True,
            "poll_consumed_notice": True,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
