#!/usr/bin/env -S uv run --script

import os
import re
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.assertions import last_tool_text
from support.previews import (
    compact_previews,
    assert_preview,
    cell_text,
    normalize_preview_paths,
    session_directory,
)
from support.client import McpClient
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import (
    host_process_id,
    process_exists,
    stop_process,
    stop_process_id,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, requires
from support.suites import run_this_suite

LARGE_OUTPUT_SIZE = 2 * 1024 * 1024

from boundaries.client_server._harness import (
    ZodFixtureControl,
    wait_for_marker,
)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
def test_restart_closes_worker_stdin(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        closing(
            FifoCheckpoint.create(Path(temporary_directory) / "cell-output-closed")
        ) as output_closed,
    ):
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment[LOADER_VARIABLE] = str(
            build_interposer(temporary_path, "cell_output_close_interposer")
        )
        environment["MCP_CONSOLE_TEST_CELL_OUTPUT_CLOSED"] = str(output_closed.path)
        environment["MCP_CONSOLE_TEST_CELL_OUTPUT_SUFFIX"] = (
            "/capture/sessions/001/cell-001.txt"
        )
        environment["ZOD_STDIN_CLOSE_RELEASE"] = str(output_closed.path)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        # Finish discovery and lazy startup before exercising worker retirement.
        client.send(requirements={"action": "get"})
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"
        client.send(r="wait for stdin close", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        wait_for_marker(
            temporary_path,
            "zod-waiting-for-stdin-close",
            client,
        )
        wait_for_path(
            session_directory(client) / "capture/sessions/001/cell-001.txt",
            "server observed the executing cell before restart",
            client=client,
        )

        client.send(control="restart")
        output = last_tool_text(client)
        raw = cell_text(client, 3)
        assert raw == "", "output after the restart cut does not belong to the cell log"
        assert "capture/sessions/001/cell-001-post.txt" in output, output
        recorded = (
            session_directory(client) / "capture/sessions/001/cell-001-post.txt"
        ).read_text()
        prefix = "zod stdin closed\n" + "x" * LARGE_OUTPUT_SIZE
        observed = len(recorded)
        assert observed > len(prefix), observed
        raw = prefix + "y" * (observed - len(prefix))
        assert recorded == raw
        suffix = "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        suffix = "[active evaluation stopped by session restart request]\n" + suffix
        assert output.endswith(suffix), "lifecycle notices followed old-worker output"
        assert_preview(output.removesuffix(suffix).removesuffix("\n"), raw)
        normalize_preview_paths(client)

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        compact_previews(client, "x", "y")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_starts_first_worker_and_waits_until_ready(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("block", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        environment["ZOD_REPORT_PID"] = "1"
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        worker_pid = None
        passed = False
        try:
            client.initialize_and_list_tools()
            restarted = client.start_send(control="restart")
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )
            worker_pid = host_process_id(
                int(
                    wait_for_marker(
                        temporary_path, "zod-worker-pid", client
                    ).read_text()
                ),
                client.process.pid,
            )

            while_restarting = client.start_send(r="echo echo")
            client.receive(while_restarting)
            result = while_restarting["result"]
            assert result["isError"] is True
            assert result["content"][0]["text"] == "[worker is restarting]"

            startup_release.touch()
            client.receive(restarted)
            assert restarted["result"]["content"][0]["text"] == (
                "[starting new worker]\n[idle]"
            )

            after_restart = client.start_send(r="echo echo")
            client.receive(after_restart)
            assert after_restart["result"]["content"][0]["text"] == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_id(worker_pid)
                stop_process(client.process)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_commits_lifecycle_before_replacement_callbacks(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"

        startup_control.write_text("ready with callback", encoding="utf-8")
        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )
        callback.unlink()

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        callback = wait_for_marker(
            temporary_path,
            "zod-startup-callback-response",
            client,
        )
        assert callback.read_text(encoding="utf-8") == (
            "Python requirements are unavailable with a custom worker"
        )

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_discards_unread_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    client.send(stdin="stale\n")
    assert last_tool_text(client) == "\n[idle]"

    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    ), last_tool_text(client)

    client.send(r="input without request", stdin="fresh\n")
    assert last_tool_text(client) == "zod stdin: fresh\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retries_initial_startup_silently(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("fail", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="echo echo")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True
        assert result["content"][0]["text"] == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]"
        )
        startup_control.write_text("ready", encoding="utf-8")
        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_force_stops_stalled_worker(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_REPORT_PID"] = "1"
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        worker_pid = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # The stalled evaluation belongs to an established worker.
            client.send(requirements={"action": "get"})
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            client.send(r="stall", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            pid_marker = wait_for_marker(
                temporary_path,
                "zod-worker-pid",
                client,
            )
            worker_pid = host_process_id(
                int(pid_marker.read_text()), client.process.pid
            )
            wait_for_marker(temporary_path, "zod-stalled", client)

            restart_call = client.start_send(control="restart")
            client.receive(restart_call)
            assert not process_exists(worker_pid), "old worker survived restart"
            assert last_tool_text(client) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_id(worker_pid)
                stop_process(client.process)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_shuts_down_stalled_worker(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ZodFixtureControl(Path(temporary_directory)) as control,
    ):
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        control.configure(environment)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        worker_pid = None
        passed = False
        try:
            client.initialize_and_list_tools()
            operation = client._next_request_id
            stalled = client.start_send(
                r=f"stall: {operation}",
                stdin="x" * (2 * 1024 * 1024),
            )
            stalled["send"]["r"] = "stall"
            stalled["send"]["stdin"] = "<large stdin>"
            control.connect(client)
            event = control.wait_for(operation, "parent_operation_stalled")
            worker_pid = host_process_id(event["pid"], client.process.pid)
            assert isinstance(worker_pid, int) and worker_pid > 0, event
            client.stdin.close()
            try:
                return_code = client.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                raise AssertionError(
                    "mcp-console did not stop its stalled worker; "
                    + control.diagnostics()
                ) from None

            assert return_code == 0, client.stderr.read()
            client.stdout.read()
            assert client.stderr.read() == ""
            assert not process_exists(worker_pid), "Zod outlived mcp-console"
            passed = True
            return client.transcript
        finally:
            if not passed:
                stop_process_id(worker_pid)
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
