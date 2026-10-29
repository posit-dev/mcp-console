"""Immediate R admission preserves ordinary execution and interrupt ownership."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
    wait_for_queued_integrator,
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


@requires(POSIX, R)
@executions(RUNTIME)
def test_idle_immediate_r_rejects_ordinary_source_and_releases_waiting_integrator(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        release = root / "release"
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            McpClient(
                binary,
                execution.serve(
                    "--control-socket", str(endpoint), "--writable-root", str(root)
                ),
                current_directory=root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(r="priority_unwound <- FALSE")
            with (
                ControlClient(endpoint) as priority,
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as inspector,
            ):
                # The FIFO proves immediate R has entered. The file gate is
                # failure cleanup; successful execution ends through interrupt.
                # fmt: r
                source = code("""
                    (function() {
                      on.exit(priority_unwound <<- TRUE)
                      gate <- fifo("ENTERED", "wb", blocking = TRUE)
                      writeBin(charToRaw("1"), gate)
                      close(gate)
                      while (!file.exists("release")) {
                        Sys.sleep(0.01)
                      }
                    })()
                    """).replace("ENTERED", str(entered.path))
                immediate = priority.start(
                    op="evaluate", language="r", execution="immediate", code=source
                )
                try:
                    entered.wait("idle immediate R is running", client=client)
                    for arguments in (
                        {"r": 'writeLines("ordinary R ran", "ordinary-r-ran")'},
                        {"python": 'open("ordinary-python-ran", "w").close()'},
                    ):
                        rejected = client.send(**arguments, timeout_ms=0)
                        assert rejected == {
                            "content": [
                                {
                                    "type": "text",
                                    "text": "[immediate R execution is in progress; cell was not run]",
                                }
                            ],
                            "isError": True,
                        }, rejected
                    rejected = control.request(
                        op="evaluate",
                        language="r",
                        code='writeLines("serial control ran", "control-ran")',
                        busy="reject",
                    )
                    assert rejected["error"]["code"] == "busy", rejected
                    queued_source = (
                        'stopifnot(priority_unwound); cat("queued serial\\n")'
                    )
                    queued = control.start(
                        op="evaluate",
                        language="r",
                        code=queued_source,
                        busy="wait",
                        wait_ms=30000,
                    )
                    wait_for_queued_integrator(client, root, queued_source)
                    status = inspector.request(op="status")["result"]
                    assert status["state"] == "idle" and status["evaluation"] is None, (
                        status
                    )
                    interrupted = client.send(control="interrupt")
                    assert not interrupted.get("isError"), interrupted
                    private = priority.receive(immediate)
                    assert (
                        private["result"]["execution_error"]["ename"] == "Interrupt"
                    ), private
                    result = control.receive(queued)
                    assert result_text(result) == "queued serial\n", result
                    assert result["result"]["execution_error"] is None, result
                    for marker in (
                        "ordinary-r-ran",
                        "ordinary-python-ran",
                        "control-ran",
                    ):
                        assert not (root / marker).exists(), (
                            f"rejected source executed: {marker}"
                        )
                    client.expect(
                        "worker usable\n",
                        r='stopifnot(priority_unwound); cat("worker usable\\n")',
                    )
                    transcript = normalize_recording(client.finish(), root)
                finally:
                    release.touch()
            return TranscriptWithCompanions(
                transcript,
                {
                    "priority.yaml": normalize_recording(priority.transcript, root),
                    "control.yaml": control.transcript,
                    "status.yaml": inspector.transcript,
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
