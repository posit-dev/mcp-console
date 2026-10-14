#!/usr/bin/env -S uv run --script

import os
import select
import signal
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed, without_elapsed_result
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    capture_process_identity,
    host_process_id,
    live_processes,
    stop_process,
    stop_process_group,
)
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    R,
    command,
    requires,
)
from support.normalization import code
from support.resolvers import resolver_interrupt_permission_environment
from support.suites import run_this_suite

FIXTURE_CHECKPOINT_TIMEOUT_SECONDS = 15

from boundaries.client_server._harness import (
    ZodFixtureControl,
    continue_stopped_worker,
    stop_recorded_worker,
    wait_for_path,
    wait_for_process_group_exit,
    wait_for_stopped_worker,
    wait_for_worker_retirement,
)


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_interrupt_with_requirements_in_mixed_managed_session(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "r.packages=[DBI]", "-c", "python.managed.packages=[]"),
    ) as client:
        client.initialize_and_list_tools()
        inspected = client.send(requirements={"action": "get"})["structuredContent"]
        assert inspected["requirements"]["r"] == ["DBI"], inspected
        client.expect("[1] 0\n", r="print(0)")
        client.expect("0\n", python="0")
        client.expect(
            "\n[output produced while idle]\n[1] 42\n[done]",
            control="interrupt",
            requirements={"r": ["DBI"]},
            r="print(42)",
        )
        # The public input request proves that the old R evaluation is active.
        wait_for_evaluation_output(
            client,
            '[input requested: "old> "]\n[waiting for stdin]',
            "interruptible R input",
            # fmt: r
            r=code("""
                tryCatch(
                  {
                    readline("old> ")
                    old_cell_completed <- TRUE
                  },
                  interrupt = function(e) cat("interrupted\\n")
                )
                """),
        )
        interrupted = client.send(
            control="interrupt",
            requirements={"r": ["DBI"]},
            r='stopifnot(!exists("old_cell_completed")); print(43)',
        )
        assert not interrupted.get("isError"), interrupted
        output = last_tool_text(client)
        assert output == "interrupted\n[1] 43\n[done]", repr(output)
        client.expect("42\n", python="40 + 2")
        return client.finish()


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_interrupt_precedes_follow_up_requirement_policy(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    transcripts = {}
    for policy in ("startup_only", "disabled"):
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
                "-c",
                f"r.resolution={policy}",
                "-c",
                "python.managed.packages=[]",
            ),
        ) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", r='cat("ready\\n")')
            wait_for_evaluation_output(
                client,
                '[input requested: "old> "]\n[waiting for stdin]',
                "interruptible R input",
                # fmt: r
                r=code("""
                    tryCatch(
                      readline("old> "),
                      interrupt = function(e) cat("interrupted\\n")
                    )
                    """),
            )
            result = client.send(
                control="interrupt",
                requirements={"r": ["DBI"]},
                r="follow_up_ran <- TRUE",
            )
            assert result.get("isError"), result
            output = result["content"][0]["text"]
            assert output.startswith("interrupted\n"), result
            assert f"r.resolution={policy}" in output, result
            client.expect(
                "worker retained\n",
                r='stopifnot(!exists("follow_up_ran")); cat("worker retained\\n")',
            )
            transcripts[policy] = client.finish()
    return TranscriptWithCompanions(
        transcripts["startup_only"],
        {"disabled.yaml": McpTranscript(transcripts["disabled"])},
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_running_worker_with_sigint(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with ZodFixtureControl() as control:
        control.configure(environment)
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        finished = False
        try:
            client.initialize_and_list_tools()

            # Establish a ready worker before observing the interruptible cell.
            ready_id = client._next_request_id
            client.send(r=f"checkpoint {ready_id}")
            assert last_tool_text(client) == "[done]"
            control.connect(client)
            control.wait_for(ready_id, "worker_operation_completed")

            target_id = client._next_request_id
            client.send(
                r=f"wait for interrupt: {target_id}",
                timeout_ms=0,
            )
            assert client.transcript[-1]["id"] == target_id
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            control.wait_for(target_id, "worker_operation_started")

            interrupt_id = client._next_request_id
            client.send(control="interrupt", timeout_ms=3_000)
            assert client.transcript[-1]["id"] == interrupt_id
            assert last_tool_text(client) == "zod interrupted\n"
            observed = control.wait_for(target_id, "worker_interrupt_observed")
            assert observed["signal"] == signal.SIGINT, observed
            control.wait_for(target_id, "worker_operation_completed")
            control.record_client_event(
                interrupt_id,
                "interrupt_response_received",
                target_operation=target_id,
            )
            control.assert_before(
                (target_id, "worker_interrupt_observed"),
                (interrupt_id, "interrupt_response_received"),
            )

            checkpoint_id = client._next_request_id
            client.send(r=f"checkpoint {checkpoint_id}")
            assert client.transcript[-1]["id"] == checkpoint_id
            assert last_tool_text(client) == "[done]"
            control.wait_for(checkpoint_id, "worker_operation_completed")
            control.assert_before(
                (target_id, "worker_operation_completed"),
                (checkpoint_id, "worker_operation_started"),
            )

            transcript = client.finish()
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_supervises_stopped_and_continued_workers(
    binary: Path, execution: Execution
) -> Transcript:
    wrapper = Path(__file__).resolve().parents[3] / "fixtures" / "stop_continue_zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_control.write_text("stop evaluation", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        client = McpClient(
            binary,
            execution.serve("--worker", str(wrapper)),
            environment,
        )
        workers: list[tuple[int, int]] = []
        passed = False
        try:
            client.initialize_and_list_tools()
            evaluation = client.start_send(r="echo echo")
            marker, worker_pid, worker_group = wait_for_stopped_worker(
                temporary_path,
                workers,
                client,
                execution,
            )

            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            readable, _, _ = select.select(
                [client.stdout],
                [],
                [],
                FIXTURE_CHECKPOINT_TIMEOUT_SECONDS,
            )
            assert readable, "relay supervision did not answer the interrupt request"
            client.receive(interrupt)
            assert without_elapsed_result(interrupt["result"]) == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, interrupt

            continue_stopped_worker(worker_pid, worker_group)
            wait_for_path(
                marker.with_name("zod-stop-continue-resumed"),
                "stopped worker to resume",
                client,
            )
            client.receive(evaluation)
            assert evaluation["result"] == {
                "content": [{"type": "text", "text": "zod: echo\n"}],
                "isError": False,
            }, evaluation

            startup_control.write_text("stop startup", encoding="utf-8")
            restarted = client.start_send(control="restart")
            replacement_marker, replacement_pid, replacement_group = (
                wait_for_stopped_worker(
                    temporary_path,
                    workers,
                    client,
                    execution,
                )
            )
            assert replacement_pid != worker_pid, (
                "replacement reused the retiring worker"
            )
            if execution == SANDBOXED:
                assert replacement_group != worker_group, (
                    "replacement reused the retiring process group"
                )
            wait_for_worker_retirement(worker_pid, worker_group, client, execution)

            continue_stopped_worker(replacement_pid, replacement_group)
            wait_for_path(
                replacement_marker.with_name("zod-stop-continue-resumed"),
                "replacement worker to resume",
                client,
            )
            client.receive(restarted)
            assert restarted["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[worker stopped: in-memory state lost]\n"
                            "[starting new worker]\n"
                            "[idle]"
                        ),
                    }
                ],
                "isError": False,
            }, restarted

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                for recorded_pid, recorded_group in reversed(workers):
                    stop_recorded_worker(recorded_pid, recorded_group, execution)
                stop_process(client.process)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
