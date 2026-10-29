"""A delayed control interrupt cannot move to a successor cell."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from boundaries.client_server.python.test_without_r import selected_environment
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION, requires
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES, PTHREAD_MUTEX_INTERPOSITION)
@executions(DIRECT)
def test_rejects_selected_interrupt_after_successor_starts(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-control-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with (
            closing(FifoCheckpoint.create(root / "selected")) as selected,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                execution.serve("--control-socket", str(endpoint)),
                {
                    **selected_environment(root),
                    LOADER_VARIABLE: str(
                        build_interposer(root, "control_interrupt_checkpoint")
                    ),
                    "MCP_CONSOLE_TEST_INTERRUPT_SELECTED": str(selected.path),
                    "MCP_CONSOLE_TEST_INTERRUPT_RELEASE": str(release.path),
                },
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="pass")
            with (
                ControlClient(endpoint) as interrupt,
                ControlClient(endpoint) as control,
            ):
                try:
                    waiting = client.send(
                        python="print(input('selected interrupt target: '))"
                    )
                    assert tool_text(waiting).endswith("[waiting for stdin]"), waiting
                    first = control.request(op="status")["result"]
                    assert first["state"] == "input_requested", first
                    first_id = first["evaluation"]["id"]
                    delayed = interrupt.start(op="interrupt", evaluation_id=first_id)
                    selected.wait(
                        "the interrupt selected its evaluation but has not queued a signal",
                        client=client,
                    )
                    supplied = control.request(
                        op="stdin", evaluation_id=first_id, input="old completed\n"
                    )
                    assert supplied["result"] == {"accepted": True}, supplied
                    client.expect("old completed\n")

                    waiting = client.send(python="print(input('successor: '))")
                    assert tool_text(waiting).endswith("[waiting for stdin]"), waiting
                    successor = control.request(op="status")["result"]
                    assert successor["state"] == "input_requested", successor
                    successor_id = successor["evaluation"]["id"]
                    assert successor_id != first_id, successor

                    release.release()
                    rejected = interrupt.receive(delayed)
                    assert "error" in rejected, rejected
                    assert rejected["error"]["code"] == "stale_evaluation", rejected
                    supplied = control.request(
                        op="stdin", evaluation_id=successor_id, input="still running\n"
                    )
                    assert supplied["result"] == {"accepted": True}, supplied
                    client.expect("still running\n")
                    client.expect("\n[idle]")
                    transcript = client.finish()
                finally:
                    release.release()
            return TranscriptWithCompanions(
                transcript,
                {
                    "control.yaml": control.transcript,
                    "interrupt.yaml": interrupt.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
