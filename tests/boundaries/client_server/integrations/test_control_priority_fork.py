"""Fork children never execute immediate R work queued for their parent."""

import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    wait_for_cell_started,
)
from support.assertions import tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code, normalize_recording
from support.progress import RUNNING, without_elapsed
from support.r import r_test_environment
from support.records import TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX, R, NATIVE_FIXTURES)
@executions(RUNTIME)
def test_queued_immediate_r_runs_only_in_parent_after_fork(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-priority-fork-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        # Keep the submitted library path identical across native platforms.
        native = build_interposer(root, "control_priority_fork").rename(
            root / "fork-checkpoint"
        )
        observation = build_interposer(root, "recording_observation")
        wrapper = root / "worker-wrapper"
        wrapper.write_text(
            "#!/usr/bin/env python3\n"
            # fmt: python
            + code("""
                import os
                import sys

                binary = os.environ["MCP_CONSOLE_TEST_BINARY"]
                if sys.platform == "linux":
                    # Keep worker loader setup from re-executing after the
                    # observation fixture removes its injection variable.
                    paths = [os.path.join(os.environ["R_HOME"], "lib")]
                    if previous := os.environ.get("LD_LIBRARY_PATH"):
                        paths.append(previous)
                    os.environ["LD_LIBRARY_PATH"] = os.pathsep.join(paths)
                variable = os.environ.pop("MCP_CONSOLE_TEST_LOADER_VARIABLE")
                os.environ[variable] = os.environ.pop("MCP_CONSOLE_TEST_OBSERVATION_LIBRARY")
                os.execv(binary, [binary, "worker"])
                """),
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        arguments = ["--control-socket", str(endpoint), "--worker", str(wrapper)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        environment, _ = r_test_environment()
        with (
            closing(FifoCheckpoint.create(root / "fork-reached")) as reached,
            closing(FifoCheckpoint.create(root / "fork-release")) as release,
            closing(FifoCheckpoint.create(root / "priority-queued")) as queued,
        ):
            environment.update(
                MCP_CONSOLE_TEST_BINARY=str(binary),
                MCP_CONSOLE_TEST_LOADER_VARIABLE=LOADER_VARIABLE,
                MCP_CONSOLE_TEST_OBSERVATION_LIBRARY=str(observation),
                MCP_CONSOLE_TEST_OBSERVED_TEXT='"kind":"priority_ready"',
                MCP_CONSOLE_TEST_JOURNAL_OBSERVED=str(queued.path),
                MCP_CONSOLE_TEST_FORK_REACHED=str(reached.path),
                MCP_CONSOLE_TEST_FORK_RELEASE=str(release.path),
            )
            with McpClient(
                binary, execution.serve(*arguments), environment, root
            ) as client:
                client.transcript.append(
                    {
                        "scenario": "Immediate R work queued before a foreground fork runs exactly once in the parent worker.",
                        "fixture": "A native R call gates the fork without processing events. An observer confirms the worker ingress wrote PriorityReady after queueing the request. The child checks R events before the parent resumes.",
                    }
                )
                client.initialize_and_list_tools()
                # fmt: r
                setup = code("""
                    dyn.load("NATIVE")
                    worker_pid <- Sys.getpid()
                    """).replace("NATIVE", str(native))
                client.expect(r=setup)
                with ControlClient(endpoint) as control:
                    try:
                        # fmt: r
                        foreground = code("""
                            status <- .C("mcp_test_priority_fork", status = integer(1))$status
                            stopifnot(identical(status, 0L))
                            cat("foreground complete\\n")
                            """)
                        pending = client.send(r=foreground, timeout_ms=0)
                        assert without_elapsed(tool_text(pending)) == RUNNING, pending
                        reached.wait(
                            "foreground stopped immediately before fork", client=client
                        )
                        wait_for_cell_started(client, root, foreground)
                        # fmt: r
                        immediate = code("""
                            cat(
                              if (Sys.getpid() == worker_pid) "parent\\n" else "child\\n",
                              file = "priority-effects",
                              append = TRUE
                            )
                            cat("immediate complete\\n")
                            """)
                        request = control.start(
                            op="evaluate",
                            language="r",
                            execution="immediate",
                            code=immediate,
                        )
                        queued.wait(
                            "immediate request queued before fork", client=client
                        )
                        release.release()
                        response = control.receive(request)
                        assert response == {
                            "id": request["request"]["id"],
                            "result": {
                                "resultType": "complete",
                                "content": [
                                    {"type": "text", "text": "immediate complete\n"}
                                ],
                                "isError": False,
                                "execution_error": None,
                            },
                        }, response
                        client.expect("foreground complete\n")
                        effects = (root / "priority-effects").read_text()
                        assert effects == "parent\n", effects
                        client.transcript.append({"immediate_side_effects": effects})
                        client.expect(
                            "parent remains usable\n",
                            r='cat("parent remains usable\\n")',
                        )
                        transcript = normalize_recording(client.finish(), root)
                    finally:
                        release.release()
                return TranscriptWithCompanions(
                    transcript, {"control.yaml": control.transcript}
                )


if __name__ == "__main__":
    run_this_suite(__file__)
