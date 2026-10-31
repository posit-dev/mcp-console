"""R preflight interruption reports the current condition and withholds source."""

import json
import sys
import tempfile
from pathlib import Path
from textwrap import indent

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
def test_preflight_interrupt_preserves_condition_and_source_ownership(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-preflight-"
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
                    preflight_source_ran <- FALSE
                    options(interrupt = function() cat("preflight interrupt option\\n"))
                    """),
            )
            with (
                ControlClient(endpoint) as evaluator,
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe")
                for caught in (False, True):
                    failed = evaluator.request(
                        op="evaluate",
                        language="r",
                        code='stop("earlier R failure")',
                        busy="wait",
                        wait_ms=30000,
                    )
                    assert (
                        "earlier R failure"
                        in failed["result"]["execution_error"]["evalue"]
                    ), failed
                    # The public trace hook removes itself before blocking, so
                    # recovery can parse normally after the interrupt.
                    # fmt: r
                    trace_source = code("""
                        invisible(trace(
                          "str2expression",
                          where = baseenv(),
                          print = FALSE,
                          tracer = quote({
                            untrace("str2expression", where = baseenv())
                            WAIT
                          })
                        ))
                        """)
                    if caught:
                        # fmt: r
                        wait = code("""
                            tryCatch(
                              readline("handled preflight: "),
                              interrupt = function(condition) cat("caught preflight interrupt\\n")
                            )
                            """)
                    else:
                        wait = 'readline("uncaught preflight: ")'
                    traced = evaluator.request(
                        op="evaluate",
                        language="r",
                        code=trace_source.replace(
                            "    WAIT", indent(wait.rstrip(), "    ")
                        ),
                        busy="wait",
                        wait_ms=30000,
                    )
                    assert traced["result"]["execution_error"] is None, traced
                    pending = evaluator.start(
                        op="evaluate",
                        language="r",
                        code="preflight_source_ran <- TRUE",
                        busy="wait",
                        wait_ms=30000,
                    )
                    prompt = "handled preflight: " if caught else "uncaught preflight: "
                    while True:
                        event = json.loads(observer.reader.readline(timeout=30))
                        assert event["event"] == "preview", event
                        if prompt in result_text(event):
                            break
                    status = control.request(op="status")["result"]
                    assert status["state"] == "input_requested", status
                    accepted = control.request(
                        op="interrupt", evaluation_id=status["evaluation"]["id"]
                    )
                    assert accepted["result"] == {"accepted": True}, accepted
                    result = evaluator.receive(pending)
                    assert "earlier R failure" not in result_text(result), result
                    if caught:
                        assert "caught preflight interrupt\n" in result_text(result), (
                            result
                        )
                        assert "preflight interrupt option" not in result_text(
                            result
                        ), result
                        assert result["result"]["execution_error"] is None, result
                    else:
                        assert "preflight interrupt option\n" in result_text(result), (
                            result
                        )
                        assert result["result"]["execution_error"] == {
                            "ename": "Interrupt",
                            "evalue": "R evaluation interrupted",
                            "traceback": [],
                        }, result
                    # fmt: r
                    recovery = code("""
                        stopifnot(identical(preflight_source_ran, EXPECTED))
                        if (sys.nframe() != 0L) {
                          stop("not top level")
                        }
                        cat("recovered\\n")
                        """).replace("EXPECTED", "TRUE" if caught else "FALSE")
                    recovered = evaluator.request(
                        op="evaluate",
                        language="r",
                        code=recovery,
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
                    "control.yaml": control.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
