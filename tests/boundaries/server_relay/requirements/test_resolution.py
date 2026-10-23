#!/usr/bin/env -S uv run --script

import base64
import select
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CAPTURE_NAME,
    CHECKPOINT_NAME,
    EXPLICIT_R_PREPARATION_CALLBACK_NAME,
    EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME,
    IDLE_R_EVALUATION_RECEIVED_NAME,
    IDLE_R_RESOLUTION_READY_NAME,
    IDLE_R_RESOLUTION_RELEASE_NAME,
    PREPARATION_RECEIVED_NAME,
    PREPARATION_RESULT_RELEASE_NAME,
    PREPARATION_RESULT_SENT_NAME,
    R_PREPARATION_RESOLVE_CHECKPOINT_NAME,
    R_PREPARATION_RESOLVE_RELEASE_NAME,
    RELEASE_NAME,
    RETIREMENT_RELEASE_NAME,
    SHUTDOWN_RECEIVED_NAME,
    STDIN_FAILURE_RELEASED_NAME,
    ServerRelayClient,
    _normalize_shutdown_grace,
    _receive_checkpointed,
    _tool_error,
)
from support.progress import without_elapsed
from support.assertions import tool_text as _tool_text
from support.checkpoints import FifoCheckpoint
from support.client import stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.resolvers import fake_ir_environment as _fake_ir_environment
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_prepares_initial_requirements_before_stdin_and_skips_retained_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "initial-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary,
            "initial_requirements_stdin_idempotent",
            environment,
            execution=execution,
        )

        assert (
            _tool_text(
                client.send(
                    python="42",
                    stdin="answer\n",
                    requirements={"r": ["initial-requirement"]},
                )
            )
            == "[done]"
        )
        assert (
            _tool_text(
                client.send(
                    r="43",
                    requirements={"r": ["initial-requirement"]},
                )
            )
            == "[done]"
        )
        transcript = client.finish_active()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    server_commands = [
        entry["server"] for entry in transcript if entry.keys() == {"server"}
    ]
    assert [command["kind"] for command in server_commands[:3]] == [
        "stdin",
        "evaluate",
        "evaluate",
    ], server_commands
    assert not any(command["kind"] == "prepare_r" for command in server_commands)
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_send_timeout_includes_blocked_requirements_resolver(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as temporary,
        closing(
            FifoCheckpoint.create(Path(temporary) / "clock-advanced")
        ) as clock_advanced,
    ):
        root = Path(temporary)
        library = root / "timeout-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment.update(
            {
                LOADER_VARIABLE: str(build_interposer(root, "relay_completed_output")),
                "MCP_CONSOLE_TEST_CLOCK_AFTER_FRAME": '"result":{}',
                "MCP_CONSOLE_TEST_OUTPUT_COMPLETE": str(root / "clock-advanced"),
                "MCP_CONSOLE_TEST_CLOCK_SECONDS": "2",
            }
        )
        client = ServerRelayClient(
            binary,
            "live_r_requirements_then_evaluate",
            environment,
            execution=execution,
        )
        client.start_worker()
        evaluation_received = FifoCheckpoint.attach(
            client.relay_root() / IDLE_R_EVALUATION_RECEIVED_NAME
        )
        evaluation_release = FifoCheckpoint.attach(client.relay_root() / RELEASE_NAME)
        finished = False
        try:
            evaluation = client.client.start_send(
                r="42",
                requirements={"r": ["timeout-requirement"]},
                # Keep the real timer wakeup short too: Linux epoll timers do
                # not advance with the interposed observation clock.
                timeout_ms=1_000,
            )
            resolver_started.wait()
            client.client.request("ping")
            clock_advanced.wait("send observation deadline has passed")
            assert "result" not in evaluation, evaluation

            resolver_release.release()
            evaluation_received.wait()
            _receive_checkpointed(
                client.client,
                evaluation,
                "the evaluation after requirement resolution",
            )
            assert without_elapsed(_tool_text(evaluation["result"])) == (
                "\n[running; poll with an empty send]"
            )
            evaluation_release.release()
            assert _tool_text(client.send()) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            evaluation_release.release()
            evaluation_received.close()
            evaluation_release.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()
            resolver_started.close()
            resolver_release.close()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<timeout-candidate>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared_events == [{"kind": "r_prepared", "library": str(library)}]
    prepared_events[0]["library"] = "<timeout-candidate>"
    evaluations = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
    ]
    assert evaluations == [{"kind": "evaluate", "language": "r", "source": "42"}]
    return transcript


