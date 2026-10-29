#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CAPTURE_NAME,
    EVALUATION_OUTPUT_READY_NAME,
    PENDING_TEXT_BUDGET,
    PNG_1X1,
    PRELUDE_PROCESSED_NAME,
    PRELUDE_RELEASE_NAME,
    RETIREMENT_RELEASE_NAME,
    SHUTDOWN_RECEIVED_NAME,
    ServerRelayClient,
    _normalize_shutdown_grace,
    _receive_checkpointed,
)
from support.assertions import tool_text as _tool_text
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.previews import compact_previews, assert_preview, normalize_preview_paths
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.resolvers import fake_ir_environment as _fake_ir_environment
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_gracefully_shuts_down(binary: Path, execution: Execution) -> Transcript:
    client = ServerRelayClient(binary, "shutdown", execution=execution)
    assert _tool_text(client.send(control="restart")) == (
        "[starting new worker]\n[idle]"
    )
    return client.finish_shutdown()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_shutdown_precedes_blocked_resolver_cancellation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "blocked-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = root / "resolver-release"
        os.mkfifo(resolver_release)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release)
        armed = root / "shutdown-armed"
        order = root / "shutdown-order"
        environment[LOADER_VARIABLE] = str(
            build_interposer(root, "preparation_shutdown_order")
        )
        environment["MCP_CONSOLE_TEST_SHUTDOWN_ORDER_ARMED"] = str(armed)
        environment["MCP_CONSOLE_TEST_SHUTDOWN_ORDER_RECORD"] = str(order)

        client = ServerRelayClient(
            binary, "blocked_live_r_resolver_shutdown", environment, execution=execution
        )
        client.start_worker()
        relay_root = client.relay_root()
        capture = (relay_root / CAPTURE_NAME).open(encoding="utf-8")
        shutdown_received = FifoCheckpoint.attach(relay_root / SHUTDOWN_RECEIVED_NAME)
        retirement_release = FifoCheckpoint.attach(relay_root / RETIREMENT_RELEASE_NAME)
        finished = False
        try:
            preparation = client.client.start_send(
                r="must not execute during shutdown",
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait()
            armed.touch()
            client.client.stdin.close()
            shutdown_received.wait()
            # Shutdown receipt precedes resolver cancellation. This response
            # proves the cancelled resolver callback has now returned.
            _receive_checkpointed(
                client.client,
                preparation,
                "the cancelled R preparation",
            )
            result = preparation["result"]
            assert result.get("isError") is True, result
            assert result["content"] == [
                {"type": "text", "text": "R package resolution cancelled"}
            ], result
            retirement_release.release()
            client.client.finish()
            finished = True
            commands = order.read_text().splitlines()
            assert commands == [
                "shutdown",
                "control",
                "close",
            ], commands
            transcript = client._read_open_capture(capture)
        finally:
            if not finished:
                stop_client(client.client)
            capture.close()
            shutdown_received.close()
            retirement_release.close()
            resolver_started.close()
            client._temporary.cleanup()

    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert server_commands == shutdown, server_commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_cancelled_send_returns_owned_output_to_restart(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "cancelled_waiting_send", execution=execution)
    client.start_worker()
    relay_root = client.relay_root()
    prelude_release = FifoCheckpoint.attach(relay_root / PRELUDE_RELEASE_NAME)
    prelude_processed = FifoCheckpoint.attach(relay_root / PRELUDE_PROCESSED_NAME)
    output_ready = FifoCheckpoint.attach(relay_root / EVALUATION_OUTPUT_READY_NAME)
    shutdown_received = FifoCheckpoint.attach(relay_root / SHUTDOWN_RECEIVED_NAME)
    retirement_release = FifoCheckpoint.attach(relay_root / RETIREMENT_RELEASE_NAME)
    finished = False
    retirement_released = False
    try:
        prelude_release.release()
        prelude_processed.wait()

        waiting = client.client.start_send(r="42", timeout_ms=30_000)
        output_ready.wait()
        restart = client.client.start_send(control="restart")
        shutdown_received.wait()
        client.client.notify(
            "notifications/cancelled",
            requestId=waiting["id"],
            reason="acceptance test cancelled the waiting send",
        )
        cancellation = client.client.transcript[-1]["input"]["params"]
        assert cancellation["requestId"] == waiting["id"], cancellation
        cancellation["requestId"] = "<request ID>"
        retirement_release.release()
        retirement_released = True
        client.client.receive(restart)

        assert "result" not in waiting, waiting
        result = restart["result"]
        assert result["isError"] is True, result
        assert [content["type"] for content in result["content"]] == [
            "text",
            "image",
            "text",
            "image",
            "text",
        ], result
        assert result["content"][0]["text"] == "idle before image\n", result
        assert result["content"][1] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result
        assert result["content"][2]["text"] == (
            """idle after image
[output produced while idle]
cell before image
"""
        ), result
        assert result["content"][3] == {
            "type": "image",
            "data": PNG_1X1,
            "mimeType": "image/png",
        }, result

        cell_prefix = "cell before image\n"
        assert client.client.temporary_directory is not None
        workspace = Path(client.client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        relative_output = Path("capture/sessions/001/cell-001.txt")
        public_output = (
            f".agents/console/sessions/{session.name}/{relative_output.as_posix()}"
        )
        tail = result["content"][4]["text"]
        raw = cell_prefix + "x" * (PENDING_TEXT_BUDGET + 7)
        assert (session / relative_output).read_text(encoding="utf-8") == raw
        notices = (
            "[stopped by session restart request before evaluation finished]",
            "[worker stopped: in-memory state lost]",
            "[active evaluation stopped by session restart request]",
            "[starting new worker]",
            "[idle]",
        )
        suffix = "\n" + "\n".join(notices)
        assert tail.endswith(suffix), tail[-1_000:]
        for notice in notices:
            assert tail.count(notice) == 1, (notice, tail[-1_000:])
        omitted = assert_preview(cell_prefix + tail.removesuffix(suffix), raw)
        assert f"raw log: {public_output}" in tail
        assert (
            sum(len(block.get("text", "").encode()) for block in result["content"])
            <= 8192
        )
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        summary = [
            event
            for event in events
            if event["event"] == "cell_output" and event["call_id"] == 2
        ][-1]
        assert summary["retained_bytes"] == len(raw.encode())
        assert summary["inline_omitted_bytes"] == omitted
        assert summary["discarded_bytes"] == 0
        normalize_preview_paths(client.client)
        compact_previews(client.client, "x")

        client.send(session="default")
        assert _tool_text(client.client.transcript[-1]["result"]) == "\n[idle]"
        transcript = client.client.finish()
        finished = True
        return transcript
    finally:
        if not retirement_released:
            retirement_release.release()
        if not finished:
            stop_client(client.client)
        prelude_release.close()
        prelude_processed.close()
        output_ready.close()
        shutdown_received.close()
        retirement_release.close()
        client._temporary.cleanup()


if __name__ == "__main__":
    run_this_suite(__file__)
