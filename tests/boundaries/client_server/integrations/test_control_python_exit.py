"""Immediate Python exits cannot retire the shared worker during reporting."""

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
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def escaped_exit_case(
    binary: Path, execution: Execution, source: str, expected_error: tuple[str, str]
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-exit-") as directory:
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
                    import os
                    import sys

                    worker_pid = os.getpid()
                    sentinel = object()
                    original = sentinel
                    """),
            )
            with ControlClient(endpoint) as control:
                failed = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code=source,
                )
                assert "result" in failed, failed
                assert result_text(failed).endswith("SystemExit: escaped exit\n"), (
                    failed
                )
                error = failed["result"]["execution_error"]
                assert error == {
                    "ename": expected_error[0],
                    "evalue": expected_error[1],
                    "traceback": error["traceback"],
                }, failed
                survived = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="print(os.getpid() == worker_pid, sentinel is original)",
                )
                assert result_text(survived) == "True True\n", survived
                assert survived["result"]["execution_error"] is None, survived
                # Private fallback diagnostics must not reach MCP, and ordinary
                # cells must retain the same worker and in-memory objects too.
                client.expect("\n[idle]")
                client.expect(
                    "True True\n",
                    python="print(os.getpid() == worker_pid, sentinel is original)",
                )
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX)
@executions(RUNTIME)
def test_contains_profile_exit_during_immediate_teardown(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    # fmt: python
    source = code("""
        evaluation_frame = sys._getframe().f_back


        def exit_on_return(frame, event, argument):
            if event == "return" and frame is evaluation_frame:
                sys.setprofile(None)
                raise SystemExit("escaped exit")


        sys.setprofile(exit_on_return)
        """)
    return escaped_exit_case(
        binary, execution, source, ("PythonError", "Python evaluation failed")
    )


@requires(POSIX)
@executions(RUNTIME)
def test_contains_stderr_exit_during_immediate_error_reporting(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    # fmt: python
    source = code("""
        original_stderr = sys.stderr


        class ExitWriter:
            def write(self, text: str) -> int:
                sys.stderr = original_stderr
                raise SystemExit("escaped exit")


        sys.stderr = ExitWriter()
        raise ValueError("original failure")
        """)
    return escaped_exit_case(
        binary, execution, source, ("ValueError", "original failure")
    )


if __name__ == "__main__":
    run_this_suite(__file__)
