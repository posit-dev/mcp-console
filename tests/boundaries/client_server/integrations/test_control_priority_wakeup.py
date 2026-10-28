"""Priority R wakeups remain bounded while foreground execution services events."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.progress import RUNNING, without_elapsed
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_many_reentrant_requests_preserve_ingress_and_rearm_idle_wakeup(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with McpClient(
            binary, execution.serve(*arguments), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="wakeup_count <- 0L")
            with ControlClient(endpoint) as control:
                # 4096 unread wakeup frames exceed both a 16 KiB and 64 KiB
                # command pipe. Foreground completion requires a later priority
                # request, so blocked ingress cannot recover by draining the pipe.
                for cycle, repetitions in enumerate((4096, 1)):
                    with closing(
                        FifoCheckpoint.create(root / f"entered-{cycle}")
                    ) as entered:
                        # fmt: r
                        source = code("""
                            wakeup_release <- FALSE
                            gate <- fifo("ENTERED", "wb", blocking = TRUE)
                            writeBin(charToRaw("1"), gate)
                            close(gate)
                            while (!wakeup_release) {
                              Sys.sleep(0.001)
                            }
                            cat("foreground complete\\n")
                            """).replace("ENTERED", str(entered.path))
                        pending = client.send(r=source, timeout_ms=0)
                        assert without_elapsed(tool_text(pending)) == RUNNING, pending
                        entered.wait(
                            "foreground is held until a priority request releases it",
                            client=client,
                        )
                        for _ in range(repetitions):
                            response = control.request(
                                op="evaluate",
                                language="r",
                                execution="immediate",
                                code="wakeup_count <- wakeup_count + 1L; cat('ok\\n')",
                            )
                            assert response == {
                                "id": str(len(control.transcript)),
                                "result": {
                                    "resultType": "complete",
                                    "content": [{"type": "text", "text": "ok\n"}],
                                    "isError": False,
                                    "execution_error": None,
                                },
                            }, response
                        released = control.request(
                            op="evaluate",
                            language="r",
                            execution="immediate",
                            code="wakeup_release <- TRUE; cat('released\\n')",
                        )
                        assert result_text(released) == "released\n", released
                        assert released["result"]["execution_error"] is None, released
                        client.expect("foreground complete\n")
                        # An ordinary round trip follows the queued wakeup in
                        # wire order, proving its consumption before the idle call.
                        client.expect(f"[1] {4096 + cycle}\n", r="wakeup_count")
                        idle = control.request(
                            op="evaluate",
                            language="r",
                            execution="immediate",
                            code="cat('idle wakeup\\n')",
                        )
                        assert result_text(idle) == "idle wakeup\n", idle
                        assert idle["result"]["execution_error"] is None, idle

                # Every stress reply was compared in full above. Preserve one
                # representative with explicit lossless repetition notation.
                first = control.transcript[0]
                first["transcript_normalization"] = {
                    "target": "identical completed priority evaluations",
                    "repeated_exchange_count": 4096,
                    "request_ids": "1 through 4096, distinct in each exchange",
                }
                control.transcript[:4096] = [first]
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


if __name__ == "__main__":
    run_this_suite(__file__)
