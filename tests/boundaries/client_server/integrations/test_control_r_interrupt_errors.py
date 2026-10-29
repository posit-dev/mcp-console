"""Ordinary R interrupts report their own condition, not a prior error."""

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
def test_r_interrupt_metadata_does_not_reuse_prior_error(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-interrupt-"
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
                r=code("""
                    if (sys.nframe() != 0L) {
                      stop("not top level")
                    }
                    initial_handlers <- globalCallingHandlers()
                    options(interrupt = function() cat("interrupt option\\n"))
                    """),
            )
            with (
                ControlClient(endpoint) as evaluator,
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe")
                # fmt: r
                sleeping = code("""
                    cat("signal checkpoint\\n")
                    repeat {
                      Sys.sleep(0.1)
                    }
                    """)
                for source, checkpoint, error_name in (
                    ('readline("uncaught input: ")', "uncaught input: ", "Interrupt"),
                    (sleeping, "signal checkpoint\n", "Interrupt"),
                    (
                        # fmt: r
                        code("""
                            tryCatch(
                              readline("caught input: "),
                              interrupt = function(condition) cat("caught interrupt\\n")
                            )
                            """),
                        "caught input: ",
                        None,
                    ),
                    (
                        # fmt: r
                        code("""
                            tryCatch(
                              readline("converted input: "),
                              interrupt = function(condition) stop("current handler failure")
                            )
                            """),
                        "converted input: ",
                        "RError",
                    ),
                    (
                        # fmt: r
                        code("""
                            {
                              globalCallingHandlers(NULL)
                              readline("unobserved input: ")
                            }
                            """),
                        "unobserved input: ",
                        "RAbort",
                    ),
                ):
                    failed = evaluator.request(
                        op="evaluate",
                        language="r",
                        code='stop("previous R failure")',
                        busy="wait",
                        wait_ms=30000,
                    )
                    assert failed["result"]["execution_error"]["ename"] == "RError", (
                        failed
                    )
                    assert (
                        "previous R failure"
                        in failed["result"]["execution_error"]["evalue"]
                    ), failed
                    pending = evaluator.start(
                        op="evaluate",
                        language="r",
                        code=source,
                        busy="wait",
                        wait_ms=30000,
                    )
                    while True:
                        event = json.loads(observer.reader.readline(timeout=30))
                        assert event["event"] == "preview", event
                        if checkpoint in result_text(event):
                            break
                    status = control.request(op="status")["result"]
                    assert status["evaluation"]["origin"] == "integrator", status
                    accepted = control.request(
                        op="interrupt", evaluation_id=status["evaluation"]["id"]
                    )
                    assert accepted["result"] == {"accepted": True}, accepted
                    result = evaluator.receive(pending)
                    assert "previous R failure" not in result_text(result), result
                    if error_name is None:
                        assert "caught interrupt\n" in result_text(result), result
                        assert result["result"]["execution_error"] is None, result
                        assert "interrupt option" not in result_text(result), result
                    elif error_name == "RError":
                        error = result["result"]["execution_error"]
                        assert error["ename"] == "RError", result
                        assert "current handler failure" in error["evalue"], result
                        assert "previous R failure" not in error["evalue"], result
                        assert "interrupt option" not in result_text(result), result
                    else:
                        assert "interrupt option\n" in result_text(result), result
                        assert result["result"]["execution_error"] == {
                            "ename": error_name,
                            "evalue": "R evaluation aborted"
                            if error_name == "RAbort"
                            else "R evaluation interrupted",
                            "traceback": [],
                        }, result
                    recovered = evaluator.request(
                        op="evaluate",
                        language="r",
                        # fmt: r
                        code=code("""
                            if (sys.nframe() != 0L) {
                              stop("not top level")
                            }
                            stopifnot(identical(globalCallingHandlers(), initial_handlers))
                            cat("recovered\\n")
                            """),
                        busy="wait",
                        wait_ms=30000,
                    )
                    assert result_text(recovered) == "recovered\n", recovered
                    assert recovered["result"]["execution_error"] is None, recovered
                registered = evaluator.request(
                    op="evaluate",
                    language="r",
                    # fmt: r
                    code=code("""
                        globalCallingHandlers(
                          warning = function(condition) invokeRestart("muffleWarning")
                        )
                        warning("suppressed warning")
                        globalCallingHandlers(NULL)
                        cat("global handlers usable\\n")
                        """),
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(registered) == "global handlers usable\n", registered
                assert registered["result"]["execution_error"] is None, registered
                client.expect("\n[idle]")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript,
                {
                    "evaluation.yaml": evaluator.transcript,
                    "control.yaml": control.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