def test_reports_resolver_interrupt_permission_error(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        (
            environment,
            resolver_started,
            resolver_lifetime,
            resolver_group_record,
            denied_interrupt,
            resolver_watches,
        ) = resolver_interrupt_permission_environment(temporary_path, execution)

        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod)),
            environment,
            temporary_path,
        )
        resolver_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            preparation = client.start_send(
                requirements={"r": ["blocked-resolver"]},
            )
            resolver_started.wait("permission-denied R resolver")
            namespace_group = int(resolver_group_record.read_text(encoding="utf-8"))
            resolver_group = host_process_id(namespace_group, client.process.pid)
            resolver_identity = capture_process_identity(resolver_group)
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )
            wait_for_path(
                resolver_watches / str(namespace_group),
                "active resolver supervision",
                client,
            )

            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client.receive_many([preparation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(denied_interrupt.read_text(encoding="utf-8"))
            assert denied_group == namespace_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not live_processes([resolver_identity]), (
                "resolver survived its failed interrupt"
            )
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            expected = {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "failed to interrupt R package resolver `ir`: "
                            "Operation not permitted (os error 1)"
                        ),
                    }
                ],
                "isError": True,
            }
            assert preparation["result"] == expected, preparation
            interrupt_expected = {
                "content": [
                    {
                        "type": "text",
                        "text": f"[{expected['content'][0]['text']}]",
                    }
                ],
                "isError": True,
            }
            assert interrupt["result"] == interrupt_expected, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)
            resolver_started.close()
            resolver_lifetime.close()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
