"""Empty polls preserve the console output exposed to MCP hosts."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.progress import without_elapsed_result
from support.records import Transcript, without_meta
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(RUNTIME)
def test_empty_poll_preserves_completed_r_errors(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(root)) if execution == SANDBOXED else ())
            ),
            current_directory=root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Empty polls expose completed R errors with one or multiple active sessions.",
                    "fixture": "A FIFO holds each cell until its initial send has returned, avoiding timing-dependent sleeps.",
                }
            )
            client.initialize_and_list_tools()
            client.expect(r="invisible(NULL)")
            for stage in ("default-only", "two-sessions", "named-stopped"):
                if stage == "two-sessions":
                    client.expect(r="invisible(NULL)", session="alpha")
                elif stage == "named-stopped":
                    client.expect(
                        "[worker stopped: in-memory state lost]\n[stopped]",
                        control="restart",
                        session="alpha",
                    )
                with closing(FifoCheckpoint.create(root / stage)) as release:
                    pending = client.send(
                        # fmt: r
                        r=code("""
                            release <- fifo("GATE", open = "rb", blocking = TRUE)
                            stopifnot(identical(readBin(release, "raw", 1L), charToRaw("1")))
                            close(release)
                            stop("console-poll-mre")
                            """).replace("GATE", stage),
                        timeout_ms=0,
                    )
                    try:
                        assert without_meta(without_elapsed_result(pending)) == {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "\n[running; poll with an empty send]",
                                }
                            ],
                            "isError": False,
                        }, pending
                    finally:
                        release.release()
                    completed = client.send(timeout_ms=60_000)
                    expected = "Error: console-poll-mre\n"
                    if stage == "two-sessions":
                        expected = (
                            '[session: "alpha"; status: idle]\n\n[idle]\n'
                            '[session: "default"; status: idle]\n' + expected
                        )
                    # Hosts can prefer structuredContent over content, so the
                    # complete result must not include a status-only substitute.
                    assert without_meta(completed) == {
                        "content": [{"type": "text", "text": expected}],
                        "isError": False,
                    }, completed
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
