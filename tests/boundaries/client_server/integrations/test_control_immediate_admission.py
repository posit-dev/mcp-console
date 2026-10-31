"""Immediate R admission preserves ordinary execution and interrupt ownership."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code, normalize_recording
from support.progress import RUNNING, without_elapsed
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_r_immediate_during_python_rejects_without_occupying_priority_slot(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                execution.serve(
                    "--control-socket", str(endpoint), "--writable-root", str(root)
                ),
                current_directory=root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(r="immediate_r_ran <- FALSE")
            client.expect(python="pass")
            # fmt: python
            foreground_source = code(f"""
                with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                    gate.write(b"1")
                with open({str(release.path)!r}, "rb", buffering=0) as gate:
                    gate.read(1)
                print("foreground complete")
                """)
            foreground = client.start_send(python=foreground_source, timeout_ms=60000)
            try:
                entered.wait("Python foreground owns the interpreter", client=client)
                with ControlClient(endpoint) as control:
                    r_source = 'immediate_r_ran <- TRUE; cat("R immediate\\n")'
                    rejected = control.request(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code=r_source,
                    )
                    assert "error" in rejected, rejected
                    assert rejected["error"]["code"] == "busy", rejected
                    # fmt: python
                    release_source = code(f"""
                        with open({str(release.path)!r}, "wb", buffering=0) as gate:
                            gate.write(b"1")
                        print("Python immediate released foreground")
                        """)
                    result = control.request(
                        op="evaluate",
                        language="python",
                        execution="immediate",
                        code=release_source,
                    )
                    assert (
                        result_text(result) == "Python immediate released foreground\n"
                    ), result
                    assert result["result"]["execution_error"] is None, result
                    client.receive(foreground)
                    assert result_text(foreground) == "foreground complete\n", (
                        foreground
                    )
                    client.expect("[1] FALSE\n", r="immediate_r_ran")
                    result = control.request(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code=r_source,
                    )
                    assert result_text(result) == "R immediate\n", result
                    assert result["result"]["execution_error"] is None, result
                    client.expect("[1] TRUE\n", r="immediate_r_ran")
                    transcript = normalize_recording(client.finish(), root)
            finally:
                release.release()
            return TranscriptWithCompanions(
                transcript,
                {"control.yaml": normalize_recording(control.transcript, root)},
            )


@requires(POSIX, R)
@executions(RUNTIME)
def test_idle_immediate_r_preserves_ordinary_admission_and_interrupts(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        release = root / "release"
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            closing(FifoCheckpoint.create(root / "caught")) as caught,
            McpClient(
                binary,
                execution.serve(
                    "--control-socket", str(endpoint), "--writable-root", str(root)
                ),
                current_directory=root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(
                # fmt: r
                r=code("""
                    priority_unwound <- FALSE
                    hold_immediate <- function(catch_interrupt = FALSE) {
                      on.exit(priority_unwound <<- TRUE)
                      wait_for_release <- function() {
                        gate <- fifo("entered", "wb", blocking = TRUE)
                        writeBin(charToRaw("1"), gate)
                        close(gate)
                        while (!file.exists("release")) {
                          Sys.sleep(0.01)
                        }
                      }
                      if (catch_interrupt) {
                        tryCatch(
                          wait_for_release(),
                          interrupt = function(condition) {
                            gate <- fifo("caught", "wb", blocking = TRUE)
                            writeBin(charToRaw("1"), gate)
                            close(gate)
                            while (!file.exists("release")) {
                              Sys.sleep(0.01)
                            }
                          }
                        )
                      } else {
                        wait_for_release()
                      }
                      cat("private released\\n")
                    }
                    """),
            )
            with (
                ControlClient(endpoint) as priority,
                ControlClient(endpoint) as control,
            ):
                try:
                    immediate = priority.start(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code="hold_immediate()",
                    )
                    entered.wait("idle immediate R is running", client=client)
                    # This public reply proves admission; the immediate FIFO
                    # alone says nothing about ordinary request processing.
                    admitted = client.send(
                        r='writeLines("ordinary ran", "interrupted-source")',
                        timeout_ms=0,
                    )
                    assert without_elapsed(tool_text(admitted)) == RUNNING, admitted
                    status = control.request(op="status")["result"]
                    evaluation_id = status["evaluation"]["id"]
                    assert status == {
                        "state": "evaluating",
                        "idle": False,
                        "evaluation": {
                            "id": evaluation_id,
                            "origin": "mcp",
                            "language": "r",
                            "started": False,
                        },
                    }, status
                    with ControlClient(endpoint) as observer:
                        snapshot = observer.request(op="output")["result"]
                    observed = next(
                        item
                        for item in snapshot["evaluations"]
                        if item["id"] == evaluation_id
                    )
                    assert (
                        observed["started"] is False and observed["state"] == "running"
                    ), observed
                    assert not any(
                        item["type"] == "evaluation_started"
                        and item["evaluation_id"] == evaluation_id
                        for item in snapshot["events"]
                    ), snapshot
                    interrupted = control.request(
                        op="interrupt", evaluation_id=evaluation_id
                    )
                    assert interrupted["result"] == {"accepted": True}, interrupted
                    private = priority.receive(immediate)
                    assert (
                        private["result"]["execution_error"]["ename"] == "Interrupt"
                    ), private
                    completed = client.expect(None)
                    assert not (root / "interrupted-source").exists(), (
                        "interrupted ordinary source executed"
                    )
                    assert tool_text(completed) == "[done]", completed
                    with ControlClient(endpoint) as observer:
                        snapshot = observer.request(op="output")["result"]
                    observed = next(
                        item
                        for item in snapshot["evaluations"]
                        if item["id"] == evaluation_id
                    )
                    assert (
                        observed["started"] is False and observed["state"] == "complete"
                    ), observed

                    # A different language is accepted while immediate R owns
                    # the interpreter, then runs after private completion.
                    client.expect(
                        r="stopifnot(priority_unwound); priority_unwound <- FALSE"
                    )
                    immediate = priority.start(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code="hold_immediate()",
                    )
                    entered.wait("second idle immediate R is running", client=client)
                    admitted = client.send(
                        # fmt: python
                        python=code("""
                            with open("ordinary-python-ran", "w") as marker:
                                marker.write("ordinary Python ran")
                            print("ordinary Python complete")
                            """),
                        timeout_ms=0,
                    )
                    assert without_elapsed(tool_text(admitted)) == RUNNING, admitted
                    status = control.request(op="status")["result"]
                    assert status["evaluation"]["language"] == "python", status
                    assert status["evaluation"]["started"] is False, status
                    assert not (root / "ordinary-python-ran").exists()
                    release.touch()
                    private = priority.receive(immediate)
                    assert result_text(private) == "private released\n", private
                    assert private["result"]["execution_error"] is None, private
                    client.expect("ordinary Python complete\n")
                    assert (root / "ordinary-python-ran").read_text() == (
                        "ordinary Python ran"
                    )
                    release.unlink()

                    # Even a private handler that consumes the signal must not
                    # keep the cancelled ordinary evaluation waiting behind it.
                    client.expect(
                        r="stopifnot(priority_unwound); priority_unwound <- FALSE"
                    )
                    immediate = priority.start(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code="hold_immediate(catch_interrupt = TRUE)",
                    )
                    entered.wait(
                        "interrupt-catching immediate R is running", client=client
                    )
                    admitted = client.send(
                        r='writeLines("ordinary ran", "caught-interrupt-source")',
                        timeout_ms=0,
                    )
                    assert without_elapsed(tool_text(admitted)) == RUNNING, admitted
                    status = control.request(op="status")["result"]
                    assert status["evaluation"]["started"] is False, status
                    interrupted = control.request(
                        op="interrupt", evaluation_id=status["evaluation"]["id"]
                    )
                    assert interrupted["result"] == {"accepted": True}, interrupted
                    caught.wait("private R caught its interrupt", client=client)
                    client.expect()
                    assert not (root / "caught-interrupt-source").exists()
                    assert not release.exists()
                    release.touch()
                    private = priority.receive(immediate)
                    assert result_text(private) == "private released\n", private
                    assert private["result"]["execution_error"] is None, private
                    release.unlink()
                    client.expect(r="stopifnot(priority_unwound)")

                    # Retirement also releases a dispatch wait, even when the
                    # immediate request handles interrupts without finishing.
                    immediate = priority.start(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        code="hold_immediate(catch_interrupt = TRUE)",
                    )
                    entered.wait("immediate R is running before restart", client=client)
                    admitted = client.send(
                        r='writeLines("ordinary ran", "restarted-source")',
                        timeout_ms=0,
                    )
                    assert without_elapsed(tool_text(admitted)) == RUNNING, admitted
                    status = control.request(op="status")["result"]
                    assert status["evaluation"]["started"] is False, status
                    restarted = client.send(control="restart")
                    assert not restarted.get("isError"), restarted
                    retired = priority.receive(immediate)
                    assert retired["error"]["code"] == "execution_failed", retired
                    assert not (root / "restarted-source").exists()
                    client.expect(
                        "worker usable\n",
                        # fmt: r
                        r=code("""
                            stopifnot(!exists("hold_immediate", inherits = FALSE))
                            cat("worker usable\\n")
                            """),
                    )
                    transcript = normalize_recording(client.finish(), root)
                finally:
                    release.touch()
            return TranscriptWithCompanions(
                transcript,
                {
                    "priority.yaml": normalize_recording(priority.transcript, root),
                    "control.yaml": control.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