def test_reports_runtime_r_resolver_interrupt_permission_error(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        (
            environment,
            resolver_started,
            resolver_lifetime,
            resolver_group_record,
            denied_interrupt,
            resolver_watches,
        ) = resolver_interrupt_permission_environment(temporary_path, execution)
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host", "--worker", str(zod)),
            environment,
            temporary_path,
        )
        resolver_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # Complete lazy custom-worker startup before timing resolver arrival.
            client.expect("zod: ready\n", r="echo ready")
            evaluation = client.start_send(
                r="report runtime R resolution failure",
            )
            try:
                resolver_started.wait("permission-denied runtime R resolver")
            except AssertionError:
                client.receive(evaluation)
                raise AssertionError(evaluation) from None
            namespace_group = int(resolver_group_record.read_text(encoding="utf-8"))
            resolver_group = host_process_id(namespace_group, client.process.pid)
            resolver_identity = capture_process_identity(resolver_group)
            assert resolver_group != os.getpgrp(), (
                "resolver did not enter a dedicated process group"
            )
            wait_for_path(
                resolver_watches / str(namespace_group),
                "active resolver supervision",
                client,
            )

            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            responses_returned = threading.Event()
            forced_stop = threading.Event()

            def stop_if_calls_block() -> None:
                if not responses_returned.wait(2):
                    forced_stop.set()
                    stop_process_group(resolver_group)

            watchdog = threading.Thread(target=stop_if_calls_block, daemon=True)
            watchdog.start()
            try:
                client.receive_many([evaluation, interrupt])
            finally:
                responses_returned.set()
                watchdog.join()

            denied_group = int(denied_interrupt.read_text(encoding="utf-8"))
            assert denied_group == namespace_group, (
                "SIGINT denial targeted a different process group"
            )
            wait_for_process_group_exit(resolver_group, client)
            assert not live_processes([resolver_identity]), (
                "resolver survived its failed interrupt"
            )
            assert not forced_stop.is_set(), (
                "resolver interrupt failure did not terminate both calls"
            )

            message = (
                "failed to interrupt R package resolver `ir`: "
                "Operation not permitted (os error 1)"
            )
            assert evaluation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": f"zod R resolution failure: host: {message}\n",
                    }
                ],
                "isError": False,
            }, evaluation
            assert interrupt["result"] == {
                "content": [{"type": "text", "text": f"[{message}]"}],
                "isError": True,
            }, interrupt

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(resolver_group)
                stop_client(client)
            resolver_started.close()
            resolver_lifetime.close()


if __name__ == "__main__":
    run_this_suite(__file__)
