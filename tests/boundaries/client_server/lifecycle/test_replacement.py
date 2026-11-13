#!/usr/bin/env -S uv run --script

import os
import select
import sys
import tempfile
import threading
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import execution_snapshots
from support.progress import without_elapsed, without_elapsed_result
from support.assertions import large_output, last_tool_text
from support.checkpoints import (
    FifoCheckpoint,
    wait_for_path,
)
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import (
    host_process_id,
    process_exists,
    stop_process,
)
from support.r import r_test_environment
from support.previews import (
    compact_previews,
    assert_preview,
    normalize_preview_paths,
    session_directory,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, requires
from support.resolvers import record_resolved_r_library
from support.suites import run_this_suite

FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)

from boundaries.client_server._harness import (
    expose_idle_sideband_output,
    wait_for_marker,
)


@executions(DIRECT, SANDBOXED)
def test_reports_missing_worker_launch_failure(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(
        binary,
        execution.serve("--worker", "/definitely/missing/mcp-console-worker"),
    )
    client.initialize_and_list_tools()

    client.send(r="complete silently")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    failure = result["content"][0]["text"]
    assert failure.removeprefix("[").startswith("failed to launch worker: "), failure
    result["content"][0]["text"] = "failed to launch worker: <missing executable>"

    transcript, standard_error = client.finish_with_standard_error()
    if standard_error:
        assert standard_error.strip() == failure.removeprefix("[").removesuffix("]")
    return transcript


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_reports_replacement_startup_failure_and_retry(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        startup_control = Path(temporary_directory) / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        record_resolved_r_library(environment, Path(temporary_directory))
        writable_root = (
            ("--writable-root", temporary_directory) if execution == SANDBOXED else ()
        )
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod), *writable_root),
            environment,
        )
        client.initialize_and_list_tools()

        client.send(r="complete silently")
        assert last_tool_text(client) == "[done]"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        failed = client.start_send(r="exit unexpectedly")
        failure_marker = wait_for_marker(
            Path(temporary_directory),
            "zod-replacement-startup-failing",
            client,
        )
        response_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_replacement_loops() -> None:
            if not response_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_replacement_loops, daemon=True)
        watchdog.start()
        try:
            client.receive(failed)
        finally:
            response_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "replacement startup retried automatically"
        assert failure_marker.is_file(), "startup checkpoint was lost during retirement"
        result = failed["result"]
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "zod replacement startup failed\n"
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker exited with status 86]"
                    ),
                }
            ],
            "isError": True,
        }, result

        startup_control.write_text("ready", encoding="utf-8")
        # Package preparation can consume one observation budget. Collect the
        # admitted cell's exact output without resubmitting it.
        client.expect(
            "[starting new worker]\nzod R requirement: prepared=true\n",
            r="report managed R requirement",
            requirements={"r": ["praise"]},
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_polls_replacement_startup_after_send_timeout(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = ""
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod)),
            environment,
        )
        forced_release = threading.Event()
        response_returned = threading.Event()
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(r="complete silently")
            assert last_tool_text(client) == "[done]"
            startup_control.write_text(
                "block",
                encoding="utf-8",
            )

            failed = client.start_send(r="exit unexpectedly", timeout_ms=1_000)
            wait_for_marker(
                temporary_path,
                "zod-replacement-waiting-ready",
                client,
            )

            def release_if_send_ignores_timeout() -> None:
                if not response_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
                    forced_release.set()
                    startup_release.touch()

            watchdog = threading.Thread(
                target=release_if_send_ignores_timeout,
                daemon=True,
            )
            watchdog.start()
            try:
                client.receive(failed)
            finally:
                response_returned.set()
                watchdog.join()
            assert not forced_release.is_set(), "send did not honor its startup timeout"
            assert failed["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]\n"
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[phase: replacement]\n"
                            "[worker starting]"
                        ),
                    }
                ],
                "isError": True,
            }, failed

            client.send(requirements={"python": ["py-yaml12"]})
            assert client.transcript[-1]["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, client.transcript[-1]

            combined = client.send(
                r="echo startup overlap cell ran",
                requirements={"r": ["praise"]},
            )
            assert combined == {
                "content": [
                    {
                        "type": "text",
                        "text": "[requirements not prepared: worker is starting]",
                    }
                ],
                "isError": True,
            }, combined
            assert not (temporary_path / "resolved-r-library").exists()

            startup_release.touch()
            client.send(session="default")
            assert last_tool_text(client) == "[idle]"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            startup_release.touch()
            if not passed:
                stop_process(client.process)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
def test_orders_explicit_restart_output(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        closing(
            FifoCheckpoint.create(Path(temporary_directory) / "cell-output-closed")
        ) as output_closed,
    ):
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("ready", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
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

        startup_control.write_text("ready", encoding="utf-8")
        client.send(control="restart")
        result = client.transcript[-1]["result"]
        assert result["isError"] is False, result
        suffix = (
            "\n[active evaluation stopped by session restart request]"
            "\n[worker stopped: in-memory state lost]"
            "\n[starting new worker]"
            "\n[idle]"
        )
        output = last_tool_text(client)
        assert output.endswith(suffix), output[-500:]
        assert_preview(output.removesuffix(suffix), large_output("zod stdin closed\n"))
        assert "capture/sessions/001/cell-001-post.txt" in output
        assert (
            session_directory(client) / "capture/sessions/001/cell-001-post.txt"
        ).read_text() == large_output("zod stdin closed\n")
        normalize_preview_paths(client)

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"
        compact_previews(client, "x", "y")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_controlled_restart_runs_cell_once_in_fresh_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

        client.send(r="set controlled restart state")
        assert last_tool_text(client) == "zod controlled state: old\n"
        old_worker = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-old-worker",
            client,
        )
        old_pid = host_process_id(
            int(old_worker.read_text(encoding="utf-8")), client.process.pid
        )

        client.send(
            control="restart",
            r="inspect controlled restart state",
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "zod controlled state: fresh; evaluation=1\n"
            "[done]"
        )

        evaluations = wait_for_marker(
            temporary_path,
            "zod-controlled-restart-cell-evaluations",
            client,
        )
        records = evaluations.read_text(encoding="utf-8").splitlines()
        assert len(records) == 1, records
        new_pid, state, count = records[0].split()
        assert host_process_id(int(new_pid), client.process.pid) != old_pid, records
        assert (state, count) == ("fresh", "1"), records
        assert not process_exists(old_pid), old_pid

        client.send(session="default")
        assert last_tool_text(client) == "\n[idle]"
        assert evaluations.read_text(encoding="utf-8").splitlines() == records
        return client.finish()


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_preserves_idle_worker_startup_failure(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("fail with stderr", encoding="utf-8")
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint.create(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint.create(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint.create(
            temporary_path / "resolver-interrupted"
        )

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["UV_TOOL_DIR"] = str(temporary_path)
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)

        writable_root = (
            ("--writable-root", temporary_directory) if execution == SANDBOXED else ()
        )
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod), *writable_root),
            environment,
        )
        finished = False
        try:
            client.initialize_and_list_tools()
            preparation = client.start_send(
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait("controlled interrupt R resolver")

            controlled = client.start_send(
                control="interrupt",
                stdin="unused input\n",
            )
            resolver_interrupted.wait("controlled interrupt signal delivery")
            client.receive_many([preparation, controlled])

            assert preparation["result"].get("isError") is True, preparation
            result = controlled["result"]
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "zod replacement startup failed\n"
                            "[worker sideband read failed: worker sideband closed]\n"
                            "[worker exited with status 86]"
                        ),
                    }
                ],
                "isError": True,
            }, result

            startup_control.write_text("ready", encoding="utf-8")
            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            finished = True
            return transcript
        finally:
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            if not finished:
                stop_client(client)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_control_only_interrupt_returns_while_explicit_preparation_settles(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    ordered_ir = (
        Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
    )
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        library = temporary_path / "resolved-library"
        library.mkdir()
        fake_bin = temporary_path / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ordered_ir)
        resolver_started = FifoCheckpoint.create(temporary_path / "resolver-started")
        resolver_release = FifoCheckpoint.create(temporary_path / "resolver-release")
        resolver_interrupted = FifoCheckpoint.create(
            temporary_path / "resolver-interrupted"
        )
        interrupt_release = FifoCheckpoint.create(temporary_path / "interrupt-release")

        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["UV_TOOL_DIR"] = str(temporary_path)
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(temporary_path / "ir-counter")
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )

        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod)),
            environment,
        )
        finished = False
        interrupt_waiting = False
        try:
            client.initialize_and_list_tools()

            def interrupt_preparation(
                preparation_arguments: dict[str, object],
                description: str,
            ) -> None:
                nonlocal interrupt_waiting
                preparation = client.start_send(**preparation_arguments)
                readable, _, _ = select.select(
                    [resolver_started.descriptor, client.stdout], [], [], 10
                )
                if client.stdout in readable:
                    client.receive(preparation)
                    raise AssertionError(
                        f"{description} returned before its checkpoint: {preparation}"
                    )
                resolver_started.wait(description, timeout=0, client=client)

                interrupt = client.start_send(
                    control="interrupt",
                    timeout_ms=0,
                )
                resolver_interrupted.wait(f"{description} interrupt", client=client)
                interrupt_waiting = True
                readable, _, _ = select.select([client.stdout], [], [], 3)
                assert client.stdout in readable, (
                    "control-only interrupt waited for explicit preparation to settle"
                )
                client.receive(interrupt)
                assert without_elapsed_result(interrupt["result"]) == {
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "\n[phase: dependency preparation]"
                                "\n[running; poll with an empty send]"
                            ),
                        }
                    ],
                    "isError": False,
                }, interrupt

                interrupt_release.release()
                interrupt_waiting = False
                client.receive(preparation)
                assert preparation["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "R package resolution failed with exit status: 130: ",
                        }
                    ],
                    "isError": True,
                }, preparation

            interrupt_preparation(
                {"requirements": {"r": ["blocked-standalone-resolver"]}},
                "standalone preparation resolver",
            )

            client.send(r="echo worker ready")
            assert last_tool_text(client) == "zod: worker ready\n"

            interrupt_preparation(
                {
                    "r": "echo interrupted preparation cell ran",
                    "requirements": {"r": ["blocked-cell-resolver"]},
                },
                "cell preparation resolver",
            )

            client.send(r="echo worker remains usable")
            assert last_tool_text(client) == "zod: worker remains usable\n"
            transcript = client.finish()
            finished = True
            return transcript
        finally:
            if interrupt_waiting:
                interrupt_release.release()
            resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            interrupt_release.close()
            if not finished:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_preserves_pending_sideband_output(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
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

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.send(control="restart")
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, result
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_preserves_unpolled_completion(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
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

        # This generation must be ready before testing its unpolled completion.
        client.send(requirements={"action": "get"})
        client.send(r="complete before restart checkpoint", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        ), last_tool_text(client)
        started = wait_for_marker(temporary_path, "zod-completion-started", client)
        (started.parent / "zod-release-completion").touch()
        wait_for_marker(
            temporary_path,
            "zod-completion-processed",
            client,
        )

        client.send(control="restart")
        restart_output = last_tool_text(client)
        assert restart_output == (
            "[done]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), restart_output
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_restart_interrupts_waiting_send(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        expose_idle_sideband_output(client, temporary_path)

        waiting = client.start_send(
            r="emit output and image before completion",
            timeout_ms=30_000,
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        restarted = client.start_send(control="restart")
        responses_returned = threading.Event()
        forced_stop = threading.Event()

        def stop_if_calls_block() -> None:
            if not responses_returned.wait(FIXTURE_CHECKPOINT_TIMEOUT_SECONDS):
                forced_stop.set()
                stop_process(client.process)

        watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
        watchdog.start()
        try:
            client.receive(waiting)
            client.receive(restarted)
        finally:
            responses_returned.set()
            watchdog.join()
        assert not forced_stop.is_set(), "restart did not release the waiting send"

        assert restarted["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                },
            ],
            "isError": False,
        }, restarted
        assert waiting["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "zod background sideband\n"
                        "[output produced while idle]\n"
                        "before pending image\n"
                    ),
                },
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "after pending image\n"
                        "[stopped by session restart request before evaluation finished]\n"
                        "[worker stopped: in-memory state lost]"
                    ),
                },
            ],
            "isError": True,
        }, waiting
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restarts_after_worker_exit(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.send(r="exit unexpectedly")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 86]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }
    client.send(stdin="replacement\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r="input without request")
    assert last_tool_text(client) == "zod stdin: replacement\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_unexpected_worker_exit_zero(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.send(r="exit zero")
    assert client.transcript[-1]["result"] == {
        "content": [
            {
                "type": "text",
                "text": (
                    "[worker sideband read failed: worker sideband closed]\n"
                    "[worker exited with status 0]\n"
                    "[worker stopped: in-memory state lost]\n"
                    "[starting new worker]\n"
                    "[idle]"
                ),
            }
        ],
        "isError": True,
    }

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
