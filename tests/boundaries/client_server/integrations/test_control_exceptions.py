"""Language diagnostics survive exceptions raised by an exception's formatter."""

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
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, SQL, requires
from support.suites import run_this_suite


def python_formatter_case(
    binary: Path, execution: Execution, mode: str
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-errors-") as directory:
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
            client.expect(
                # fmt: python
                python=code("""
                    sentinel = object()
                    original = sentinel


                    class BrokenMessage(Exception):
                        calls = 0
                        formatter_failure = RuntimeError

                        def __str__(self) -> str:
                            type(self).calls += 1
                            raise self.formatter_failure("formatter failure")
                    """)
            )
            with ControlClient(endpoint) as control:
                for failure in ("RuntimeError", "SystemExit"):
                    client.expect(
                        python=f"BrokenMessage.calls = 0; BrokenMessage.formatter_failure = {failure}"
                    )
                    options = (
                        {"busy": "wait", "wait_ms": 30000} if mode == "serial" else {}
                    )
                    failed = control.request(
                        op="evaluate",
                        language="python",
                        execution=mode,
                        code="raise BrokenMessage()",
                        **options,
                    )
                    assert "result" in failed, failed
                    error = failed["result"]["execution_error"]
                    assert error["ename"] == "BrokenMessage", failed
                    assert error["evalue"] == "<exception str() failed>", failed
                    text = result_text(failed)
                    assert text.endswith("BrokenMessage: <exception str() failed>\n"), (
                        failed
                    )
                    assert "".join(error["traceback"]) == text, failed
                    # Diagnostics belong only to the control result. The same
                    # worker must remain available, with one formatting attempt.
                    client.expect("\n[idle]")
                    client.expect(
                        "1 True\n",
                        python="print(BrokenMessage.calls, sentinel is original)",
                    )
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_serial_preserves_errors_with_broken_messages(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    return python_formatter_case(binary, execution, "serial")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_python_thread_preserves_errors_with_broken_messages(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    return python_formatter_case(binary, execution, "python_thread")


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_preserves_errors_with_broken_messages(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-errors-") as directory:
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
            client.expect(
                # fmt: python
                python=code("""
                    sentinel = object()
                    original = sentinel
                    formatter_calls = 0
                    formatter_failure = RuntimeError


                    def broken_message(self: BaseException) -> str:
                        global formatter_calls
                        formatter_calls += 1
                        raise formatter_failure("formatter failure")


                    class BrokenMessage(Exception):
                        __str__ = broken_message


                    class BrokenBaseMessage(BaseException):
                        __str__ = broken_message


                    class BrokenConnection:
                        def cursor(self):
                            return self

                        def execute(self, source: str):
                            raise driver_error()


                    _console.sql_connection(BrokenConnection())
                    """)
            )
            with ControlClient(endpoint) as control:
                for driver in ("BrokenMessage", "BrokenBaseMessage"):
                    for failure in ("RuntimeError", "SystemExit"):
                        client.expect(
                            python=f"formatter_calls = 0; formatter_failure = {failure}; driver_error = {driver}"
                        )
                        failed = control.request(
                            op="evaluate",
                            language="sql",
                            code="SELECT 1",
                            busy="wait",
                            wait_ms=30000,
                        )
                        assert "result" in failed, failed
                        error = failed["result"]["execution_error"]
                        assert error["ename"] == driver, failed
                        assert error["evalue"] == "<exception str() failed>", failed
                        text = result_text(failed)
                        if driver == "BrokenMessage":
                            assert text == "Error: <exception str() failed>\n", failed
                        else:
                            assert text.endswith(
                                "BrokenBaseMessage: <exception str() failed>\n"
                            ), failed
                        client.expect("\n[idle]")
                        client.expect(
                            "1 True\n",
                            python="print(formatter_calls, sentinel is original)",
                        )
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


if __name__ == "__main__":
    run_this_suite(__file__)
