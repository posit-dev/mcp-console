#!/usr/bin/env -S uv run --script

import os
import re
import select
import time
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.native import build_interposer
from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.macos import (
    DarwinProcessIdentity,
    kill_darwin_processes,
    live_darwin_processes,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    darwin_process_file_descriptors,
)
from support.records import Transcript
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite


TIMEOUT = 10
MARKER_NAME = "mcp-console-startup-marker"
# Python's select module omits Darwin's deprecated process-reaping flag.
_KQ_NOTE_REAP = 0x10000000


def _wait_for_startup_cleanup(
    identities: tuple[DarwinProcessIdentity, ...],
    events: "select.kqueue",
) -> None:
    watched = {identity[0] for identity in identities}
    remaining = watched.copy()
    deadline = time.monotonic() + TIMEOUT
    while remaining:
        timeout = deadline - time.monotonic()
        assert timeout > 0, f"startup cleanup left processes {remaining}"
        notifications = events.control(None, len(watched), timeout)
        assert notifications, f"startup cleanup left processes {remaining}"
        for event in notifications:
            assert event.ident in watched, event
            assert event.filter == select.KQ_FILTER_PROC, event
            # Exit alone leaves a zombie identity until its parent reaps it.
            if event.fflags & _KQ_NOTE_REAP:
                remaining.remove(event.ident)


def _assert_zod_echo(entry: dict[str, object]) -> None:
    result = entry["result"]
    assert result == {
        "content": [{"type": "text", "text": "zod: echo\n"}],
        "isError": False,
    }, result


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_sandbox_setup_failure_is_reported_and_retryable(binary: Path) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory:
        temporary_parent = Path(directory) / "sandbox-parent"
        temporary_parent.write_text("not a directory", encoding="utf-8")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary_parent)
        client = McpClient(
            binary, SANDBOXED.serve("--worker", str(worker)), environment
        )
        try:
            server = capture_darwin_process_identity(client.process.pid)
            client.initialize_and_list_tools()
            result = client.send(r="echo echo")
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": "mcp-console-sandbox: create private storage: Not a directory (os error 20)\n[worker relay exited before readiness]",
                    }
                ],
                "isError": True,
            }, result
            assert darwin_child_process_identities(server) == ()

            temporary_parent.unlink()
            temporary_parent.mkdir()
            client.send(r="echo echo")
            _assert_zod_echo(client.transcript[-1])
            transcript, stderr = client.finish_with_standard_error()
            prefix = "mcp-console: transcript recording disabled: failed to create temporary capture: Not a directory (os error 20) at path "
            assert re.fullmatch(
                re.escape(prefix + '"' + str(temporary_parent)) + r'/\.tmp[^"\n]+"\n',
                stderr,
            ), stderr
            transcript.append({"stderr": prefix + '"<temporary capture>"\n'})
            return transcript
        finally:
            stop_client(client)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_manager_failure_before_readiness_keeps_custom_relay_gated(
    binary: Path,
) -> Transcript:
    return _manager_failure_before_readiness(binary, diagnostic_before_cut=True)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_manager_failure_does_not_wait_for_future_helper_stderr(
    binary: Path,
) -> Transcript:
    return _manager_failure_before_readiness(binary, diagnostic_before_cut=False)


