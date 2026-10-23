#!/usr/bin/env -S uv run --script

import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed_result
from support.assertions import assert_result_content, last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_workers_keep_plot_files_separate(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    with tempfile.TemporaryDirectory() as directory:
        environment["TMPDIR"] = directory
        expected = [
            reference_plots(
                rscript,
                environment,
                source,
                width=4,
                height=3,
                dpi=100,
                pages=1,
            )[0]
            for source in ("plot(1:3)", "plot(3:1)")
        ]
        with (
            McpClient(binary, execution.serve(), environment) as first,
            McpClient(binary, execution.serve(), environment) as second,
            ExitStack() as checkpoints,
        ):
            directories = []
            releases = []
            for client, values in ((first, "1:3"), (second, "3:1")):
                client.initialize_and_list_tools()
                # Create checkpoints inside each worker's writable directory.
                # fmt: r
                r = code(r"""
                    plot_checkpoint <- tempfile("plot-checkpoint-")
                    cat(plot_checkpoint)
                    """)
                client.send(r=r)
                checkpoint = Path(last_tool_text(client))
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<plot checkpoint directory>"
                )
                checkpoint.mkdir()
                ready = FifoCheckpoint.create(checkpoint / "ready")
                release = FifoCheckpoint.create(checkpoint / "release")
                checkpoints.callback(ready.close)
                checkpoints.callback(release.close)
                checkpoints.callback(release.release)
                releases.append(release)
                # Keep both devices open under the same inherited TMPDIR.
                # fmt: r
                r = code(r"""
                    options(
                      console.plot.width_in = 4,
                      console.plot.height_in = 3,
                      console.plot.dpi = 100
                    )
                    plot(VALUES)
                    path <- attr(.Devices[[dev.cur()]], "filepath")
                    writeLines(dirname(path), file.path(plot_checkpoint, "directory"))
                    ready <- fifo(
                      file.path(plot_checkpoint, "ready"),
                      open = "wb",
                      blocking = TRUE
                    )
                    writeBin(charToRaw("1"), ready)
                    close(ready)
                    release <- fifo(
                      file.path(plot_checkpoint, "release"),
                      open = "rb",
                      blocking = TRUE
                    )
                    stopifnot(identical(
                      readBin(release, "raw", n = 1L),
                      charToRaw("1")
                    ))
                    close(release)
                    """).replace("VALUES", values)
                evaluation = client.start_send(r=r, timeout_ms=0)
                ready.wait("plot device is open")
                client.receive(evaluation)
                assert without_elapsed_result(evaluation["result"]) == {
                    "content": [
                        {"type": "text", "text": "\n[running; poll with an empty send]"}
                    ],
                    "isError": False,
                }, evaluation
                directories.append(Path((checkpoint / "directory").read_text().strip()))
            assert directories[0] != directories[1], directories

            for client, image, release in (
                (second, expected[1], releases[1]),
                (first, expected[0], releases[0]),
            ):
                release.release()
                client.send()
                assert_result_content(client, [image])
            return first.finish() + second.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
