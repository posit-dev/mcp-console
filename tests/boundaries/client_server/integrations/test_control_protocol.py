"""Internal control replies retain language failures independently of console text."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from boundaries.client_server.python.test_without_r import selected_environment
from support.client import McpClient
from support.assertions import wait_for_evaluation_output
from support.execution import DIRECT, Execution, executions
from support.records import TranscriptWithCompanions
from support.normalization import normalize_recording
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT)
def test_bounds_structured_language_errors(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
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
            client.expect(python="pass")
            with ControlClient(endpoint) as control:
                for mode in ("serial", "immediate"):
                    options = (
                        {"busy": "wait", "wait_ms": 30000} if mode == "serial" else {}
                    )
                    failed = control.request(
                        op="evaluate",
                        language="python",
                        execution=mode,
                        code="raise ValueError('界' * 50000)",
                        **options,
                    )
                    error = failed["result"]["execution_error"]
                    strings = [error["ename"], error["evalue"], *error["traceback"]]
                    assert sum(len(text.encode()) for text in strings) <= 8192, (
                        "structured language error exceeded its 8 KiB budget"
                    )
                    assert (
                        error["truncated"] is True and error["ename"] == "ValueError"
                    ), error
                    assert error["evalue"] and set(error["evalue"]) == {"界"}, error
                    assert len(result_text(failed).encode()) <= 8192, failed
                recovered = control.request(
                    op="evaluate",
                    language="python",
                    code="print('usable')",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(recovered) == "usable\n", recovered
                assert recovered["result"]["execution_error"] is None, recovered
                return TranscriptWithCompanions(
                    normalize_recording(client.finish(), root),
                    {"control.yaml": normalize_recording(control.transcript, root)},
                )


@requires(POSIX)
@executions(DIRECT)
def test_reports_language_errors_separately_from_diagnostics(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
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
            client.expect(python="pass")
            with ControlClient(endpoint) as control:
                diagnostic = control.request(
                    op="evaluate",
                    language="python",
                    code="import sys; print('diagnostic only', file=sys.stderr)",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(diagnostic) == "diagnostic only\n", diagnostic
                assert diagnostic["result"]["execution_error"] is None, diagnostic
                failed = control.request(
                    op="evaluate",
                    language="python",
                    code="raise ValueError('invalid value')",
                    busy="wait",
                    wait_ms=30000,
                )
                text = result_text(failed)
                error = failed["result"]["execution_error"]
                assert error == {
                    "ename": "ValueError",
                    "evalue": "invalid value",
                    "traceback": error["traceback"],
                }, failed
                assert "".join(error["traceback"]) == text, failed
                recovered = control.request(
                    op="evaluate",
                    language="python",
                    code="42",
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(recovered) == "42\n", recovered
                assert recovered["result"]["execution_error"] is None, recovered
                client.expect("\n[idle]")
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX)
@executions(DIRECT)
def test_pins_input_and_interrupt_to_observed_evaluation(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
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
            client.expect(python="pass")
            waiting = client.send(
                python="answer = input('answer: '); print('received:', answer)"
            )
            assert waiting["content"][-1]["text"].endswith("[waiting for stdin]"), (
                waiting
            )
            with ControlClient(endpoint) as control:
                status = control.request(op="status")["result"]
                assert status["state"] == "input_requested", status
                evaluation_id = status["evaluation"]["id"]
                accepted = control.request(
                    op="stdin", evaluation_id=evaluation_id, input="42\n"
                )
                assert accepted["result"] == {"accepted": True}, accepted
                client.expect("received: 42\n")

                waiting = client.send(python="input('second answer: ')")
                assert waiting["content"][-1]["text"].endswith("[waiting for stdin]"), (
                    waiting
                )
                status = control.request(op="status")["result"]
                assert status["state"] == "input_requested", status
                next_id = status["evaluation"]["id"]
                assert next_id != evaluation_id, status
                for op, fields in (("stdin", {"input": "stale\n"}), ("interrupt", {})):
                    rejected = control.request(
                        op=op, evaluation_id=evaluation_id, **fields
                    )
                    assert rejected["error"]["code"] == "stale_evaluation", rejected
                accepted = control.request(op="interrupt", evaluation_id=next_id)
                assert accepted["result"] == {"accepted": True}, accepted
                result = wait_for_evaluation_output(
                    client,
                    lambda text: "KeyboardInterrupt" in text,
                    "interrupted managed input",
                )
                assert "KeyboardInterrupt" in result, result
                client.expect("survived\n", python="print('survived')")
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX)
@executions(DIRECT)
def test_answers_and_interrupts_private_integrator_input(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
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
            client.expect(python="pass")
            with (
                ControlClient(endpoint) as execution_control,
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe")
                initial = json.loads(observer.reader.readline(timeout=30))
                assert result_text(initial) == "", initial
                for prompt, operation in (
                    ("private answer", "stdin"),
                    ("cancel answer", "interrupt"),
                ):
                    pending = execution_control.start(
                        op="evaluate",
                        language="python",
                        code=f"value = input('{prompt}: '); print(value)",
                        busy="wait",
                        wait_ms=30000,
                    )
                    while True:
                        event = json.loads(observer.reader.readline(timeout=30))
                        assert event["event"] == "preview", event
                        if prompt in result_text(event):
                            break
                    status = control.request(op="status")["result"]
                    assert status["state"] == "input_requested", status
                    assert status["evaluation"]["origin"] == "integrator", status
                    fields = (
                        {"input": "private reply\n"} if operation == "stdin" else {}
                    )
                    accepted = control.request(
                        op=operation, evaluation_id=status["evaluation"]["id"], **fields
                    )
                    assert accepted["result"] == {"accepted": True}, accepted
                    result = execution_control.receive(pending)
                    if operation == "stdin":
                        assert "private reply\n" in result_text(result), result
                        assert result["result"]["execution_error"] is None, result
                    else:
                        assert "KeyboardInterrupt" in result_text(result), result
                        assert (
                            result["result"]["execution_error"]["ename"]
                            == "KeyboardInterrupt"
                        ), result
                    client.expect("\n[idle]")
                return TranscriptWithCompanions(
                    client.finish(),
                    {
                        "control.yaml": control.transcript,
                        "evaluation.yaml": execution_control.transcript,
                    },
                )


if __name__ == "__main__":
    run_this_suite(__file__)
