#!/usr/bin/env -S uv run --script

import base64
import json
import os
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import execution_snapshots
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.progress import RUNNING, elapsed_progress, without_elapsed
from support.previews import (
    TEXT_BUDGET,
    assert_preview,
    cell_text,
    compact_previews,
    normalize_preview_paths,
)
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_silent_polls_retain_admission_age(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "intervals.json").write_text("[]")
        with (
            closing(FifoCheckpoint.create(root / "progress-release")) as release,
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
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "progress_polls",
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_PROGRESS_INTERVALS": str(root / "intervals.json"),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            # Resolve connection startup before measuring the admitted cell's
            # age; discovery time precedes that clock.
            client.send(requirements={"action": "get"})
            # A public restart also waits for this lazy relay's transport.
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            client.send(r="42", timeout_ms=250)
            first_age, silent = elapsed_progress(last_tool_text(client))
            assert first_age >= 0.2 and silent
            assert without_elapsed(last_tool_text(client)) == RUNNING
            # Immediate polls must retain the accepted cell's age, including a
            # later bounded observation. No exact wall-clock value is promised.
            for timeout_ms in (0, 100, 0):
                client.send(timeout_ms=timeout_ms)
                age, silent = elapsed_progress(last_tool_text(client))
                assert age >= first_age and silent
                assert without_elapsed(last_tool_text(client)) == RUNNING
                first_age = age
            release.release()
            client.send()
            assert last_tool_text(client) == "[done]"
            client.send()
            assert last_tool_text(client) == "\n[idle]"
            return client.finish()


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_worker_activity_survives_preview_projection(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    png = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
        "AAAAASUVORK5CYII="
    )
    overflow = "x" * (8 * 1024 * 1024 + 7)
    # Each row states the received events, rendered text, and worker activity.
    intervals = {
        "text": (
            [{"kind": "console_output", "data": "worker text\n"}],
            "worker text\n",
            True,
        ),
        "diagnostic": (
            [{"kind": "console_diagnostic", "data": "worker diagnostic\n"}],
            "worker diagnostic\n",
            True,
        ),
        "partial UTF-8": (
            [{"kind": "stdout_bytes", "data": base64.b64encode(b"\xe2").decode()}],
            "",
            True,
        ),
        "finished UTF-8": (
            [
                {
                    "kind": "stdout_bytes",
                    "data": base64.b64encode(b"\x82\xac\n").decode(),
                }
            ],
            "€\n",
            True,
        ),
        "stderr": (
            [{"kind": "stderr", "data": "worker stderr\n"}],
            "worker stderr\n",
            True,
        ),
        "image": ([{"kind": "image", "data": png, "mime_type": "image/png"}], "", True),
        "erased text": (
            [{"kind": "console_output", "data": "erase\r\x1b[2K"}],
            "",
            True,
        ),
        "overflow": ([{"kind": "console_output", "data": overflow}], None, True),
        "omitted image": (
            [{"kind": "image", "data": "AAAA", "mime_type": "m" * 65537}],
            None,
            True,
        ),
        "callback only": ([], "", False),
        "native diagnostic": (
            [
                {
                    "kind": "native_stderr",
                    "data": base64.b64encode(b"native diagnostic\n").decode(),
                }
            ],
            "native diagnostic\n",
            False,
        ),
    }
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        (root / "intervals.json").write_text(
            json.dumps([events for events, _, _ in intervals.values()])
        )
        roots = ("--writable-root", temporary) if execution == SANDBOXED else ()
        with (
            closing(FifoCheckpoint.create(root / "progress-release")) as release,
            closing(FifoCheckpoint.create(root / "progress-processed")) as processed,
            McpClient(
                binary,
                execution.serve(
                    "--worker",
                    str(fixtures / "zod"),
                    "--relay",
                    str(fixtures / "server_relay/scripted_relay.py"),
                    *roots,
                ),
                {
                    **os.environ,
                    "TMPDIR": temporary,
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "progress_polls",
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_PROGRESS_INTERVALS": str(root / "intervals.json"),
                },
            ) as client,
        ):
            client.initialize_and_list_tools()
            # Finish connection discovery and this lazy relay's transport
            # startup before observing the admitted cell's activity.
            client.send(requirements={"action": "get"})
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            client.send(r="42", timeout_ms=0)
            assert elapsed_progress(last_tool_text(client))[1]
            assert without_elapsed(last_tool_text(client)) == RUNNING
            for name, (_, expected, worker_activity) in intervals.items():
                release.release()
                processed.wait(f"{name} interval ingested before its response cut")
                if name == "native diagnostic":
                    # Native stderr is an independent reader. Its public text,
                    # rather than the sideband receipt, proves that it arrived.
                    wait_for_evaluation_output(
                        client,
                        expected + RUNNING,
                        "native diagnostic reached its output cut",
                        timeout_ms=0,
                    )
                    result = client.transcript[-1]["result"]
                else:
                    result = client.send(timeout_ms=0)
                text = "".join(
                    part["text"] for part in result["content"] if part["type"] == "text"
                )
                assert elapsed_progress(text)[1] == (not worker_activity), (
                    name,
                    result,
                )
                assert len(text.encode()) <= TEXT_BUDGET
                output = without_elapsed(text).removesuffix(RUNNING)
                if expected is not None:
                    assert output == expected, (name, result)
                if name == "image":
                    assert result["content"][0] == {
                        "type": "image",
                        "data": png,
                        "mimeType": "image/png",
                    }
                if name == "overflow":
                    assert_preview(output, overflow)
                if name == "omitted image":
                    assert "1 image (4 encoded bytes)" in output, output
                    assert all(part["type"] == "text" for part in result["content"])
                # No later publications are released: the next cut is silent.
                client.send(timeout_ms=0)
                assert elapsed_progress(last_tool_text(client))[1]
                assert without_elapsed(last_tool_text(client)) == RUNNING
            release.release()
            client.send()
            assert last_tool_text(client) == "[done]"
            assert cell_text(client, 3) == (
                "worker text\nworker diagnostic\n€\nworker stderr\nerase\r\x1b[2K"
                + overflow
                + "native diagnostic\n"
            )
            normalize_preview_paths(client)
            compact_previews(client, "x")
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_starts_a_new_admission_clock(
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
        # Measure cell admission after the lazy worker reaches readiness.
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        client.send(r="stall", timeout_ms=250)
        old_age, silent = elapsed_progress(last_tool_text(client))
        assert old_age >= 0.2 and silent
        # Restart only dispatched cells, so both retirement notices have the
        # same public meaning regardless of startup speed.
        marker = wait_for_worker_file(Path(temporary), "zod-stalled", client)
        marker.unlink()
        started = time.monotonic()
        client.send(control="restart", r="stall", timeout_ms=0)
        new_age, silent = elapsed_progress(last_tool_text(client))
        # Admission follows retirement; its age cannot include the first cell.
        assert new_age <= time.monotonic() - started + 0.05 and silent
        wait_for_worker_file(Path(temporary), "zod-stalled", client)
        client.send(control="restart")
        assert last_tool_text(client).endswith("[idle]")
        client.send(r="echo after restart")
        assert last_tool_text(client) == "zod: after restart\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
