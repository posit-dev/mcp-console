"""Opt-in control execution can service a busy interpreter without claiming MCP output."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from boundaries.client_server.python.test_without_r import selected_environment
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_python_thread_releases_busy_foreground(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            McpClient(
                binary, execution.serve(*arguments), selected_environment(root), root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="import threading; release = threading.Event()")
            # fmt: python
            source = code(f"""
                print("foreground pending", flush=True)
                with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                    gate.write(b"1")
                assert release.wait(30), "priority request did not release the foreground"
                print("foreground complete")
                """)
            pending = client.start_send(python=source, timeout_ms=60000)
            entered.wait(client=client)
            with ControlClient(endpoint) as control:
                failed = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="raise ValueError('priority failure')",
                )
                assert failed["result"]["execution_error"]["ename"] == "ValueError", (
                    failed
                )
                assert "priority failure" in result_text(failed), failed
                result = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="assert threading.current_thread() is not threading.main_thread(); print('priority output'); release.set()",
                )
                assert result_text(result) == "priority output\n", result
                client.receive(pending)
                assert (
                    result_text(pending) == "foreground pending\nforeground complete\n"
                ), pending
                client.expect("True\n", python="print(release.is_set())")
                idle = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="priority_value = 42; print(priority_value)",
                )
                assert result_text(idle) == "42\n", idle
                client.expect("42\n", python="print(priority_value)")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_r_reentrant_releases_busy_foreground(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            McpClient(
                binary, execution.serve(*arguments), current_directory=root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(r="release <- FALSE")
            # fmt: r
            source = code("""
                cat("foreground pending\\n")
                gate <- fifo(
                  "ENTERED",
                  "wb",
                  blocking = TRUE
                )
                writeBin(charToRaw("1"), gate)
                close(gate)
                while (!release) {
                  Sys.sleep(0.01)
                }
                cat("foreground complete\\n")
                """).replace("ENTERED", str(entered.path))
            pending = client.start_send(r=source, timeout_ms=60000)
            entered.wait(client=client)
            with ControlClient(endpoint) as control:
                failed = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='stop("priority failure")',
                )
                assert failed["result"]["execution_error"]["ename"] == "RError", failed
                assert (
                    "priority failure" in failed["result"]["execution_error"]["evalue"]
                ), failed
                assert "priority failure" in result_text(failed), failed
                result = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='release <- TRUE; cat("priority output\\n")',
                )
                assert result_text(result) == "priority output\n", result
                client.receive(pending)
                assert (
                    result_text(pending) == "foreground pending\nforeground complete\n"
                ), pending
                client.expect("[1] TRUE\n", r="release")
                idle = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code="priority_value <- 42L; priority_value",
                )
                assert result_text(idle) == "[1] 42\n", idle
                client.expect("[1] 42\n", r="priority_value")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_retires_priority_thread_without_recovering_its_output(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            McpClient(
                binary, execution.serve(*arguments), selected_environment(root), root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="import threading")
            with (
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                unsupported = control.request(
                    op="evaluate",
                    language="sql",
                    execution="immediate",
                    code="SELECT 1",
                )
                assert unsupported["error"]["code"] == "execution_failed", unsupported
                # fmt: python
                source = code(f"""
                    print("private priority pending", flush=True)
                    with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                        gate.write(b"1")
                    threading.Event().wait()
                    """)
                pending = control.start(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code=source,
                )
                entered.wait(client=client)
                busy = observer.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="print('must not run')",
                )
                assert busy["error"]["code"] == "busy", busy
                restarted = client.send(
                    control="restart", python="print('replacement ready')"
                )
                assert not restarted.get("isError"), restarted
                assert "private priority" not in str(restarted), restarted
                retired = control.receive(pending)
                assert retired["error"]["code"] == "execution_failed", retired
                replacement = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="print('new priority')",
                )
                assert result_text(replacement) == "new priority\n", replacement
                client.expect("\n[idle]")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript,
                {
                    "control.yaml": normalize_recording(control.transcript, root),
                    "observer.yaml": observer.transcript,
                },
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_python_thread_system_exit_is_an_execution_error(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            selected_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="shared = 42")
            with ControlClient(endpoint) as control:
                failed = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="raise SystemExit('priority exit')",
                )
                assert "result" in failed, failed
                assert failed["result"]["execution_error"]["ename"] == "SystemExit", (
                    failed
                )
                assert "priority exit" in result_text(failed), failed
                survived = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="print(shared)",
                )
                assert result_text(survived) == "42\n", survived
                client.expect("42\n", python="print(shared)")
                transcript = client.finish()
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


if __name__ == "__main__":
    run_this_suite(__file__)