def _manager_failure_before_readiness(
    binary: Path, *, diagnostic_before_cut: bool
) -> Transcript:
    fixture_root = Path(__file__).resolve().parents[3] / "fixtures"
    worker = fixture_root / "zod"
    marker_relay = fixture_root / "startup_marker_relay"

    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        closing(select.kqueue()) as reaping_events,
    ):
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint.create(temporary / "manager-started")
        manager_release = FifoCheckpoint.create(temporary / "manager-release")
        diagnostic_checkpoints = {
            name: FifoCheckpoint.create(temporary / name.lower().replace("_", "-"))
            for name in (
                "DIAGNOSTIC_STARTED",
                "DIAGNOSTIC_RELEASE",
                "DIAGNOSTIC_WRITTEN",
                "HELPER_EXIT",
                "DRAIN_STARTED",
                "DRAIN_RELEASE",
                "DRAIN_EMPTY",
                "DRAIN_DIAGNOSTIC",
            )
        }
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment.update(
            {
                f"MCP_CONSOLE_TEST_{name}": str(checkpoint.path)
                for name, checkpoint in diagnostic_checkpoints.items()
            }
        )
        environment["DYLD_INSERT_LIBRARIES"] = str(
            build_interposer(temporary, "manager_diagnostic_interposer")
        )

        client = McpClient(
            binary,
            SANDBOXED.serve("--worker", str(worker), "--relay", str(marker_relay)),
            environment,
        )
        identities: tuple[DarwinProcessIdentity, ...] = ()
        replacement_released = False
        try:
            client.initialize_and_list_tools()
            waiting = client.start_send(r="echo echo")
            manager_started.wait("manager startup")

            server = capture_darwin_process_identity(client.process.pid)
            (manager,) = darwin_child_process_identities(server)
            (root,) = darwin_child_process_identities(manager)
            manager_pid = manager[0]
            identities = (root, manager)
            reap_watches = [
                select.kevent(
                    identity[0],
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT | _KQ_NOTE_REAP,
                )
                for identity in identities
            ]
            assert reaping_events.control(reap_watches, 0, 0) == []
            assert live_darwin_processes(identities) == [
                identity[0] for identity in identities
            ], "startup process identities changed while registering reap watches"
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []

            assert kill_darwin_processes((manager,)) == [manager_pid], (
                "sandbox manager exited before failure injection"
            )
            diagnostic_checkpoints["DIAGNOSTIC_STARTED"].wait("helper stderr write")
            for _ in range(2):
                diagnostic_checkpoints["DRAIN_STARTED"].wait("launcher-exit output cut")
            if diagnostic_before_cut:
                # Complete every diagnostic byte while both readers are held
                # before FIONREAD. The helper retains stderr until we release it.
                diagnostic_checkpoints["DIAGNOSTIC_RELEASE"].release()
                diagnostic_checkpoints["DIAGNOSTIC_WRITTEN"].wait(
                    "complete helper stderr"
                )
            for _ in range(2):
                diagnostic_checkpoints["DRAIN_RELEASE"].release()
            diagnostic_checkpoints["DRAIN_EMPTY"].wait("empty stdout cut")
            if diagnostic_before_cut:
                diagnostic_checkpoints["DRAIN_DIAGNOSTIC"].wait(
                    "complete diagnostic at stderr cut"
                )
            else:
                diagnostic_checkpoints["DRAIN_EMPTY"].wait("empty finite output cut")
            readable, _, _ = select.select([client.stdout], [], [], TIMEOUT)
            assert readable, "server did not return after sandbox manager failure"
            client.receive(waiting)
            result = waiting["result"]
            diagnostic = "mcp-console-sandbox: failed to fill whole buffer"
            output = "[worker relay exited before readiness]"
            if diagnostic_before_cut:
                output = diagnostic + "\n" + output
            assert result == {
                "content": [{"type": "text", "text": output}],
                "isError": True,
            }, result
            assert live_darwin_processes((root,)) == [root[0]]
            assert 2 in darwin_process_file_descriptors(root)
            if diagnostic_before_cut:
                diagnostic_checkpoints["HELPER_EXIT"].release()
            else:
                # A dead manager cannot supervise its deliberately held helper.
                # Retire that fixture process only after bounded response delivery.
                assert kill_darwin_processes((root,)) == [root[0]]
            _wait_for_startup_cleanup(identities, reaping_events)
            assert live_darwin_processes(identities) == [], (
                "startup helper and manager identities survived cleanup"
            )
            assert darwin_child_process_identities(server) == ()
            assert list(temporary.glob(f"**/{MARKER_NAME}")) == []
            waiting["startup_supervision_failure"] = {
                "manager": "killed before readiness",
                "custom_relay": "did not execute",
                "sandbox_stderr": diagnostic
                if diagnostic_before_cut
                else "not yet written",
                "diagnostic_order": "complete before cut"
                if diagnostic_before_cut
                else "held beyond cut and response",
                "verified_cleanup": "helper and manager reaping",
            }

            replacement = client.start_send(r="echo echo")
            manager_started.wait("replacement manager startup")
            manager_release.release()
            replacement_released = True
            client.receive(replacement)
            _assert_zod_echo(replacement)
            markers = list(temporary.glob(f"**/{MARKER_NAME}"))
            assert len(markers) == 1, markers
            replacement["startup_supervision_recovery"] = {
                "custom_relay": "executed only for the replacement generation",
            }
            return client.finish()
        finally:
            diagnostic_checkpoints["DIAGNOSTIC_RELEASE"].release()
            diagnostic_checkpoints["HELPER_EXIT"].release()
            for _ in range(2):
                diagnostic_checkpoints["DRAIN_RELEASE"].release()
            if not replacement_released:
                manager_release.release()
            stop_client(client)
            if identities:
                kill_darwin_processes(identities)
            manager_started.close()
            manager_release.close()
            for checkpoint in diagnostic_checkpoints.values():
                checkpoint.close()


if __name__ == "__main__":
    run_this_suite(__file__)
