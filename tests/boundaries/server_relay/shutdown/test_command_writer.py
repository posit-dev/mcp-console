import os
import signal
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import capture_process_identity, host_process_id, signal_process
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires, POSIX
from support.suites import run_this_suite


def retirement_case(
    binary: Path, execution: Execution, mode: str, restarts: int
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        checkpoints = {
            (name, generation): stack.enter_context(
                closing(FifoCheckpoint.create(root / f"{name}-{generation}"))
            )
            for generation in range(1, restarts + 1)
            for name in ("partial", "held", "exit", "holder")
        }
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(root, "relay_writer_retirement")),
            "MCP_CONSOLE_TEST_WRITER_ROOT": str(root),
            "MCP_CONSOLE_TEST_WRITER_MODE": mode,
            "MCP_CONSOLE_TEST_WRITER_RESTARTS": str(restarts),
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/command_writer.py"
        )
        writable = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        client = McpClient(
            binary,
            execution.serve(*writable, "--worker", str(binary), "--relay", str(relay)),
            environment,
            response_timeout=15,
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            first = client.send(r="writer-probe")
            assert tool_text(first) == "[done]", first
            for generation in range(1, restarts + 1):
                if mode != "stdout":
                    client.send(stdin="x" * (8 * 1024 * 1024), timeout_ms=0)
                    checkpoints["partial", generation].wait(
                        "partial command frame received"
                    )
                    wait_for_path(
                        root / f"large-write-{generation}",
                        "writer entered the oversized transfer",
                        client=client,
                    )
                checkpoints["held", generation].wait(
                    "descendant retains exactly one stream"
                )
                pid = host_process_id(
                    int((root / f"holder-pid-{generation}").read_text()),
                    client.process.pid,
                )
                identity = capture_process_identity(pid)
                events = stack.enter_context(Events())
                events.watch_process(pid)
                identities.append((identity, events))
                checkpoints["exit", generation].release()
                # Replacement must wait for actual owned joins, even when the
                # old stdin or stdout endpoint remains open in the fixture.
                result = client.send(control="restart", r="writer-probe")
                assert not result.get("isError", False), result
                assert tool_text(result).endswith("[done]"), result
                assert (root / f"closed-{generation}").exists(), (
                    f"generation {generation} command descriptor was not closed before replacement: {list(root.iterdir())}"
                )
                assert (root / f"joined-{generation}").exists(), (
                    f"generation {generation} command writer was not joined before replacement"
                )
                checkpoints["holder", generation].release()
            client.finish()
            assert (root / f"closed-{restarts + 1}").exists()
            assert (root / f"joined-{restarts + 1}").exists()
        finally:
            for checkpoint in checkpoints.values():
                checkpoint.release()
            for identity, events in identities:
                signal_process(identity, signal.SIGKILL)
                assert identity[0] in events.wait(10), (
                    "fixture descriptor holder did not exit"
                )
            client.close()
        return [{"retained_stream": mode, "retired_writers": restarts + 1}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_restarts_join_writer_with_retained_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    return retirement_case(binary, execution, "stdin", 3)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_idle_writer_joins_with_retained_stdout(
    binary: Path, execution: Execution
) -> Transcript:
    return retirement_case(binary, execution, "stdout", 1)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_forced_retirement_aborts_full_command_pipe(
    binary: Path, execution: Execution
) -> Transcript:
    return retirement_case(binary, execution, "forced", 1)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_restart_accepts_aborted_frame_after_confirmed_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        read_blocked, read_release, kill_reached, kill_release = [
            stack.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "read-blocked",
                "read-release",
                "kill-reached-1",
                "kill-release-1",
            )
        ]
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "relay_writer_retirement")),
            "MCP_CONSOLE_TEST_WRITER_ROOT": str(root),
            "MCP_CONSOLE_TEST_WRITER_MODE": "aborted_frame",
            "MCP_CONSOLE_TEST_RELAY_BINARY": str(binary),
            "MCP_CONSOLE_TEST_RELAY_READ_DYLIB": str(
                build_interposer(root, "relay_stdout_read_interposer")
            ),
            "MCP_CONSOLE_TEST_RELAY_READ_MATCH": '{"kind":"stdin"',
        }
        writable = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        client = McpClient(
            binary,
            execution.serve(
                *writable,
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "server_relay/command_writer.py"),
            ),
            environment,
        )
        try:
            client.initialize_and_list_tools()
            assert (
                tool_text(client.send(r="echo writer-probe")) == "zod: writer-probe\n"
            )
            pid = host_process_id(
                int((root / "relay-pid-1").read_text()), client.process.pid
            )
            events = stack.enter_context(Events())
            events.watch_process(pid)
            client.send(stdin="x" * (8 * 1024 * 1024), timeout_ms=0)
            read_blocked.wait("real relay read the partial stdin frame header")
            wait_for_path(
                root / "partial-write-1", "actual partial command write", client=client
            )
            restart = client.start_send(control="restart", r="echo writer-probe")
            kill_reached.wait("operation retired and launcher signal selected")
            wait_for_path(
                root / "closed-1", "abort closed command stdin", client=client
            )
            # The real relay now observes partial-frame EOF, reaps its worker,
            # publishes Fatal and exits before the selected signal is delivered.
            read_release.release()
            assert pid in events.wait(10), "real relay did not finish retirement"
            kill_release.release()
            client.receive(restart)
            result = restart["result"]
            assert not result.get("isError", False), result
            assert tool_text(result).endswith("zod: writer-probe\n[done]"), result
            assert (root / "joined-1").exists(), "old command writer was not joined"
            assert (root / "generation").read_text() == "2"
            assert (
                tool_text(client.send(r="echo after restart")) == "zod: after restart\n"
            )
            client.finish()
        finally:
            read_release.release()
            kill_release.release()
            client.close()
        return [
            {
                "aborted_frame_restart": "confirmed retirement",
                "subsequent_send": "completed",
            }
        ]


