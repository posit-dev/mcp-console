"""Session capacity is reserved through startup and confirmed retirement."""

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment as without_r
from boundaries.client_server.server.test_no_r import no_r_environment
from boundaries.client_server.server.test_startup_retry import (
    preparation_reap_environment,
)
from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.discovery import discovery_environment
from support.execution import DIRECT, RUNTIME, Execution, executions
from support.normalization import normalize_process_diagnostic
from support.previews import recording_events
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
)
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    command,
    requires,
)
from support.suites import run_this_suite


def assert_limit(client: McpClient, maximum: int, **arguments: object) -> None:
    result = client.send(**arguments)
    assert result["isError"] is True, result
    message = last_result_text(client)
    assert "session limit reached" in message, message
    assert f"sessions.max = {maximum}" in message, message


@executions(RUNTIME)
def test_default_reserves_capacity_and_remains_usable(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with McpClient(
            binary,
            execution.serve("-c", "sessions.max=1"),
            dict(without_r(root), RETICULATE_PYTHON=sys.executable),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 40")
            assert_limit(client, 1, session="alpha", python="print('must not execute')")
            absent = client.send(session="alpha", timeout_ms=0)
            assert absent["isError"] is True, absent
            assert 'unknown session "alpha"' in last_result_text(client)
            assert client.send(timeout_ms=0)["structuredContent"] == {
                "sessions": [{"session": "default", "status": "idle"}]
            }
            client.expect("42\n", python="retained += 2; print(retained)")
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect("False\n", python="print('retained' in globals())")
            assert_limit(client, 1, session="beta", python="print('must not execute')")
            return client.finish()


@executions(RUNTIME)
def test_stopping_named_session_releases_capacity_for_creation_and_relaunch(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        rejected_source = "raise AssertionError('rejected source executed')"
        with McpClient(
            binary,
            execution.serve("-c", "sessions.max=2"),
            dict(without_r(root), RETICULATE_PYTHON=sys.executable),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 'default'")
            client.expect(python="retained = 100", session="alpha")
            assert_limit(client, 2, session="beta", python=rejected_source)
            absent = client.send(session="beta", timeout_ms=0)
            assert absent["isError"] is True, absent
            assert 'unknown session "beta"' in last_result_text(client)
            restarted = client.send(
                session="alpha",
                control="restart",
                python="retained = 100; print(retained)",
            )
            assert restarted["isError"] is False, restarted
            assert last_result_text(client).endswith("100\n[done]"), restarted
            client.expect(
                "101\n", python="retained += 1; print(retained)", session="alpha"
            )
            stopped = client.send(session="alpha", control="restart")
            assert stopped["isError"] is False, stopped
            assert "[worker stopped: in-memory state lost]" in last_result_text(client)
            client.expect(python="retained = 200", session="beta")
            assert_limit(client, 2, session="alpha", python=rejected_source)
            assert_limit(client, 2, session="alpha", stdin="rejected input\n")
            assert_limit(client, 2, session="alpha", requirements={"action": "set"})
            inspected = client.send(session="alpha", requirements={"action": "get"})
            assert inspected["isError"] is False, inspected
            assert_limit(
                client, 2, session="alpha", control="restart", python=rejected_source
            )
            client.expect("\n[stopped]", session="alpha")
            assert client.send(timeout_ms=0)["structuredContent"] == {
                "sessions": [
                    {"session": "beta", "status": "idle"},
                    {"session": "default", "status": "idle"},
                ]
            }
            stopped = client.send(session="beta", control="restart")
            assert stopped["isError"] is False, stopped
            client.expect(
                '[starting new worker]\nFalse\n[input requested: "fresh> "]\n[waiting for stdin]',
                python="print('retained' in globals()); print(input('fresh> '))",
                session="alpha",
            )
            client.expect("fresh input\n", session="alpha", stdin="fresh input\n")
            client.expect("default\n", python="print(retained)")
            transcript = client.finish()

        (recording,) = (root / ".agents/console/sessions").iterdir()
        events = recording_events(recording)
        rejected_calls = {
            event["call_id"]
            for event in events
            if event["event"] == "tool_call"
            and event["request"]["arguments"].get("python") == rejected_source
        }
        assert len(rejected_calls) == 3, rejected_calls
        assert not any(
            event["event"] in ("source_admitted", "cell_started")
            and event["call_id"] in rejected_calls
            for event in events
        ), events
        assert [
            event.get("session", "default")
            for event in events
            if event["event"] == "worker_started"
        ] == ["default", "alpha", "alpha", "beta", "alpha"], events
        return transcript


@requires(command("uv"))
@executions(RUNTIME)
def test_stopped_preparation_reserves_capacity_but_inspection_does_not(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with McpClient(
            binary,
            execution.serve("-c", "sessions.max=2", "-c", "python.managed.packages=[]"),
            no_r_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 'default'")
            client.expect(python="retained = 'alpha'", session="alpha")
            stopped = client.send(session="alpha", control="restart")
            assert stopped["isError"] is False, stopped
            client.expect(python="retained = 'beta'", session="beta")
            assert_limit(client, 2, session="alpha", requirements={"action": "set"})
            inspected = client.send(session="alpha", requirements={"action": "get"})
            assert inspected["isError"] is False, inspected
            client.expect("\n[stopped]", session="alpha")
            client.expect("beta\n", python="print(retained)", session="beta")
            stopped = client.send(session="beta", control="restart")
            assert stopped["isError"] is False, stopped
            client.expect("[prepared]", session="alpha", requirements={"action": "set"})
            client.expect("\n[stopped]", session="alpha")
            client.expect(
                "[starting new worker]\n42\n", session="beta", python="print(42)"
            )
            client.expect("default\n", python="print(retained)")
            return client.finish()


@requires(command("rustc"))
def test_pending_discovery_reserves_capacity_until_failed_preparation_retires(
    binary: Path,
) -> Transcript:
    with discovery_environment() as (environment, discovery):
        hidden = discovery.executable.with_name("hidden-R")
        discovery.executable.rename(hidden)
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary, DIRECT.serve("-c", "sessions.max=2"), environment, discovery.root
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 40")
            hidden.rename(discovery.executable)
            accepted = client.send(
                session="alpha", python="rejected = True", timeout_ms=0
            )
            assert accepted["isError"] is False, accepted
            discovery.wait(
                "alpha reserves the final slot while runtime discovery waits"
            )
            competing = [
                client.start_send(
                    session=name, python="raise AssertionError('ran')", timeout_ms=0
                )
                for name in ("beta", "gamma")
            ]
            client.receive_many(competing)
            for call in competing:
                assert call["result"]["isError"] is True, call
                text = "".join(part["text"] for part in call["result"]["content"])
                assert "session limit reached" in text and "sessions.max = 2" in text, (
                    call
                )
                name = call["send"]["session"]
                missing = client.send(session=name, timeout_ms=0)
                assert missing["isError"] is True, missing
                assert f'unknown session "{name}"' in last_result_text(client)
            assert client.send(timeout_ms=0)["structuredContent"] == {
                "sessions": [
                    {"session": "alpha", "status": "starting"},
                    {"session": "default", "status": "idle"},
                ]
            }
            discovery.release()
            failed = client.send(session="alpha", timeout_ms=60000)
            assert failed["isError"] is True, failed
            assert "fixture R discovery failed" in last_result_text(client)
            discovery.assert_retired()
            discovery.executable.rename(hidden)
            client.expect("42\n", session="beta", python="print(42)")
            assert_limit(
                client, 2, session="alpha", python="raise AssertionError('ran')"
            )
            stopped = client.send(session="beta", control="restart")
            assert stopped["isError"] is False, stopped
            client.expect(
                "False\n", session="alpha", python="print('rejected' in globals())"
            )
            client.expect("40\n", python="print(retained)")
            transcript = client.finish()
        return json.loads(
            normalize_process_diagnostic(json.dumps(transcript)).replace(
                str(discovery.root), "<discovery>"
            )
        )


@requires(command("rustc"))
def test_default_keeps_its_slot_through_discovery_failure(binary: Path) -> Transcript:
    with discovery_environment() as (environment, discovery):
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary, DIRECT.serve("-c", "sessions.max=1"), environment, discovery.root
        ) as client:
            client.initialize_and_list_tools()
            discovery.wait(
                "default reserves capacity before runtime discovery finishes"
            )
            assert_limit(
                client, 1, session="alpha", python="raise AssertionError('ran')"
            )
            discovery.release()
            failed = client.send(session="default", timeout_ms=60000)
            assert failed["isError"] is True, failed
            assert "fixture R discovery failed" in last_result_text(client)
            discovery.assert_retired()
            assert_limit(
                client, 1, session="beta", python="raise AssertionError('ran')"
            )
            discovery.executable.rename(discovery.executable.with_name("hidden-R"))
            client.expect(
                "[runtime discovery retried]\n42\n[done]",
                control="restart",
                python="print(42)",
            )
            assert_limit(
                client, 1, session="alpha", python="raise AssertionError('ran')"
            )
            transcript = client.finish()
        return json.loads(
            normalize_process_diagnostic(json.dumps(transcript)).replace(
                str(discovery.root), "<discovery>"
            )
        )


@requires(POSIX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_unconfirmed_named_preparation_retains_capacity(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = preparation_reap_environment(root, root / "missing-python")
        environment.update(
            {
                "MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE": str(root / "blocked-close"),
                "MCP_CONSOLE_TEST_REAP_DENY_KILL": str(root / "denied-kill"),
            }
        )
        identities: list[ProcessIdentity] = []
        with (
            closing(FifoCheckpoint.create(root / "blocked-close")) as blocked,
            McpClient(
                binary, DIRECT.serve("-c", "sessions.max=2"), environment, root
            ) as client,
        ):
            try:
                client.initialize_and_list_tools()
                for session in ("default", "alpha"):
                    pending = client.start_send(session=session, python="print(42)")
                    blocked.wait(f"{session} preparation cannot acknowledge closure")
                    identity = capture_process_identity(
                        int((root / "resolver-pid").read_text())
                    )
                    identities.append(identity)
                    client.receive(pending)
                    assert pending["result"]["isError"] is True, pending
                    assert "retirement unconfirmed" in str(pending["result"]), pending
                assert len(set(identities)) == 2, identities
                assert live_processes(identities) == [
                    identity[0] for identity in identities
                ], identities
                before = child_process_identities(
                    capture_process_identity(client.process.pid)
                )
                assert set(before) == set(identities), before
                assert_limit(
                    client, 2, session="beta", python="raise AssertionError('ran')"
                )
                absent = client.send(session="beta", timeout_ms=0)
                assert absent["isError"] is True, absent
                assert 'unknown session "beta"' in last_result_text(client)
                assert (
                    child_process_identities(
                        capture_process_identity(client.process.pid)
                    )
                    == before
                ), "capacity rejection launched another preparation process"
                assert live_processes(identities) == [
                    identity[0] for identity in identities
                ], identities
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert "retirement unconfirmed" in stderr, stderr
                return json.loads(
                    json.dumps(transcript + [{"stderr": stderr}]).replace(
                        str(root), "<workspace>"
                    )
                )
            finally:
                if client.process.poll() is None:
                    kill_processes(
                        child_process_identities(
                            capture_process_identity(client.process.pid)
                        )
                    )
                kill_processes(identities)


@executions(RUNTIME)
def test_named_worker_crash_releases_capacity_after_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        with McpClient(
            binary,
            execution.serve("-c", "sessions.max=2"),
            dict(without_r(root), RETICULATE_PYTHON=sys.executable),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="retained = 'default'")
            client.expect(python="retained = 'alpha'", session="alpha")
            assert_limit(client, 2, session="beta", python="print(42)")
            failed = client.send(python="import os; os._exit(86)", session="alpha")
            assert failed["isError"] is True, failed
            assert "[worker stopped: in-memory state lost]" in last_result_text(client)
            assert "[starting new worker]" not in last_result_text(client)
            client.expect("\n[stopped]", session="alpha")
            client.expect("42\n", python="print(42)", session="beta")
            assert client.send(timeout_ms=0)["structuredContent"] == {
                "sessions": [
                    {"session": "beta", "status": "idle"},
                    {"session": "default", "status": "idle"},
                ]
            }
            client.expect("default\n", python="print(retained)")
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
