#!/usr/bin/env -S uv run --script

import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_startup import selected_python
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.progress import RUNNING, without_elapsed
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING, requires
from support.suites import run_this_suite


def interrupt_held_evaluator(
    binary: Path, execution: Execution, *, before_wait: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "bootstrap-completing",
                "bootstrap-release",
                "bootstrap-signal",
                "evaluator-waiting",
                "evaluator-reached",
                "evaluator-release",
                "receipt-processed",
            )
        }
        environment = selected_python(root, Path(sys.executable))
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "evaluator_bootstrap_checkpoint")
                ),
                "MCP_CONSOLE_LANGUAGES": "python",
                "MCP_CONSOLE_TEST_BOOTSTRAP_INTERPOSER": str(
                    build_interposer(root, "bootstrap_completion_checkpoint")
                ),
                "MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETING": str(
                    checkpoints["bootstrap-completing"].path
                ),
                "MCP_CONSOLE_TEST_BOOTSTRAP_COMPLETE": str(
                    checkpoints["bootstrap-release"].path
                ),
                "MCP_CONSOLE_TEST_BOOTSTRAP_SIGNAL": str(
                    checkpoints["bootstrap-signal"].path
                ),
                "MCP_CONSOLE_TEST_EVALUATOR_WAITING": str(
                    checkpoints["evaluator-waiting"].path
                ),
                "MCP_CONSOLE_TEST_EVALUATOR_REACHED": str(
                    checkpoints["evaluator-reached"].path
                ),
                "MCP_CONSOLE_TEST_EVALUATOR_RELEASE": str(
                    checkpoints["evaluator-release"].path
                ),
                "MCP_CONSOLE_TEST_RECEIPT_PROCESSED": str(
                    checkpoints["receipt-processed"].path
                ),
            }
        )
        if before_wait:
            environment["MCP_CONSOLE_TEST_EVALUATOR_BEFORE_WAIT"] = "1"
        with McpClient(binary, execution.serve(), environment, root) as client:
            try:
                checkpoints["bootstrap-completing"].wait(
                    "the worker already sampled its non-interrupted bootstrap receipt",
                    timeout=client.response_timeout,
                )
                client.initialize_and_list_tools()
                accepted = client.send(python="discarded_cell = True", timeout_ms=0)
                assert without_elapsed(tool_text(accepted)) == RUNNING, accepted
                if before_wait:
                    checkpoints["evaluator-reached"].wait(
                        "the accepted evaluator has not begun its first bootstrap wait"
                    )
                else:
                    checkpoints["evaluator-waiting"].wait(
                        "the evaluator entered its bootstrap condition wait"
                    )
                    checkpoints["bootstrap-release"].release()
                    checkpoints["evaluator-reached"].wait(
                        "the bootstrap receipt woke the evaluator before Evaluate dispatch"
                    )
                interrupted = client.send(control="interrupt", timeout_ms=0)
                assert without_elapsed(tool_text(interrupted)) == RUNNING, interrupted
                if before_wait:
                    checkpoints["bootstrap-signal"].wait(
                        "the worker handled the admitted interrupt before receipt publication"
                    )
                    checkpoints["bootstrap-release"].release()
                checkpoints["receipt-processed"].wait(
                    "the controller committed RuntimeInitialized while dispatch is pending"
                )
                checkpoints["evaluator-release"].release()
                settled = client.send()
                assert tool_text(settled) == "[done]", settled
                fresh = client.send(
                    python="assert 'discarded_cell' not in globals(); 42"
                )
                assert tool_text(fresh) == "42\n", fresh
                return client.finish()
            finally:
                checkpoints["bootstrap-release"].release()
                checkpoints["evaluator-release"].release()


@requires(NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
@executions(DIRECT)
def test_interrupt_before_evaluator_bootstrap_wait_withholds_accepted_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return interrupt_held_evaluator(binary, execution, before_wait=True)


@requires(NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
@executions(DIRECT)
def test_interrupt_after_bootstrap_receipt_before_dispatch_withholds_accepted_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return interrupt_held_evaluator(binary, execution, before_wait=False)


if __name__ == "__main__":
    run_this_suite(__file__)
