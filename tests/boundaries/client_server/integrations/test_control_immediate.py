"""The public immediate mode selects its runtime from the requested language."""

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
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_dispatches_immediate_execution_by_language(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="shared_r <- 40L")
            client.expect(python="import threading; shared_python = 40")
            with ControlClient(endpoint) as control:
                result = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code="shared_r <- shared_r + 2L; shared_r",
                )
                assert result_text(result) == "[1] 42\n", result
                assert result["result"]["execution_error"] is None, result
                result = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="assert threading.current_thread() is not threading.main_thread(); shared_python += 2; print(shared_python)",
                )
                assert result_text(result) == "42\n", result
                assert result["result"]["execution_error"] is None, result
                client.expect("[1] 42\n", r="shared_r")
                client.expect("42\n", python="print(shared_python)")
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX)
@executions(RUNTIME)
def test_rejects_old_mode_names_and_immediate_sql(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-"
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
            client.expect(python="sentinel = 0")
            with ControlClient(endpoint) as control:
                for old_mode in ("python_thread", "r_reentrant"):
                    pending = control.start(
                        op="evaluate",
                        language="python",
                        execution=old_mode,
                        code="sentinel = 1",
                    )
                    rejected = control.receive(pending, invalid=True)
                    assert rejected["error"]["code"] == "invalid_request", rejected
                rejected = control.request(
                    op="evaluate",
                    language="sql",
                    execution="immediate",
                    code="SELECT 1",
                )
                assert rejected["error"] == {
                    "code": "execution_failed",
                    "message": "immediate execution supports only R and Python",
                }, rejected
                rejected = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    code="sentinel = 1",
                    busy="wait",
                    wait_ms=30000,
                )
                assert rejected["error"]["code"] == "invalid_request", rejected
                client.expect("0\n", python="print(sentinel)")
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


if __name__ == "__main__":
    run_this_suite(__file__)
