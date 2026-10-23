#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CAPTURE_NAME,
    RETIREMENT_RELEASE_NAME,
    SHUTDOWN_RECEIVED_NAME,
    ServerRelayClient,
    _normalize_shutdown_grace,
)
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, R, requires
from support.resolvers import fake_ir_environment


@requires(POSIX, NATIVE_FIXTURES, R)
@executions(DIRECT, SANDBOXED)
def test_restart_and_eof_share_shutdown(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "library"
        library.mkdir()
        started = FifoCheckpoint.create(root / "started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        closed = FifoCheckpoint.create(root / "closed")
        environment = fake_ir_environment(root, [library])
        environment.update(
            {
                LOADER_VARIABLE: str(build_interposer(root, "retirement_overlap")),
                "MCP_CONSOLE_TEST_RETIREMENT_CLOSE": str(closed.path),
                "MCP_CONSOLE_TEST_IR_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(resolver_release.path),
            }
        )
        client = ServerRelayClient(
            binary, "blocked_live_r_resolver_shutdown", environment, execution=execution
        )
        client.start_worker()
        relay_root = client.relay_root()
        shutdown = FifoCheckpoint.attach(relay_root / SHUTDOWN_RECEIVED_NAME)
        release = FifoCheckpoint.attach(relay_root / RETIREMENT_RELEASE_NAME)
        capture = (relay_root / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            preparation = client.client.start_send(
                requirements={"r": ["blockedretirement"]}
            )
            started.wait("preparation admitted before restart")
            restart = client.client.start_send(control="restart")
            shutdown.wait("restart queued its only Shutdown")
            client.client.stdin.close()
            closed.wait("EOF closed preparation during restart retirement")
            release.release()
            client.client.receive_many([preparation, restart])
            exit_code = client.client.process.wait(timeout=10)
            stderr = client.client.stderr.read()
            assert exit_code == 0, (exit_code, stderr)
            assert stderr == "", stderr
            transcript = client._read_open_capture(capture)
            assert len(_normalize_shutdown_grace(transcript)) == 1, transcript
            finished = True
            return transcript
        finally:
            resolver_release.release()
            if not finished:
                stop_client(client.client)
            capture.close()
            shutdown.close()
            release.close()
            closed.close()
            started.close()
            resolver_release.close()
            client._temporary.cleanup()


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_restart_retires_callback_after_command_closure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "library"
        library.mkdir()
        environment = fake_ir_environment(root, [library])
        started = FifoCheckpoint.create(root / "resolver-started")
        release = FifoCheckpoint.create(root / "resolver-release")
        environment.update(
            {
                "MCP_CONSOLE_TEST_IR_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(release.path),
            }
        )
        client = ServerRelayClient(
            binary, "failed_during_resolver_callback", environment, execution=execution
        )
        finished = False
        client.client.response_timeout = 10
        try:
            client.start_worker()
            client.client.send(r="42", timeout_ms=0)
            started.wait("worker callback admitted its resolver")
            cell = client.client.start_send(control="restart", timeout_ms=30_000)
            client.client.receive(cell)
            result = cell["result"]
            assert result["isError"] is False, result
            text = result["content"][0]["text"]
            assert "[starting new worker]" in text, text
            assert text.count("[worker stopped: in-memory state lost]") == 1, text
            transcript = client.client.finish()
            finished = True
            return transcript
        finally:
            release.release()
            if not finished:
                stop_client(client.client)
            started.close()
            release.close()
            client._temporary.cleanup()