def interrupt_shutdown_case(
    binary: Path, execution: Execution, *, late_ack: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as stack:
        root = Path(temporary)
        received, shutdown, ack, kill_reached, kill_release = [
            stack.enter_context(closing(FifoCheckpoint.create(root / f"{name}-1")))
            for name in ("interrupt", "shutdown", "ack", "kill-reached", "kill-release")
        ]
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/command_writer.py"
        )
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(root, "relay_writer_retirement")),
            "MCP_CONSOLE_TEST_WRITER_ROOT": str(root),
            "MCP_CONSOLE_TEST_WRITER_MODE": "interrupt_after_eof"
            if late_ack
            else "interrupt",
            "MCP_CONSOLE_TEST_WRITER_RESTARTS": "1",
        }
        writable = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        client = McpClient(
            binary,
            execution.serve(*writable, "--worker", str(binary), "--relay", str(relay)),
            environment,
        )
        try:
            client.initialize_and_list_tools()
            assert tool_text(client.send(r="writer-probe")) == "[done]"
            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            received.wait("relay received the generation-bound interrupt")
            if not late_ack:
                client.notify("notifications/cancelled", requestId=interrupt["id"])
                client.request("ping")
            client.stdin.close()
            shutdown.wait(
                "ordered shutdown reached the relay while its interrupt ack is withheld"
            )
            if late_ack:
                kill_reached.wait("forced retirement selected after command abort")
                wait_for_path(
                    root / "late-ack",
                    "relay emitted interrupt receipt after command EOF",
                    client=client,
                )
                ack.release()
                kill_release.release()
                client.receive(interrupt)
                assert interrupt["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "[worker stopped before operation completed]",
                        }
                    ],
                    "isError": True,
                }, interrupt
            # Retirement must wake the outstanding control owner without another
            # interrupt or a duplicate reply, even if its receipt arrives after EOF.
            client.finish()
            assert (root / "closed-1").exists()
            assert (root / "joined-1").exists()
            assert (root / "generation").read_text() == "1"
            assert (root / "late-ack").exists() == late_ack
        finally:
            ack.release()
            kill_release.release()
            client.close()
        return [
            {
                "interrupt_settled_by_shutdown": True,
                "observation_cancelled": not late_ack,
                "late_ack_after_command_eof": late_ack,
                "writer_joined": True,
            }
        ]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_shutdown_settles_cancelled_interrupt_without_ack(
    binary: Path, execution: Execution
) -> Transcript:
    return interrupt_shutdown_case(binary, execution, late_ack=False)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_shutdown_ignores_interrupt_ack_after_command_eof(
    binary: Path, execution: Execution
) -> Transcript:
    return interrupt_shutdown_case(binary, execution, late_ack=True)


if __name__ == "__main__":
    run_this_suite(__file__)
