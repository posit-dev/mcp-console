"""Immediate R errors preserve the foreground's legacy error buffer."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_immediate_error_preserves_foreground_error_buffer(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-error-ownership-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                # fmt: r
                r=code(r"""
                    release_foreground_error <- FALSE
                    options(error = function() {
                      cat("foreground error handler ready\n")
                      while (!release_foreground_error) {
                        Sys.sleep(0.01)
                      }
                      cat("foreground handler message: ", geterrmessage(), sep = "")
                    })
                    """),
            )
            with (
                ControlClient(endpoint) as evaluator,
                ControlClient(endpoint) as priority,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe")
                pending = evaluator.start(
                    op="evaluate",
                    language="r",
                    code='stop("foreground failure", call. = FALSE)',
                    busy="wait",
                    wait_ms=30000,
                )
                while True:
                    event = json.loads(observer.reader.readline(timeout=30))
                    assert event["event"] == "preview", event
                    if "foreground error handler ready\n" in result_text(event):
                        break
                private = priority.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    # fmt: r
                    code=code("""
                        (function() {
                          on.exit(release_foreground_error <<- TRUE)
                          stop("private failure", call. = FALSE)
                        })()
                        """),
                )
                assert result_text(private) == "Error: private failure\n", private
                assert private["result"]["execution_error"] == {
                    "ename": "RError",
                    "evalue": "Error: private failure\n",
                    "traceback": [],
                }, private
                foreground = evaluator.receive(pending)
                assert foreground["result"]["execution_error"] == {
                    "ename": "RError",
                    "evalue": "Error: foreground failure\n",
                    "traceback": [],
                }, foreground
                assert result_text(foreground) == (
                    "Error: foreground failure\n"
                    "foreground error handler ready\n"
                    "foreground handler message: Error: foreground failure\n"
                ), foreground
                # A native restart bypasses the helper's condition result. It
                # must not attribute the restored foreground buffer to itself.
                aborted = priority.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='invokeRestart("abort")',
                )
                assert result_text(aborted) == "", aborted
                assert aborted["result"]["execution_error"] == {
                    "ename": "RAbort",
                    "evalue": "R priority evaluation aborted",
                    "traceback": [],
                }, aborted
                recovered = evaluator.request(
                    op="evaluate",
                    language="r",
                    # fmt: r
                    code=code(r"""
                        options(error = NULL)
                        stopifnot(identical(geterrmessage(), "Error: foreground failure\n"))
                        cat("recovered\n")
                        """),
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(recovered) == "recovered\n", recovered
                assert recovered["result"]["execution_error"] is None, recovered
                client.expect("\n[idle]")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript,
                {
                    "evaluation.yaml": evaluator.transcript,
                    "priority.yaml": priority.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
