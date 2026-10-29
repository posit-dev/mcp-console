"""Reentrant R warnings retain their evaluation owner."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
    wait_for_cell_started,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_reentrant_warnings_preserve_foreground_warning_ownership(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-warnings-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            McpClient(
                binary, execution.serve(*arguments), current_directory=root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(r="options(warn = 0); release <- FALSE")
            # Keep the warning and wait inside one top-level expression: R
            # normally publishes this warning when the foreground block ends.
            # fmt: r
            source = code("""
                {
                  warning("foreground deferred", call. = FALSE)
                  gate <- fifo("ENTERED", "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), gate)
                  close(gate)
                  while (!release) {
                    Sys.sleep(0.01)
                  }
                  cat("foreground complete\\n")
                }
                """).replace("ENTERED", str(entered.path))
            pending = client.start_send(r=source, timeout_ms=60000)
            entered.wait(client=client)
            wait_for_cell_started(client, root, source)
            with ControlClient(endpoint) as control:
                deferred = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='stopifnot(getOption("warn") == 0); warning("priority deferred", call. = FALSE); cat("priority complete\\n")',
                )
                text = result_text(deferred)
                assert "priority complete\n" in text and "priority deferred" in text, (
                    deferred
                )
                assert "foreground deferred" not in text, deferred
                assert deferred["result"]["execution_error"] is None, deferred
                suppressed = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='options(warn = -1); warning("priority suppressed", call. = FALSE)',
                )
                assert result_text(suppressed) == "", suppressed
                immediate = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='options(warn = 1); warning("priority immediate", call. = FALSE); cat("after immediate\\n")',
                )
                assert (
                    result_text(immediate)
                    == "Warning: priority immediate\nafter immediate\n"
                ), immediate
                converted = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code='options(warn = 2); warning("priority converted", call. = FALSE)',
                )
                assert converted["result"]["execution_error"]["ename"] == "RError", (
                    converted
                )
                assert "priority converted" in result_text(converted), converted
                assert "foreground deferred" not in result_text(converted), converted
                resumed = control.request(
                    op="evaluate",
                    language="r",
                    execution="immediate",
                    code="options(warn = 0); release <- TRUE",
                )
                assert result_text(resumed) == "", resumed
                client.receive(pending)
                foreground = result_text(pending)
                assert "foreground complete\n" in foreground, pending
                assert foreground.count("foreground deferred") == 1, pending
                assert "priority" not in foreground, pending
                client.expect("next cell\n", r='cat("next cell\\n")')
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


if __name__ == "__main__":
    run_this_suite(__file__)
