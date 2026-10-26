#!/usr/bin/env -S uv run --script

from concurrent.futures import ThreadPoolExecutor
import asyncio
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
        with (
            tempfile.TemporaryDirectory() as directory,
            socket.socket() as listener,
            ThreadPoolExecutor(max_workers=1) as pool,
        ):
            root = Path(directory)
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(10)
            caller_returned = threading.Event()
            rescued = threading.Event()
            caller_thread = threading.get_ident()
            sdk_thread: int | None = None
            call_tool = Client.call_tool

            async def observe_thread(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                nonlocal sdk_thread
                sdk_thread = threading.get_ident()
                return await call_tool(self, name, arguments)

            def interrupt_after_partial_reply() -> None:
                try:
                    with listener.accept()[0] as gate:
                        gate.settimeout(10)
                        assert gate.recv(1) == b"1"
                        assert sdk_thread is not None and sdk_thread != caller_thread
                        # Process-directed signals may reach any thread. Force that
                        # path while the main caller is waiting for this response.
                        signal.pthread_kill(sdk_thread, signal.SIGINT)
                        gate.sendall(b"1")
                    caller_returned.wait(timeout=10)
                finally:
                    if not caller_returned.is_set():
                        rescued.set()
                        signal.pthread_kill(caller_thread, signal.SIGINT)

            with (
                patch.object(Client, "call_tool", observe_thread),
                client(
                    root, "external-interrupt", str(listener.getsockname()[1])
                ) as console,
            ):
                guard = pool.submit(interrupt_after_partial_reply)
                try:
                    console.send(python="cancel wait")
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError("host interrupt did not reach the caller")
                finally:
                    caller_returned.set()
                guard.result(timeout=5)
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
            assert not rescued.is_set(), (
                "host interrupt needed a second signal to wake the caller"
            )
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
    before = set(threading.enumerate())
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
                context_closed = threading.Event()
                cleanup_finished = threading.Event()
                loop: asyncio.AbstractEventLoop | None = None
                cleanup_gate = asyncio.Event()
                call_tool = Client.call_tool

                async def stalled_cleanup(
                    self: Client, name: str, arguments: dict[str, object]
                ) -> CallToolResult:
                    nonlocal loop
                    try:
                        return await call_tool(self, name, arguments)
                    except get_cancelled_exc_class():
                        # Hold the external SDK's cleanup, rather than replacing
                        # Console's implementation or its completion signal.
                        try:
                            with CancelScope(shield=True):
                                loop = asyncio.get_running_loop()
                                cleanup_started.set()
                                await cleanup_gate.wait()
                        finally:
                            cleanup_finished.set()
                        raise

                def release_stalled_cleanup() -> None:
                    release_cleanup.set()
                    assert loop is not None
                    loop.call_soon_threadsafe(cleanup_gate.set)

                def release_if_caller_stalls() -> None:
                    if cleanup_started.wait(timeout=10):
                        if escape == "interrupt":
                            os.kill(os.getpid(), signal.SIGINT)
                        if caller_returned.wait(timeout=10) and context_closed.wait(
                            timeout=10
                        ):
                            return
                    # Bound the failing baseline without a timing assertion.
                    release_stalled_cleanup()

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
                    context_closed.set()
                    guard.result(timeout=5)
                    assert not release_cleanup.is_set(), (
                        "close waited until stalled SDK cleanup was released"
                    )
                    assert cleanup_finished.is_set()
                    assert (root / "closed").exists()
                    requests = (root / "requests.jsonl").read_text(encoding="utf-8")
                    assert '"python": "recovered"' not in requests
                    (root / "closed").unlink()
                    with console:
                        assert console.send(python="reconnected") == "reconnected"
                    assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    deadline = time.monotonic() + 5
    for thread in set(threading.enumerate()) - before:
        thread.join(max(0, deadline - time.monotonic()))
    assert set(threading.enumerate()) == before, threading.enumerate()
    return [{"deadline": "invalidated", "interrupt": "invalidated", "reconnect": True}]


@requires(POSIX)
def test_concurrent_send_rejected_during_cancellation(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cleanup_started = threading.Event()
            release_cleanup = threading.Event()
            call_tool = Client.call_tool

            async def stalled_cleanup(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                try:
                    return await call_tool(self, name, arguments)
                except get_cancelled_exc_class():
                    with CancelScope(shield=True):
                        cleanup_started.set()
                        await to_thread.run_sync(release_cleanup.wait)
                    raise

            with (
                patch.object(Client, "call_tool", stalled_cleanup),
                ThreadPoolExecutor(max_workers=1) as pool,
                client(root) as console,
            ):

                def try_concurrent_send() -> None:
                    try:
                        assert cleanup_started.wait(timeout=10)
                        try:
                            console.send(python="overtaking cancellation")
                        except RuntimeError as error:
                            assert str(error) == (
                                "Request cancellation cleanup is in progress; "
                                "wait before reusing this connection"
                            )
                        else:
                            raise AssertionError("send overtook cancellation cleanup")
                    finally:
                        release_cleanup.set()

                concurrent = pool.submit(try_concurrent_send)
                try:
                    console.send(python="cancel wait")
                except KeyboardInterrupt:
                    pass
                else:
                    raise AssertionError("host interrupt missing")
                concurrent.result(timeout=5)
                assert console.send(python="recovered") == "recovered"
            assert (root / "closed").exists()
            requests = (root / "requests.jsonl").read_text(encoding="utf-8")
            assert "overtaking cancellation" not in requests
    finally:
        signal.signal(signal.SIGINT, previous)
    return [{"concurrent_send": "rejected", "recovery": "recovered", "closed": True}]


@requires(POSIX)
def test_concurrent_send_cannot_overtake_host_interrupt(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            interrupted = threading.Event()
            admission_attempted = threading.Event()
            release_cleanup = threading.Event()
            sending_thread: int | None = None
            wait = threading.Condition.wait
            enter = threading.Condition.__enter__
            call_tool = Client.call_tool

            async def stalled_cleanup(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                result = await call_tool(self, name, arguments)
                if arguments.get("python") != "interrupted request":
                    return result
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                    await asyncio.Event().wait()
                finally:
                    with CancelScope(shield=True):
                        await to_thread.run_sync(release_cleanup.wait)
                raise AssertionError("request was not cancelled")

            def pause_interrupted_wait(
                self: threading.Condition, timeout: float | None = None
            ) -> bool:
                try:
                    return wait(self, timeout)
                except KeyboardInterrupt:
                    # Condition.wait has reacquired its lock. Give a concurrent
                    # caller admission before propagating the host interrupt.
                    interrupted.set()
                    assert admission_attempted.wait(timeout=10)
                    raise

            def observe_admission(self: threading.Condition) -> bool:
                nonlocal sending_thread
                if threading.get_ident() == sending_thread:
                    sending_thread = None
                    # With shared admission/response synchronization this is
                    # before admission; separate locks reach the response wait
                    # only after the overtaking request has already been queued.
                    admission_attempted.set()
                return enter(self)

            with ThreadPoolExecutor(max_workers=1) as pool, client(root) as console:

                def try_concurrent_send() -> str:
                    nonlocal sending_thread
                    assert interrupted.wait(timeout=10)
                    sending_thread = threading.get_ident()
                    try:
                        try:
                            console.send(python="overtaking host interrupt")
                        except RuntimeError as error:
                            return str(error)
                        return "send overtook host interrupt"
                    finally:
                        release_cleanup.set()

                with (
                    patch.object(threading.Condition, "wait", pause_interrupted_wait),
                    patch.object(threading.Condition, "__enter__", observe_admission),
                    patch.object(Client, "call_tool", stalled_cleanup),
                ):
                    concurrent = pool.submit(try_concurrent_send)
                    try:
                        try:
                            console.send(python="interrupted request")
                        except KeyboardInterrupt:
                            pass
                        else:
                            raise AssertionError("host interrupt missing")
                        assert concurrent.result(timeout=5) == (
                            "Request cancellation cleanup is in progress; "
                            "wait before reusing this connection"
                        )
                    finally:
                        release_cleanup.set()
                assert console.send(python="recovered") == "recovered"
            assert (root / "closed").exists()
            requests = (root / "requests.jsonl").read_text(encoding="utf-8")
            assert "overtaking host interrupt" not in requests, requests
    finally:
        signal.signal(signal.SIGINT, previous)
    return [{"concurrent_send": "rejected", "recovery": "recovered", "closed": True}]


@requires(POSIX)
def test_host_interrupt_while_sdk_loop_is_busy(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validating = threading.Event()
            release_validation = threading.Event()
            caller_returned = threading.Event()
            call_tool = Client.call_tool

            async def busy_validation(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                result = await call_tool(self, name, arguments)
                validating.set()
                release_validation.wait()
                return result

            def interrupt_validation() -> None:
                try:
                    assert validating.wait(timeout=10)
                    os.kill(os.getpid(), signal.SIGINT)
                    if caller_returned.wait(timeout=10):
                        return
                finally:
                    if not caller_returned.is_set():
                        release_validation.set()

            with (
                patch.object(Client, "call_tool", busy_validation),
                ThreadPoolExecutor(max_workers=1) as pool,
                client(root) as console,
            ):
                guard = pool.submit(interrupt_validation)
                try:
                    try:
                        console.send(python="busy validation")
                    except KeyboardInterrupt:
                        caller_returned.set()
                    else:
                        raise AssertionError("host interrupt missing")
                    assert not release_validation.is_set(), (
                        "cancellation submission waited for the busy SDK loop"
                    )
                finally:
                    caller_returned.set()
                    release_validation.set()
                guard.result(timeout=5)
            assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    return [{"host_interrupt": "KeyboardInterrupt", "loop_busy": True, "closed": True}]


@requires(POSIX)
def test_busy_loop_cancellation_retires_shielded_cleanup(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release_validation = threading.Event()
            caller_returned = threading.Event()
            cleanup_finished = threading.Event()
            cleanup_released = threading.Event()
            cleanup_gate = asyncio.Event()
            loop: asyncio.AbstractEventLoop | None = None
            call_tool = Client.call_tool

            async def busy_validation_and_cleanup(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                nonlocal loop
                result = await call_tool(self, name, arguments)
                if arguments.get("python") != "busy cleanup":
                    return result
                loop = asyncio.get_running_loop()
                try:
                    os.kill(os.getpid(), signal.SIGINT)
                    release_validation.wait()
                    await asyncio.Event().wait()
                except get_cancelled_exc_class():
                    try:
                        with CancelScope(shield=True):
                            await cleanup_gate.wait()
                    finally:
                        cleanup_finished.set()
                    raise

            def release_busy_loop() -> None:
                try:
                    assert caller_returned.wait(timeout=10)
                finally:
                    release_validation.set()

            with (
                patch.object(Client, "call_tool", busy_validation_and_cleanup),
                ThreadPoolExecutor(max_workers=1) as pool,
            ):
                console = client(root).connect()
                guard = pool.submit(release_busy_loop)
                closed = False
                try:
                    try:
                        console.send(python="busy cleanup")
                    except KeyboardInterrupt:
                        caller_returned.set()
                    else:
                        raise AssertionError("host interrupt missing")
                    console.close()
                    closed = True
                finally:
                    caller_returned.set()
                    release_validation.set()
                    if not closed:
                        # Clean up a failing baseline without allowing that
                        # release to serve as successful retirement evidence.
                        cleanup_released.set()
                        assert loop is not None
                        loop.call_soon_threadsafe(cleanup_gate.set)
                        console.close()
                guard.result(timeout=5)
                assert cleanup_finished.is_set()
                assert not cleanup_released.is_set()
                assert (root / "closed").exists()
                (root / "closed").unlink()
                with console:
                    assert console.send(python="reconnected") == "reconnected"
                assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    return [
        {"loop_busy": True, "cleanup_finished": True, "closed": True, "reconnect": True}
    ]


@requires(POSIX)
def test_host_interrupt_before_request_starts(binary: Path) -> Transcript:
    previous = signal.signal(signal.SIGINT, signal.default_int_handler)
    try:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            validating = threading.Event()
            release_validation = threading.Event()
            interrupted = threading.Event()
            call_tool = Client.call_tool
            post = asyncio.BaseEventLoop.call_soon_threadsafe

            async def busy_validation(
                self: Client, name: str, arguments: dict[str, object]
            ) -> CallToolResult:
                result = await call_tool(self, name, arguments)
                if arguments.get("python") == "occupy loop":
                    validating.set()
                    release_validation.wait()
                return result

            def interrupt_submission(
                self: asyncio.BaseEventLoop, callback, *args, **kwargs
            ):
                handle = post(self, callback, *args, **kwargs)
                if (
                    threading.current_thread() is threading.main_thread()
                    and not interrupted.is_set()
                ):
                    interrupted.set()
                    os.kill(os.getpid(), signal.SIGINT)
                return handle

            with (
                patch.object(Client, "call_tool", busy_validation),
                ThreadPoolExecutor(max_workers=1) as pool,
                client(root) as console,
            ):
                pending = pool.submit(console.send, python="occupy loop")
                assert validating.wait(timeout=10)
                try:
                    with patch.object(
                        asyncio.BaseEventLoop,
                        "call_soon_threadsafe",
                        interrupt_submission,
                    ):
                        try:
                            console.send(python="never started")
                        except KeyboardInterrupt:
                            pass
                        else:
                            raise AssertionError("host interrupt missing")
                    assert interrupted.is_set()
                finally:
                    release_validation.set()
                assert pending.result(timeout=5) == "occupy loop"
                assert console.send(python="recovered") == "recovered"
            requests = (root / "requests.jsonl").read_text(encoding="utf-8")
            assert "never started" not in requests, requests
            assert (root / "closed").exists()
            (root / "closed").unlink()
            with console:
                assert console.send(python="reconnected") == "reconnected"
            assert (root / "closed").exists()
    finally:
        signal.signal(signal.SIGINT, previous)
    return [{"unsent_request": "cancelled", "closed": True, "reconnect": True}]


def test_sync_shutdown_error_disconnects_client(binary: Path) -> Transcript:
    aexit = Client.__aexit__

    async def close_then_fail(self: Client, *args: object) -> None:
        await aexit(self, *args)
        raise RuntimeError("fixture shutdown error")

    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        console = client(root)
        with patch.object(Client, "__aexit__", close_then_fail):
            console.connect()
            assert console.send(python="hello") == "hello"
            try:
                console.close()
            except RuntimeError as error:
                assert str(error) == "fixture shutdown error"
            else:
                raise AssertionError("shutdown error missing")
        assert (root / "closed").exists()
        (root / "closed").unlink()
        with console:
            assert console.send(python="reconnected") == "reconnected"
        assert (root / "closed").exists()
    return [{"error": "fixture shutdown error", "closed": True, "reconnect": True}]


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
