#!/usr/bin/env -S uv run --script

from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import signal
import socket
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

from anyio import CancelScope, get_cancelled_exc_class, to_thread
from mcp import Client, MCPError
from mcp.types import CallToolResult
from mcp_console import MCPConsole
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def client(root: Path, mode: str = "normal", *arguments: str) -> MCPConsole:
    return MCPConsole(
        command=sys.executable,
        args=["-u", ROOT / "tests/fixtures/python_sync_server.py", mode, *arguments],
        server_parameters={
            "cwd": root,
            "env": dict(os.environ, MCP_CONSOLE_HOME=str(root)),
        },
    )


def test_sync_connection_closes_resources(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        before = set(threading.enumerate())
        with client(root) as console:
            assert console.connect() is console
            assert console.send_tool.name == "send"
            assert console.send(python="hello") == "hello"
            assert console.send(control="interrupt") == "interrupted"
        assert (root / "closed").exists()
        # AnyIO's pool receives stop sentinels when the portal exits, but those
        # workers are not joined by the portal. Observe their termination before
        # asserting the same resource inventory, with one bounded wait budget.
        deadline = time.monotonic() + 5
        for thread in set(threading.enumerate()) - before:
            thread.join(max(0, deadline - time.monotonic()))
        assert set(threading.enumerate()) == before, threading.enumerate()
    return [{"output": "hello", "control": "interrupted", "closed": True}]


@requires(POSIX)
def test_host_interrupt_cancels_wait_and_discards_partial_reply(
    binary: Path,
) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with client(root) as console:
                try:
                    console.send(python="cancel wait")
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError("host interrupt did not reach the caller")
                recovered = console.send(python="recovered")
                assert recovered == "recovered", (
                    recovered,
                    (root / "requests.jsonl").read_text(encoding="utf-8"),
                )
                assert console.send(control="interrupt") == "interrupted"
            requests = [
                json.loads(line)
                for line in (root / "requests.jsonl")
                .read_text(encoding="utf-8")
                .splitlines()
            ]
            call, cancellation, recovery, interrupt = requests[3:]
            assert cancellation["method"] == "notifications/cancelled"
            assert cancellation["params"]["requestId"] == call["id"]
            assert recovery["params"]["arguments"] == {"python": "recovered"}
            assert interrupt["params"]["arguments"] == {"control": "interrupt"}
            assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    return [
        {
            "host_interrupt": "KeyboardInterrupt",
            "request_cancelled": True,
            "recovery": "recovered",
            "control": "interrupted",
        }
    ]


@requires(POSIX)
def test_stalled_cancellation_invalidates_connection(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with (
            tempfile.TemporaryDirectory() as directory,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            for escape in ("deadline", "interrupt"):
                root = Path(directory) / escape
                root.mkdir()
                cleanup_started = threading.Event()
                release_cleanup = threading.Event()
                caller_returned = threading.Event()
                call_tool = Client.call_tool

                async def stalled_cleanup(
                    self: Client, name: str, arguments: dict[str, object]
                ) -> CallToolResult:
                    try:
                        return await call_tool(self, name, arguments)
                    except get_cancelled_exc_class():
                        # Hold the external SDK's cleanup, rather than replacing
                        # Console's implementation or its completion signal.
                        with CancelScope(shield=True):
                            cleanup_started.set()
                            await to_thread.run_sync(release_cleanup.wait)
                        raise

                def release_if_caller_stalls() -> None:
                    if cleanup_started.wait(timeout=10):
                        if escape == "interrupt":
                            os.kill(os.getpid(), signal.SIGINT)
                        if caller_returned.wait(timeout=10):
                            return
                    # Bound the failing baseline without a timing assertion.
                    release_cleanup.set()

                guard = pool.submit(release_if_caller_stalls)
                with patch.object(Client, "call_tool", stalled_cleanup):
                    with client(root) as console:
                        try:
                            try:
                                console.send(python="cancel wait")
                            except KeyboardInterrupt:
                                caller_returned.set()
                            else:
                                raise AssertionError("host interrupt missing")
                            assert cleanup_started.is_set()
                            assert not release_cleanup.is_set(), (
                                "caller waited until stalled SDK cleanup was released"
                            )
                            for operation in (
                                lambda: console.send(python="recovered"),
                                console.connect,
                            ):
                                try:
                                    operation()
                                except RuntimeError as error:
                                    assert str(error) == (
                                        "Request cancellation cleanup did not finish; "
                                        "close and reconnect before reusing this connection"
                                    )
                                else:
                                    raise AssertionError(
                                        "invalidated connection reused"
                                    )
                        finally:
                            caller_returned.set()
                            release_cleanup.set()
                    guard.result(timeout=5)
                    assert (root / "closed").exists()
                    requests = (root / "requests.jsonl").read_text(encoding="utf-8")
                    assert '"python": "recovered"' not in requests
                    (root / "closed").unlink()
                    with console:
                        assert console.send(python="reconnected") == "reconnected"
                    assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    return [{"deadline": "invalidated", "interrupt": "invalidated", "reconnect": True}]


def test_sync_connection_can_interrupt_pending_call(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory, socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        listener.settimeout(10)
        with client(
            Path(directory), "normal", str(listener.getsockname()[1])
        ) as console:
            with ThreadPoolExecutor(max_workers=2) as pool:
                pending = pool.submit(console.send, python="hold")
                gate, _ = listener.accept()
                with gate:
                    gate.settimeout(10)
                    assert gate.recv(1) == b"1"
                    try:
                        interrupt = pool.submit(console.send, control="interrupt")
                        assert interrupt.result(timeout=5) == "interrupted"
                    finally:
                        gate.sendall(b"1")
                assert pending.result(timeout=5) == "released"
            assert console.send(python="recovered") == "recovered"
    return [
        {
            "control": "interrupted",
            "pending": "released",
            "recovery": "recovered",
        }
    ]


def test_sync_protocol_errors_and_large_messages(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        console = client(root)
        with console:
            source = "λ\n" * 100_000
            assert console.send(python=source) == source
            try:
                console.send(python="rpc error")
            except MCPError as error:
                assert str(error) == "fixture error"
            else:
                raise AssertionError("JSON-RPC error was accepted")
            assert console.send(python="recovered") == "recovered"
        console.close()
        assert (root / "closed").exists()
        with console:
            assert console.send(python="reconnected") == "reconnected"
    return [
        {
            "large_unicode_message": "preserved",
            "error": "fixture error",
            "recovery": "recovered",
            "reconnect": "reconnected",
        }
    ]


def test_failed_sync_connection_reaps_server(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        try:
            client(root, "empty-tools").connect()
        except* RuntimeError as errors:
            _, unexpected = errors.split(
                lambda error: str(error) == "MCP Console did not expose its send tool"
            )
            assert unexpected is None, errors
        else:
            raise AssertionError("connection without send tool succeeded")
        assert (root / "closed").exists()
    return [{"error": "MCP Console did not expose its send tool", "closed": True}]


def test_sync_negotiates_supported_protocol_versions(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with client(root, "2025-06-18") as console:
            assert console.send(python="negotiated") == "negotiated"
        (root / "closed").unlink()
        try:
            client(root, "unsupported").connect()
        except* RuntimeError as errors:
            _, unexpected = errors.split(
                lambda error: (
                    str(error)
                    == "Unsupported protocol version from the server: unsupported"
                )
            )
            assert unexpected is None, errors
        else:
            raise AssertionError("unsupported protocol was accepted")
        assert (root / "closed").exists()
    return [{"supported": "negotiated", "unsupported": "rejected", "closed": True}]


def test_sync_server_exit_closes_connection(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        console = client(Path(directory))
        with console:
            try:
                console.send(python="exit")
            except MCPError as error:
                assert str(error) == "Connection closed"
            else:
                raise AssertionError("server exit was accepted as a response")
            try:
                console.send()
            except MCPError as error:
                assert str(error) == "Connection closed"
            else:
                raise AssertionError("failed connection accepted a call")
        with console:
            assert console.send(python="reconnected") == "reconnected"
    return [
        {
            "error": "Connection closed",
            "reconnect": "reconnected",
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
