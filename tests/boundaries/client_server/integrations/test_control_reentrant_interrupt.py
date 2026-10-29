"""Reentrant R evaluation cannot consume the foreground's interrupt."""

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_reentrant_evaluation_preserves_foreground_interrupt(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-interrupt-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with (
            closing(FifoCheckpoint.create(root / "priority")) as priority_ready,
            McpClient(
                binary,
                execution.serve(
                    "--control-socket", str(endpoint), "--writable-root", str(root)
                ),
                current_directory=root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(r="release <- FALSE; priority_unwound <- FALSE")
            with (
                ControlClient(endpoint) as priority,
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe")
                # fmt: r
                foreground_source = code("""
                    tryCatch(
                      {
                        cat("foreground ready\\n")
                        while (!release) {
                          Sys.sleep(0.01)
                        }
                        Sys.sleep(0)
                        cat("foreground completed without interruption\\n")
                      },
                      interrupt = function(condition) cat("foreground interrupted\\n")
                    )
                    """)
                foreground = client.start_send(r=foreground_source, timeout_ms=60000)
                # The controller processes CellStarted before managed output;
                # an external FIFO alone cannot prove controller readiness.
                while True:
                    event = json.loads(observer.reader.readline(timeout=30))
                    assert event["event"] == "preview", event
                    if "foreground ready\n" in result_text(event):
                        break
                status = control.request(op="status")["result"]
                assert status["state"] == "evaluating", status
                assert status["evaluation"]["started"] is True, status
                evaluation_id = status["evaluation"]["id"]
                # Cleanup releases the foreground even on the buggy path,
                # making a consumed interrupt fail without a timeout.
                # fmt: r
                priority_source = code("""
                    (function() {
                      on.exit({
                        priority_unwound <<- TRUE
                        release <<- TRUE
                      })
                      gate <- fifo("PRIORITY", "wb", blocking = TRUE)
                      writeBin(charToRaw("1"), gate)
                      close(gate)
                      repeat {
                        Sys.sleep(0.01)
                      }
                    })()
                    """).replace("PRIORITY", str(priority_ready.path))
                pending = priority.start(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code=priority_source,
                )
                priority_ready.wait(client=client)
                interrupted = control.request(
                    op="interrupt", evaluation_id=evaluation_id
                )
                assert interrupted["result"] == {"accepted": True}, interrupted
                private = priority.receive(pending)
                client.receive(foreground)
                assert result_text(foreground) == (
                    "foreground ready\nforeground interrupted\n"
                ), foreground
                assert private["result"]["execution_error"]["ename"] == "Interrupt", (
                    private
                )
                client.expect(
                    "next cell\n", r='stopifnot(priority_unwound); cat("next cell\\n")'
                )

                # With no ordinary cell suspended, the private interrupt must
                # finish here instead of being replayed into the next cell.
                client.expect(r="priority_unwound <- FALSE")
                pending = priority.start(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code=priority_source,
                )
                priority_ready.wait(client=client)
                status = control.request(op="status")["result"]
                assert status["state"] == "idle" and status["evaluation"] is None, (
                    status
                )
                interrupted = client.send(control="interrupt")
                assert not interrupted.get("isError"), interrupted
                private = priority.receive(pending)
                assert private["result"]["execution_error"]["ename"] == "Interrupt", (
                    private
                )
                client.expect(
                    "after idle interrupt\n",
                    r='stopifnot(priority_unwound); cat("after idle interrupt\\n")',
                )
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript,
                {
                    "priority.yaml": normalize_recording(priority.transcript, root),
                    "control.yaml": control.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
