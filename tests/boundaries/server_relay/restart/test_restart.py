#!/usr/bin/env -S uv run --script

import select
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CAPTURE_NAME,
    RESTART_REQUIREMENTS_CHECK_NAME,
    RESTART_REQUIREMENTS_CHECKED_NAME,
    RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME,
    RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME,
    RESTART_REQUIREMENTS_RESOLVED_NAME,
    ServerRelayClient,
    _normalize_shutdown_grace,
    _receive_checkpointed,
)
from support.progress import without_elapsed
from support.assertions import tool_text as _tool_text
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.resolvers import fake_ir_environment as _fake_ir_environment
from support.suites import run_this_suite
from support.requirements import POSIX, requires


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_restart_routes_stdin_and_cell_to_replacement(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(binary, "controlled_restart_stdin", execution=execution)
    finished = False
    old_capture = None
    try:
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")

        assert _tool_text(client.send(stdin="stale\n")) == "\n[idle]"
        result = client.send(
            control="restart",
            stdin="fresh\n",
            r="replacement cell",
        )
        output = _tool_text(result)
        stopped = output.index("[worker stopped: in-memory state lost]")
        starting = output.index("[starting new worker]")
        consumed = "replacement cell consumed stdin: fresh\n"
        evaluated = output.index(consumed)
        assert stopped < starting < evaluated, output
        assert output.count(consumed) == 1, output

        old_transcript = client._read_open_capture(old_capture)
        replacement_transcript = client.finish_active()
        finished = True
    finally:
        if old_capture is not None:
            old_capture.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    old_commands = [
        entry["server"] for entry in old_transcript if entry.keys() == {"server"}
    ]
    assert old_commands[0] == {"kind": "stdin", "data": "stale\n"}, old_commands
    assert len(old_commands) == 2 and old_commands[1]["kind"] == "shutdown", (
        old_commands
    )
    replacement_commands = [
        entry["server"]
        for entry in replacement_transcript
        if entry.keys() == {"server"}
    ]
    assert replacement_commands == [
        {"kind": "stdin", "data": "fresh\n"},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement cell",
        },
    ], replacement_commands
    assert len(_normalize_shutdown_grace(old_transcript)) == 1
    return old_transcript + replacement_transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_restart_with_requirements_and_stdin_only_reports_replacement_idle(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "restart-stdin-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary, "controlled_restart_stdin_only", environment, execution=execution
        )
        result = client.send(
            control="restart",
            requirements={"r": ["restart-stdin-requirement"]},
            stdin="replacement input\n",
        )
        assert _tool_text(result) == "[starting new worker]\n[idle]", result
        transcript = client.finish_active()
        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [{"kind": "stdin", "data": "replacement input\n"}], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_restart_resolves_requirements_before_replacement_and_timeout(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "restart-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        resolver_finished = FifoCheckpoint.create(root / "resolver-finished")
        resolver_finish_release = FifoCheckpoint.create(
            root / "resolver-finish-release"
        )
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_FINISHED"] = str(resolver_finished.path)
        environment["MCP_CONSOLE_TEST_IR_FINISH_RELEASE"] = str(
            resolver_finish_release.path
        )
        client = ServerRelayClient(
            binary, "controlled_restart_requirements", environment, execution=execution
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        requirement_check = FifoCheckpoint.attach(
            old_root / RESTART_REQUIREMENTS_CHECK_NAME
        )
        requirement_checked = FifoCheckpoint.attach(
            old_root / RESTART_REQUIREMENTS_CHECKED_NAME
        )
        requirement_resolved = FifoCheckpoint.attach(
            old_root / RESTART_REQUIREMENTS_RESOLVED_NAME
        )
        resolver_released = False
        requirement_resolution_reported = False
        resolver_finish_released = False
        replacement_evaluation_received = None
        replacement_evaluation_release = None
        replacement_evaluation_released = False
        finished = False
        try:
            evaluation = client.client.start_send(
                control="restart",
                r="replacement requirement cell",
                requirements={"r": ["restart-requirement"]},
                stdin="replacement requirement input\n",
                timeout_ms=1_000,
            )
            resolver_started.wait()
            requirement_check.release()
            requirement_checked.wait()

            readable, _, _ = select.select([client.client.stdout], [], [], 1.25)
            assert not readable, (
                "send timeout applied while restart requirements were resolving"
            )

            resolver_release.release()
            resolver_released = True
            resolver_finished.wait()
            requirement_resolved.release()
            requirement_resolution_reported = True
            resolver_finish_release.release()
            resolver_finish_released = True
            evaluation_received_path = client._wait_for(
                RESTART_REQUIREMENTS_EVALUATION_RECEIVED_NAME
            )
            replacement_evaluation_received = FifoCheckpoint.attach(
                evaluation_received_path
            )
            replacement_evaluation_release = FifoCheckpoint.attach(
                evaluation_received_path.with_name(
                    RESTART_REQUIREMENTS_EVALUATION_RELEASE_NAME
                )
            )
            replacement_evaluation_received.wait()
            _receive_checkpointed(
                client.client,
                evaluation,
                "the controlled evaluation after restart requirement resolution",
            )
            assert without_elapsed(_tool_text(evaluation["result"])) == (
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n\n"
                "[running; poll with an empty send]"
            ), evaluation
            replacement_evaluation_release.release()
            replacement_evaluation_released = True
            assert (
                _tool_text(client.send()) == "replacement requirement cell ran\n[done]"
            )
            old_transcript = client._read_open_capture(old_capture)
            replacement_transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
            if not requirement_resolution_reported:
                requirement_resolved.release()
            if not resolver_finish_released:
                resolver_finish_release.release()
            if (
                replacement_evaluation_release is not None
                and not replacement_evaluation_released
            ):
                replacement_evaluation_release.release()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            old_capture.close()
            requirement_check.close()
            requirement_checked.close()
            requirement_resolved.close()
            resolver_started.close()
            resolver_release.close()
            resolver_finished.close()
            resolver_finish_release.close()
            if replacement_evaluation_received is not None:
                replacement_evaluation_received.close()
            if replacement_evaluation_release is not None:
                replacement_evaluation_release.close()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    old_commands = [
        entry["server"] for entry in old_transcript if entry.keys() == {"server"}
    ]
    assert len(old_commands) == 1 and old_commands[0]["kind"] == "shutdown", (
        old_commands
    )
    replacement_commands = [
        entry["server"]
        for entry in replacement_transcript
        if entry.keys() == {"server"}
    ]
    assert replacement_commands == [
        {"kind": "stdin", "data": "replacement requirement input\n"},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "replacement requirement cell",
        },
    ], replacement_commands
    assert not any(
        command["kind"] == "prepare_r"
        for command in old_commands + replacement_commands
    )
    assert len(_normalize_shutdown_grace(old_transcript)) == 1
    return old_transcript + replacement_transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_restart_requirement_failure_preserves_old_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "unused-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        environment["MCP_CONSOLE_TEST_IR_FAILURE"] = (
            "synthetic controlled restart requirement failure"
        )
        client = ServerRelayClient(binary, "ready", environment, execution=execution)
        client.start_worker()
        old_capture = (client.relay_root() / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            result = client.send(
                control="restart",
                stdin="must not send\n",
                r="must not evaluate",
                requirements={"r": ["failing-restart-requirement"]},
            )
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "[R package resolution failed with exit status: 1: "
                            "synthetic controlled restart requirement failure]"
                        ),
                    }
                ],
                "isError": True,
            }, result
            assert _tool_text(client.send()) == "\n[idle]"

            before_cleanup = client._read_open_capture(old_capture)
            assert not any(entry.keys() == {"server"} for entry in before_cleanup), (
                before_cleanup
            )
            transcript = client.finish_active()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            old_capture.close()

    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert server_commands == [], server_commands
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
