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


@requires(POSIX, R)
@executions(RUNTIME)
def test_nul_source_preserves_r_error_handler_and_buffer(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-nul-source-"
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
                "Error: foreground failure\n",
                r='stop("foreground failure", call. = FALSE)',
            )
            client.expect(
                # fmt: r
                r=code("""
                    nul_handler_runs <- 0L
                    nul_source_ran <- FALSE
                    nul_error_handler <- function() {
                      nul_handler_runs <<- nul_handler_runs + 1L
                    }
                    options(error = nul_error_handler)
                    nul_error_option <- getOption("error")
                    nul_previous_error <- geterrmessage()
                    """),
            )
            with ControlClient(endpoint) as control:
                for mode in ("serial", "immediate"):
                    rejected = control.request(
                        op="evaluate",
                        language="r",
                        execution=mode,
                        code="nul_source_ran <- TRUE\x00",
                        **(
                            {"busy": "wait", "wait_ms": 30000}
                            if mode == "serial"
                            else {}
                        ),
                    )
                    inspected = control.request(
                        op="evaluate",
                        language="r",
                        execution="immediate",
                        # fmt: r
                        code=code(r"""
                            stopifnot(identical(getOption("error"), nul_error_option))
                            cat(
                              nul_handler_runs,
                              nul_source_ran,
                              identical(geterrmessage(), nul_previous_error),
                              sep = "\n"
                            )
                            """),
                    )
                    assert result_text(inspected) == "0\nFALSE\nTRUE\n", inspected
                    assert (
                        result_text(rejected) == "Error: R source cannot contain NUL\n"
                    ), rejected
                    assert rejected["result"]["execution_error"] == {
                        "ename": "SyntaxError",
                        "evalue": "Error: R source cannot contain NUL\n",
                        "traceback": [],
                    }, rejected
                client.expect("\n[idle]")
                client.expect(
                    "usable\n",
                    r='options(error = NULL); cat("usable\\n")',
                )
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


if __name__ == "__main__":
    run_this_suite(__file__)