@executions(DIRECT, SANDBOXED)
@requires(POSIX)
def test_stdin_forwarding_failure_does_not_execute_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(binary, "stdin_forwarding_failure", execution=execution)
    client.start_worker()
    relay_root = client.relay_root()
    capture_path = relay_root / CAPTURE_NAME
    finished = False
    with capture_path.open(encoding="utf-8") as capture:
        try:
            evaluation = client.client.start_send(
                r="must not execute",
                stdin="x" * (4 * 1024 * 1024),
            )
            checkpoint = client._wait_for(CHECKPOINT_NAME)
            (client.root / STDIN_FAILURE_RELEASED_NAME).touch()
            checkpoint.with_name(RELEASE_NAME).touch()
            client.client.receive(evaluation)
            result = evaluation["result"]
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert "worker relay stdin write failed" in output, output
            assert output.endswith(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), output

            transcript = client._read_open_capture(capture, allow_raw=True)
            assert transcript[:2] == [
                {"relay": {"kind": "worker_started", "pid": 42}},
                {"relay": {"kind": "ready"}},
            ], transcript
            assert transcript[2].keys() == {"server_raw"}, transcript
            raw = base64.b64decode(transcript[2]["server_raw"], validate=True)
            assert raw.startswith(b'{"kind":"stdin","data":"'), raw
            transcript[2]["server_raw"] = "<partial stdin frame>"
            assert not any(entry.keys() == {"server"} for entry in transcript), (
                transcript
            )

            assert _tool_text(client.send(r="42")) == "[done]"
            captures = list(client.root.rglob(CAPTURE_NAME))
            assert len(captures) == 1 and captures[0] != capture_path, (
                capture_path,
                captures,
            )
            replacement_capture_path = captures[0]
            with replacement_capture_path.open(encoding="utf-8") as replacement:
                client.client.finish()
                transcript.extend(client._read_open_capture(replacement))
            assert len(_normalize_shutdown_grace(transcript)) == 1
            finished = True
            return transcript
        finally:
            if not finished:
                stop_client(client.client)
            client._temporary.cleanup()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_consumes_late_r_preparation_retirement_events(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "candidate-one", root / "candidate-two"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        client = ServerRelayClient(
            binary, "late_r_prepared_retirement", environment, execution=execution
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        preparation_received = FifoCheckpoint.attach(
            old_root / PREPARATION_RECEIVED_NAME
        )
        shutdown_received = FifoCheckpoint.attach(old_root / SHUTDOWN_RECEIVED_NAME)
        retirement_release = FifoCheckpoint.attach(old_root / RETIREMENT_RELEASE_NAME)
        finished = False
        try:
            preparation = client.client.start_send(
                r="must not execute after resolver restart race",
                requirements={"r": ["ordered-retirement"]},
            )
            preparation_received.wait()
            restart = client.client.start_send(control="restart")
            shutdown_received.wait()
            # The preparation response is released by the ordered retirement
            # marker, so the old relay result below is necessarily late.
            _receive_checkpointed(
                client.client,
                preparation,
                "the retired R preparation",
            )
            preparation_result = preparation["result"]
            assert preparation_result == {
                "content": [
                    {"type": "text", "text": "R preparation cancelled by restart"}
                ],
                "isError": True,
            }, preparation_result
            retirement_release.release()
            _receive_checkpointed(client.client, restart, "restart")
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            output = restart_result["content"][0]["text"]
            assert output == (
                "drained old stdout\n"
                "drained old stderr\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), output
            assert "status 33" not in output, output

            old_transcript = client._read_open_capture(old_capture)
            replacement_root = client.relay_root()
            assert replacement_root != old_root
            replacement_capture = (replacement_root / CAPTURE_NAME).open(
                encoding="utf-8"
            )
            try:
                assert (
                    _tool_text(client.send(requirements={"r": ["ordered-retirement"]}))
                    == "[prepared]"
                )
                client.client.finish()
                finished = True
                replacement_transcript = client._read_open_capture(replacement_capture)
            finally:
                replacement_capture.close()
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            preparation_received.close()
            shutdown_received.close()
            retirement_release.close()
            client._temporary.cleanup()

    transcript = old_transcript + replacement_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 2, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert [command["library"] for command in prepare_commands] == list(
        map(str, libraries)
    ), prepare_commands
    for command in prepare_commands:
        command["library"] = f"<{Path(command['library']).name}>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert [event["library"] for event in prepared_events] == list(map(str, libraries))
    for event in prepared_events:
        event["library"] = f"<{Path(event['library']).name}>"
    assert not any(
        entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
        for entry in transcript
    ), transcript
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_discards_pre_marker_r_preparation_result(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [
            root / "candidate-one",
            root / "candidate-two",
            root / "candidate-three",
        ]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release_gate = FifoCheckpoint.create(root / "resolver-release-gate")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        resolver_finished = FifoCheckpoint.create(root / "resolver-finished")
        environment["MCP_CONSOLE_TEST_IR_GATE_INDEX"] = "1"
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE_GATE"] = str(
            resolver_release_gate.path
        )
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_IR_FINISHED"] = str(resolver_finished.path)

        client = ServerRelayClient(
            binary,
            "pre_marker_r_prepared_replacement",
            environment,
            execution=execution,
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        preparation_received = FifoCheckpoint.attach(
            old_root / PREPARATION_RECEIVED_NAME
        )
        result_release = FifoCheckpoint.attach(
            old_root / PREPARATION_RESULT_RELEASE_NAME
        )
        result_sent = FifoCheckpoint.attach(old_root / PREPARATION_RESULT_SENT_NAME)
        shutdown_received = FifoCheckpoint.attach(old_root / SHUTDOWN_RECEIVED_NAME)
        finished = False
        try:
            preparation = client.client.start_send(
                requirements={"r": ["old-generation"]},
            )
            preparation_received.wait()
            restart = client.client.start_send(
                control="restart",
                requirements={"r": ["replacement-generation"]},
            )
            resolver_started.wait()
            result_release.release()
            result_sent.wait()
            resolver_release.release()
            resolver_release_gate.release()
            resolver_finished.wait()
            client.client.receive_many([preparation, restart])

            assert preparation["result"] == {
                "content": [
                    {"type": "text", "text": "R preparation cancelled by restart"}
                ],
                "isError": True,
            }, preparation
            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result
            shutdown_received.wait()

            old_transcript = client._read_open_capture(old_capture)
            replacement_root = client.relay_root()
            assert replacement_root != old_root
            replacement_capture = (replacement_root / CAPTURE_NAME).open(
                encoding="utf-8"
            )
            try:
                assert (
                    _tool_text(
                        client.send(requirements={"r": ["replacement-generation"]})
                    )
                    == "[prepared]"
                )
                # The old result did not enter the replacement requirement set.
                assert (
                    _tool_text(client.send(requirements={"r": ["old-generation"]}))
                    == "[prepared]"
                )
                client.client.finish()
                finished = True
                replacement_transcript = client._read_open_capture(replacement_capture)
            finally:
                replacement_capture.close()
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            preparation_received.close()
            result_release.close()
            result_sent.close()
            shutdown_received.close()
            resolver_started.close()
            resolver_release_gate.close()
            resolver_release.close()
            resolver_finished.close()
            client._temporary.cleanup()

    transcript = old_transcript + replacement_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 2, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert [command["library"] for command in prepare_commands] == [
        str(libraries[0]),
        str(libraries[2]),
    ], prepare_commands
    for command in prepare_commands:
        command["library"] = f"<{Path(command['library']).name}>"
    prepared_events = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert [event["library"] for event in prepared_events] == [
        str(libraries[0]),
        str(libraries[2]),
    ], prepared_events
    for event in prepared_events:
        event["library"] = f"<{Path(event['library']).name}>"
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_r_preparation_failure_requires_restart_and_preserves_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "failed-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary, "r_preparation_failure", environment, execution=execution
        )
        client.start_worker()

        result = client.send(
            r="must not execute",
            requirements={"r": ["failing-preparation"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "scripted R preparation failed; further requirement "
                        "changes are unavailable until session restart"
                    ),
                }
            ],
            "isError": True,
        }, result

        assert _tool_text(client.send(r="42")) == "[done]"
        result = client.send(
            r="must not execute after restart requirement",
            requirements={"r": ["not-forwarded"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": ("requirements require session restart; cell was not run"),
                }
            ],
            "isError": True,
        }, result
        transcript = client.finish_active()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<failed-candidate>"
    evaluations = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "evaluate"
    ]
    assert evaluations == [{"kind": "evaluate", "language": "r", "source": "42"}]
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_runtime_r_resolution_during_r_preparation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "explicit-candidate", root / "nested-candidate"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        client = ServerRelayClient(
            binary,
            "r_resolution_during_r_preparation",
            environment,
            execution=execution,
        )
        client.start_worker()
        old_root = client.relay_root()
        checkpoint = FifoCheckpoint.attach(
            old_root / R_PREPARATION_RESOLVE_CHECKPOINT_NAME
        )
        release = FifoCheckpoint.attach(old_root / R_PREPARATION_RESOLVE_RELEASE_NAME)
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            evaluation = client.client.start_send(
                r="must not execute",
                requirements={"r": ["explicit-package"]},
            )
            checkpoint.wait(client=client.client)
            release.release()
            _receive_checkpointed(client.client, evaluation, "the rejected R callback")
            result = evaluation["result"]
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert output.endswith("[worker stopped: in-memory state lost]"), output
            old_transcript = client._read_open_capture(old_capture)
            client.client.finish()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            checkpoint.close()
            release.close()
            client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    transcript = old_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(libraries[0])}]
    prepare_commands[0]["library"] = "<explicit-candidate>"
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") in {"r_resolved", "r_resolution_failed"}
        for entry in transcript
    ), transcript
    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") == "evaluate"
        and entry["server"].get("source") == "must not execute"
        for entry in transcript
    ), transcript
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_idle_runtime_r_resolution_owns_environment_until_activation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        libraries = [root / "automatic-candidate", root / "stale-explicit-candidate"]
        for library in libraries:
            library.mkdir()
        environment = _fake_ir_environment(root, libraries)
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "idle_r_resolution_owns_environment",
            environment,
            execution=execution,
        )
        client.start_worker()
        relay_root = client.relay_root()
        ready = FifoCheckpoint.attach(relay_root / IDLE_R_RESOLUTION_READY_NAME)
        release = FifoCheckpoint.attach(relay_root / IDLE_R_RESOLUTION_RELEASE_NAME)
        evaluation_received = FifoCheckpoint.attach(
            relay_root / IDLE_R_EVALUATION_RECEIVED_NAME
        )
        resolver_released = False
        ready_reached = False
        activation_released = False
        finished = False
        try:
            resolver_started.wait()
            preparation = client.client.start_send(
                requirements={"r": ["english"]},
            )
            _receive_checkpointed(
                client.client,
                preparation,
                "explicit preparation while the idle R resolver was blocked",
            )
            _tool_error(preparation, "idle runtime R callback owns environment changes")

            assert (
                without_elapsed(_tool_text(client.send(r="42", timeout_ms=0)))
                == "\n[running; poll with an empty send]"
            )
            resolver_release.release()
            resolver_released = True
            ready.wait()
            ready_reached = True
            evaluation_received.wait()
            release.release()
            activation_released = True
            assert _tool_text(client.send()) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not resolver_released:
                resolver_release.release()
            if not ready_reached:
                ready.wait()
            if not activation_released:
                release.release()
            resolver_started.close()
            resolver_release.close()
            ready.close()
            release.close()
            evaluation_received.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    assert not any(
        entry.keys() == {"server"}
        and entry["server"].get("kind") in {"prepare_r", "r_resolution_failed"}
        for entry in transcript
    ), transcript
    for entry in transcript:
        message = entry.get("server", entry.get("relay", {}))
        if message.get("kind") in {"r_resolved", "r_activated"}:
            assert message["library"] == str(libraries[0]), message
            message["library"] = "<automatic-candidate>"
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_explicit_r_preparation_owns_environment_before_host_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "explicit-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        resolver_started = FifoCheckpoint.create(root / "resolver-started")
        resolver_release = FifoCheckpoint.create(root / "resolver-release")
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        client = ServerRelayClient(
            binary,
            "explicit_r_preparation_owns_environment",
            environment,
            execution=execution,
        )
        client.start_worker()
        relay_root = client.relay_root()
        callback = FifoCheckpoint.attach(
            relay_root / EXPLICIT_R_PREPARATION_CALLBACK_NAME
        )
        callback_reply = FifoCheckpoint.attach(
            relay_root / EXPLICIT_R_PREPARATION_CALLBACK_REPLY_NAME
        )
        callback_released = False
        resolver_released = False
        finished = False
        try:
            preparation = client.client.start_send(
                requirements={"r": ["english"]},
            )
            resolver_started.wait()
            callback.release()
            callback_released = True
            callback_reply.wait()
            readable, _, _ = select.select([client.client.stdout], [], [], 0.25)
            assert not readable, (
                "preparation completed before its resolver was released"
            )

            resolver_release.release()
            resolver_released = True
            client.client.receive(preparation)
            assert _tool_text(preparation["result"]) == "[prepared]"
            # MCP preparation completion does not order the relay's next callback.
            callback_reply.wait("runtime R callback reply before evaluation")
            assert _tool_text(client.send(r="42")) == "[done]"
            transcript = client.finish_active()
            finished = True
        finally:
            if not callback_released:
                callback.release()
            if not resolver_released:
                resolver_release.release()
            callback.close()
            callback_reply.close()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client.client)
                client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    failures = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"}
        and entry["server"].get("kind") == "r_resolution_failed"
    ]
    assert failures == [
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "R package resolution is unavailable during requirement preparation"
            ),
        }
    ]
    prepare_commands = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "prepare_r"
    ]
    assert prepare_commands == [{"kind": "prepare_r", "library": str(library)}]
    prepare_commands[0]["library"] = "<explicit-candidate>"
    prepared = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "r_prepared"
    ]
    assert prepared == [{"kind": "r_prepared", "library": str(library)}]
    prepared[0]["library"] = "<explicit-candidate>"
    runtime_environment = [
        message
        for entry in transcript
        if entry.keys() in ({"server"}, {"relay"})
        and (message := next(iter(entry.values()))).get("kind")
        in {"r_resolved", "r_activated"}
    ]
    assert runtime_environment == [
        {"kind": "r_resolved", "library": str(library)},
        {"kind": "r_activated", "library": str(library)},
    ]
    for message in runtime_environment:
        message["library"] = "<explicit-candidate>"
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_completion_before_runtime_r_activation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        library = root / "automatic-candidate"
        library.mkdir()
        environment = _fake_ir_environment(root, [library])
        client = ServerRelayClient(
            binary, "completion_before_r_activation", environment, execution=execution
        )
        client.start_worker()
        old_root = client.relay_root()
        old_capture = (old_root / CAPTURE_NAME).open(encoding="utf-8")
        finished = False
        try:
            result = client.send(r="42")
            assert result.get("isError") is True, result
            output = result["content"][0]["text"]
            assert output.startswith(
                "[worker sent an operation result before completing runtime R "
                "activation]\n"
            ), output
            assert output.endswith(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), output
            old_transcript = client._read_open_capture(old_capture)
            client.client.finish()
            finished = True
        finally:
            if not finished:
                stop_client(client.client)
            old_capture.close()
            client._temporary.cleanup()

        assert (root / "ir-counter").read_text(encoding="utf-8") == "1"

    transcript = old_transcript
    shutdown = _normalize_shutdown_grace(transcript)
    assert len(shutdown) == 1, transcript
    resolved = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"} and entry["server"].get("kind") == "r_resolved"
    ]
    assert resolved == [{"kind": "r_resolved", "library": str(library)}]
    resolved[0]["library"] = "<automatic-candidate>"
    assert not any(
        entry.keys() == {"relay"}
        and entry["relay"].get("kind") in {"r_activated", "r_activation_failed"}
        for entry in transcript
    ), transcript
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
