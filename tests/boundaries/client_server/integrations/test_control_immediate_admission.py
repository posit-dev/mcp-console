"""An R immediate request must not occupy the slot while Python owns the worker."""

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


if __name__ == "__main__":
    run_this_suite(__file__)
