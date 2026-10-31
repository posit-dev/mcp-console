"""Immediate Python owns managed output through thread-state cleanup."""

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
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(RUNTIME)
def test_immediate_python_keeps_thread_local_cleanup_output_private(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-python-cleanup-"
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
                result = control.request(
                    op="evaluate",
                    language="python",
                    execution="immediate",
                    record=False,
                    # The instance has no reference outside this thread's local
                    # dictionary, so CPython destroys it when that thread exits.
                    # fmt: python
                    code=code("""
                        import sys
                        import threading

                        cleanup_local = threading.local()

                        class CleanupOutput:
                            def __del__(self):
                                print("private thread cleanup", flush=True)
                                print("private cleanup diagnostic", file=sys.stderr, flush=True)

                        cleanup_local.value = CleanupOutput()
                        print("private body")
                        """),
                )
                assert result_text(result) == (
                    "private body\nprivate thread cleanup\nprivate cleanup diagnostic\n"
                ), result
                assert result["result"]["execution_error"] is None, result
                client.expect("\n[idle]")
                client.expect("worker usable\n", python='print("worker usable")')
                transcript = normalize_recording(client.finish(), root)
            (recording,) = (root / ".agents/console/sessions").iterdir()
            markdown = (recording / "transcript.md").read_text()
            raw = "".join(
                path.read_text()
                for path in (recording / "capture/sessions").glob("*/*.txt")
            )
            for text in (
                "private body",
                "private thread cleanup",
                "private cleanup diagnostic",
            ):
                assert text not in markdown, markdown
                assert text not in raw, raw
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


if __name__ == "__main__":
    run_this_suite(__file__)
