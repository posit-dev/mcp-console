#!/usr/bin/env -S uv run --script

import select
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CAPTURE_NAME,
    CONTROLLED_COMPLETION_RELEASE_NAME,
    CONTROLLED_COMPLETION_SENT_NAME,
    EVALUATING_NAME,
    INTERRUPT_ACK_RELEASE_NAME,
    INTERRUPT_ACKNOWLEDGED_NAME,
    INTERRUPT_ACTIVE_RELEASE_NAME,
    INTERRUPT_RECEIVED_NAME,
    POLL_STDIN_RECEIVED_NAME,
    PREPARATION_RECEIVED_NAME,
    PREPARATION_RESULT_RELEASE_NAME,
    PREPARATION_RESULT_SENT_NAME,
    ServerRelayClient,
    _ordered_input_barrier,
    _wait_for_recorded_tool_result,
)
from support.progress import without_elapsed, without_elapsed_result
from support.assertions import tool_text as _tool_text
from support.assertions import wait_for_worker_ready
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.requirements import POSIX, PROCESS_EVENTS, requires
from support.resolvers import fake_ir_environment as _fake_ir_environment
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_and_reports_result(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "interrupt", execution=execution)
    assert without_elapsed(_tool_text(client.send(r="42", timeout_ms=0))) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    assert _tool_text(client.send(control="interrupt", timeout_ms=0)) == "[done]"
    assert _tool_text(client.send()) == "\n[idle]"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupt_requirements_without_cell_is_rejected_before_signal(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(binary, "ready", execution=execution)
    client.start_worker()

    result = client.send(
        control="interrupt",
        requirements={"r": ["must-not-resolve"]},
    )
    assert result == {
        "content": [
            {
                "type": "text",
                "text": (
                    '`requirements` with `control = "interrupt"` requires a code cell'
                ),
            }
        ],
        "isError": True,
    }, result

    transcript = client.finish_active()
    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_control_only_interrupt_targets_blocked_controlled_restart_resolver(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "unused-interrupted-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        resolver_interrupted = FifoCheckpoint.create(root / "resolver-interrupted")
        resolver_interrupt_release = FifoCheckpoint.create(
            root / "resolver-interrupt-release"
        )
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(resolver_interrupted.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPT_RELEASE"] = str(
            resolver_interrupt_release.path
        )
        client = ServerRelayClient(binary, "ready", environment, execution=execution)
        client.start_worker()
        capture = (client.relay_root() / CAPTURE_NAME).open(encoding="utf-8")
        resolver_released = False
        resolver_interrupt_released = False
        finished = False
        try:
            controlled = client.client.start_send(
                control="restart",
                r="cell must not run after resolver interrupt",
                requirements={"r": ["blocked-controlled-restart"]},
            )
            resolver_started.wait()
            interrupt = client.client.start_send(
                control="interrupt",
                timeout_ms=0,
            )
            resolver_interrupted.wait()
            client.client.notify(
                "notifications/cancelled",
                requestId=interrupt["id"],
                reason="acceptance test cancelled the interrupt",
            )
            _ordered_input_barrier(client.client)
            recorded = _wait_for_recorded_tool_result(client.client, interrupt)
            assert without_elapsed_result(recorded) == {
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
            }, recorded
            assert interrupt.keys() == {"id", "send"}, interrupt

            resolver_interrupt_release.release()
            resolver_interrupt_released = True
            client.client.receive(controlled)

            result = controlled["result"]
            assert result.get("isError") is True, result
            error = "".join(
                content["text"]
                for content in result["content"]
                if content["type"] == "text"
            )
            assert "R package resolution failed with exit status: 130" in error, error
            assert _tool_text(client.send()) == "\n[idle]"

            before_cleanup = client._read_open_capture(capture)
            assert not any(entry.keys() == {"server"} for entry in before_cleanup), (
                before_cleanup
            )
            transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_interrupt_released:
                resolver_interrupt_release.release()
            if not resolver_released:
                resolver_release.release()
                resolver_released = True
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            capture.close()
            resolver_started.close()
            resolver_release.close()
            resolver_interrupted.close()
            resolver_interrupt_release.close()

        assert not (root / "ir-counter").exists()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_control_only_interrupt_preserves_controlled_completion_marker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_completion_then_interrupt", execution=execution
    )
    # MCP initialization does not await configuration. A zero-timeout control
    # requires it, and this scenario tests completion after that startup phase.
    wait_for_worker_ready(client.client, "configuration before controlled restart")
    result = client.send(
        control="restart",
        r="controlled cell completed before later interrupt",
        timeout_ms=0,
    )
    assert _tool_text(result).endswith("[running; poll with an empty send]"), result

    relay_root = client.relay_root()
    completion_release = FifoCheckpoint.attach(
        relay_root / CONTROLLED_COMPLETION_RELEASE_NAME
    )
    completion_sent = FifoCheckpoint.attach(
        relay_root / CONTROLLED_COMPLETION_SENT_NAME
    )
    finished = False
    released = False
    try:
        completion_release.release()
        released = True
        completion_sent.wait()

        result = client.send(control="interrupt", timeout_ms=3_000)
        assert _tool_text(result) == (
            "controlled cell completed before later interrupt\n[done]"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            completion_release.release()
        completion_release.close()
        completion_sent.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands[0] == {
        "kind": "evaluate",
        "language": "r",
        "source": "controlled cell completed before later interrupt",
    }, commands
    assert commands[1] == {"kind": "interrupt", "request_id": 0}, commands
    assert len(commands) == 2, commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_orders_stdin_before_new_evaluation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_stdin_evaluate", execution=execution
    )
    client.send(r="old evaluation", timeout_ms=0)
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)

    result = client.send(
        control="interrupt",
        stdin="finish old\n",
        r="new evaluation",
        timeout_ms=50,
    )
    output = _tool_text(result)
    if output.endswith("[running; poll with an empty send]"):
        output += _tool_text(client.send())
    old = output.index("old evaluation finished from stdin\n")
    new = output.index("new evaluation ran\n")
    assert old < new, output
    assert output.count("old evaluation finished from stdin\n") == 1, output
    assert output.count("new evaluation ran\n") == 1, output
    assert output.endswith("[done]"), output

    transcript = client.finish_active()
    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old\n"},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "new evaluation",
        },
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_orders_stdin_preparation_and_new_evaluation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "interrupt-success-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "controlled_interrupt_stdin_requirements_evaluate",
            environment,
            execution=execution,
        )
        client.send(
            r="old evaluation before successful requirements",
            timeout_ms=0,
        )
        assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
            "\n[running; poll with an empty send]"
        )
        client._wait_for(EVALUATING_NAME)

        result = client.send(
            control="interrupt",
            stdin="finish old before successful preparation\n",
            r="new evaluation after successful preparation",
            requirements={"r": ["successful-after-interrupt"]},
        )
        assert _tool_text(result) == (
            "old evaluation settled before successful preparation\n"
            "new evaluation ran after successful preparation\n"
            "[done]"
        )
        transcript = client.finish_active()
        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before successful requirements",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old before successful preparation\n"},
        {"kind": "prepare_r", "library": str(library)},
        {
            "kind": "evaluate",
            "language": "r",
            "source": "new evaluation after successful preparation",
        },
    ], commands
    commands[-2]["library"] = "<interrupt-success-candidate>"
    prepared = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared == [{"kind": "r_prepared", "library": str(library)}]
    prepared[0]["library"] = "<interrupt-success-candidate>"
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_stdin_precedes_failing_requirements_without_new_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "interrupt-failure-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "controlled_interrupt_stdin_requirement_failure",
            environment,
            execution=execution,
        )
        client.send(
            r="old evaluation before failing requirements",
            timeout_ms=0,
        )
        assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
            "\n[running; poll with an empty send]"
        )
        client._wait_for(EVALUATING_NAME)

        result = client.send(
            control="interrupt",
            stdin="finish old before preparation\n",
            r="new evaluation must not run after preparation failure",
            requirements={"r": ["failing-after-interrupt"]},
        )
        assert result.get("isError") is True, result
        text = "".join(
            content["text"]
            for content in result["content"]
            if content["type"] == "text"
        )
        prior = text.index("old evaluation finished before preparation\n")
        failure = text.index(
            "scripted controlled interrupt R preparation failed; further "
            "requirement changes are unavailable until session restart"
        )
        assert prior < failure, text

        transcript = client.finish_active()
        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before failing requirements",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old before preparation\n"},
        {"kind": "prepare_r", "library": str(library)},
    ], commands
    commands[-1]["library"] = "<interrupt-failure-candidate>"
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_stdin_precedes_invalid_requirements_without_new_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_stdin_invalid_requirements", execution=execution
    )
    client.send(
        r="old evaluation before invalid requirements",
        timeout_ms=0,
    )
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)

    result = client.send(
        control="interrupt",
        stdin="finish old before validation\n",
        r="new evaluation must not run after validation failure",
        requirements={},
    )
    assert result.get("isError") is True, result
    text = "".join(
        content["text"] for content in result["content"] if content["type"] == "text"
    )
    prior = text.index("old evaluation finished before validation\n")
    validation = text.index(
        "at least one of `requirements.r`, `requirements.python`, or "
        "`requirements.duckdb` is required"
    )
    assert prior < validation, text

    transcript = client.finish_active()
    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation before invalid requirements",
        },
        {"kind": "interrupt", "request_id": 0},
        {"kind": "stdin", "data": "finish old before validation\n"},
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_does_not_run_cell_while_evaluation_remains_active(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_still_active", execution=execution
    )
    client.send(r="old evaluation", timeout_ms=0)
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    release = FifoCheckpoint.attach(client.relay_root() / INTERRUPT_ACTIVE_RELEASE_NAME)
    finished = False
    released = False
    try:
        result = client.send(
            control="interrupt",
            r="new evaluation must not run",
            timeout_ms=1_000,
        )
        assert result.get("isError") is True, result
        text = "".join(
            content["text"]
            for content in result["content"]
            if content["type"] == "text"
        )
        assert "old evaluation remains active\n" in text, text
        assert "interrupted evaluation is still active" in text, text
        assert "cell was not run" in text, text

        release.release()
        released = True
        assert _tool_text(client.send(timeout_ms=3_000)) == (
            "old evaluation eventually finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            release.release()
        release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_control_only_interrupt_timeout_zero_returns_after_grace_then_poll_collects(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_still_active", execution=execution
    )
    client.send(r="old evaluation", timeout_ms=0)
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    release = FifoCheckpoint.attach(client.relay_root() / INTERRUPT_ACTIVE_RELEASE_NAME)
    finished = False
    released = False
    try:
        result = client.send(control="interrupt", timeout_ms=0)
        assert without_elapsed(_tool_text(result)) == (
            "old evaluation remains active\n\n[running; poll with an empty send]"
        ), result

        release.release()
        released = True
        assert _tool_text(client.send(timeout_ms=3_000)) == (
            "old evaluation eventually finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            release.release()
        release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_control_only_interrupt_honors_timeout_after_attachment(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_still_active", execution=execution
    )
    client.send(r="old evaluation", timeout_ms=0)
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    relay_root = client.relay_root()
    acknowledged = FifoCheckpoint.attach(relay_root / INTERRUPT_ACKNOWLEDGED_NAME)
    release = FifoCheckpoint.attach(relay_root / INTERRUPT_ACTIVE_RELEASE_NAME)
    finished = False
    released = False
    try:
        controlled = client.client.start_send(
            control="interrupt",
            timeout_ms=5_000,
        )
        acknowledged.wait()

        release.release()
        released = True
        client.client.receive(controlled)
        assert _tool_text(controlled["result"]) == (
            "old evaluation remains active\nold evaluation eventually finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not released:
            release.release()
        acknowledged.close()
        release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "old evaluation",
        },
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_controlled_interrupt_does_not_wait_for_an_existing_poll(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "controlled_interrupt_with_waiting_poll", execution=execution
    )
    client.send(r="waiter-owned evaluation", timeout_ms=0)
    assert without_elapsed(_tool_text(client.client.transcript[-1]["result"])) == (
        "\n[running; poll with an empty send]"
    )
    client._wait_for(EVALUATING_NAME)
    relay_root = client.relay_root()
    poll_stdin_received = FifoCheckpoint.attach(relay_root / POLL_STDIN_RECEIVED_NAME)
    interrupt_received = FifoCheckpoint.attach(relay_root / INTERRUPT_RECEIVED_NAME)
    interrupt_ack_release = FifoCheckpoint.attach(
        relay_root / INTERRUPT_ACK_RELEASE_NAME
    )
    evaluation_release = FifoCheckpoint.attach(
        relay_root / INTERRUPT_ACTIVE_RELEASE_NAME
    )
    interrupt_ack_released = False
    evaluation_released = False
    finished = False
    try:
        waiting = client.client.start_send(
            stdin="poll ownership checkpoint\n", timeout_ms=5_000
        )
        # Stdin reaches the relay only after this send claims the evaluation.
        poll_stdin_received.wait()
        ownership = client.send(timeout_ms=0)
        assert ownership == {
            "content": [
                {
                    "type": "text",
                    "text": "[worker evaluation is already being polled]",
                }
            ],
            "isError": True,
        }, ownership
        controlled = client.client.start_send(
            control="interrupt",
            r="new evaluation must not run",
        )
        interrupt_received.wait()

        interrupt_ack_release.release()
        interrupt_ack_released = True
        client.client.receive(controlled)
        assert controlled["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "interrupted evaluation is still active; cell was not run"
                    ),
                }
            ],
            "isError": True,
        }, controlled
        assert "result" not in waiting, waiting

        evaluation_release.release()
        evaluation_released = True
        client.client.receive(waiting)
        assert _tool_text(waiting["result"]) == (
            "output owned by original waiter\noriginal waiter evaluation finished\n"
        )
        transcript = client.finish_active()
        finished = True
    finally:
        if not interrupt_ack_released:
            interrupt_ack_release.release()
        if not evaluation_released:
            evaluation_release.release()
        poll_stdin_received.close()
        interrupt_received.close()
        interrupt_ack_release.close()
        evaluation_release.close()
        if not finished:
            stop_client(client.client)
            client._temporary.cleanup()

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {
            "kind": "evaluate",
            "language": "r",
            "source": "waiter-owned evaluation",
        },
        {"kind": "stdin", "data": "poll ownership checkpoint\n"},
        {"kind": "interrupt", "request_id": 0},
    ], commands
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_cancelled_interrupt_during_live_preparation_does_not_recover_running(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "cancelled-interrupt-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "cancelled_interrupt_during_live_r_preparation",
            environment,
            execution=execution,
        )
        client.start_worker()
        relay_root = client.relay_root()
        preparation_received = FifoCheckpoint.attach(
            relay_root / PREPARATION_RECEIVED_NAME
        )
        preparation_release = FifoCheckpoint.attach(
            relay_root / PREPARATION_RESULT_RELEASE_NAME
        )
        preparation_sent = FifoCheckpoint.attach(
            relay_root / PREPARATION_RESULT_SENT_NAME
        )
        interrupt_received = FifoCheckpoint.attach(relay_root / INTERRUPT_RECEIVED_NAME)
        interrupt_ack_release = FifoCheckpoint.attach(
            relay_root / INTERRUPT_ACK_RELEASE_NAME
        )
        resolver_released = False
        preparation_released = False
        interrupt_ack_released = False
        finished = False
        try:
            preparation = client.client.start_send(
                requirements={"r": ["cancelled-interrupt"]},
            )
            # Cold resolver launch has the preparation budget. Only start the
            # short relay receipt wait after the resolver reaches its gate.
            readable, _, _ = select.select(
                [resolver_started.descriptor, client.client.stdout],
                [],
                [],
                max(0, client.client.response_deadline() - time.monotonic()),
            )
            assert readable, "R resolver did not reach its preparation gate"
            if client.client.stdout in readable:
                client.client.receive(preparation)
                raise AssertionError(preparation)
            resolver_started.wait(timeout=0)
            resolver_release.release()
            resolver_released = True
            preparation_received.wait()

            interrupt = client.client.start_send(
                control="interrupt",
                timeout_ms=0,
            )
            interrupt_received.wait()
            client.client.notify(
                "notifications/cancelled",
                requestId=interrupt["id"],
                reason="acceptance test cancelled the interrupt",
            )
            cancellation = client.client.transcript[-1]["input"]["params"]
            assert cancellation["requestId"] == interrupt["id"], cancellation
            cancellation["requestId"] = "<request ID>"

            _ordered_input_barrier(client.client)

            interrupt_ack_release.release()
            interrupt_ack_released = True
            recorded = _wait_for_recorded_tool_result(client.client, interrupt)
            assert without_elapsed_result(recorded) == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, recorded
            assert interrupt.keys() == {"id", "send"}, interrupt

            preparation_release.release()
            preparation_released = True
            preparation_sent.wait()
            client.client.receive(preparation)
            assert _tool_text(preparation["result"]) == "[prepared]"
            assert _tool_text(client.send()) == "\n[idle]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
            if not interrupt_ack_released:
                interrupt_ack_release.release()
            if not preparation_released:
                preparation_release.release()
            preparation_received.close()
            preparation_release.close()
            preparation_sent.close()
            interrupt_received.close()
            interrupt_ack_release.close()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    commands = [entry["server"] for entry in transcript if entry.keys() == {"server"}]
    assert commands == [
        {"kind": "prepare_r", "library": str(library)},
        {"kind": "interrupt", "request_id": 0},
    ], commands
    commands[0]["library"] = "<cancelled-interrupt-candidate>"
    prepared = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared == [{"kind": "r_prepared", "library": str(library)}], prepared
    prepared[0]["library"] = "<cancelled-interrupt-candidate>"
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
