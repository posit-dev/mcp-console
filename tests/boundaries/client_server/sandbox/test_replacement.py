#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.assertions import last_tool_text
from support.checkpoints import (
    release_fixture_checkpoint,
    release_partial_sideband,
)
from support.client import McpClient
from support.execution import SANDBOXED
from support.processes import (
    host_process_id,
    process_exists,
    process_group_exists,
    stop_process,
    stop_process_group,
    stop_process_id,
)
from support.records import Transcript
from support.sandbox_observation import observed_sandbox_descendants
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, SANDBOX, requires
from support.suites import run_this_suite


from boundaries.client_server._harness import (
    ZodFixtureControl,
    wait_for_marker,
)


@requires(SANDBOX)
def test_restarts_after_unexpected_sideband_message(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            SANDBOXED.serve("--worker", str(zod)),
            environment,
        )
        worker_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(r="report process group")
            process_group_output = last_tool_text(client)
            process_group_prefix = "zod process group: "
            assert process_group_output.startswith(process_group_prefix), (
                process_group_output
            )
            reported_group = int(
                process_group_output.removeprefix(process_group_prefix)
            )
            assert process_group_output == f"{process_group_prefix}{reported_group}\n"
            worker_group = host_process_id(reported_group, client.process.pid)
            assert worker_group != os.getpgrp(), (
                "Zod did not enter a dedicated process group"
            )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "zod process group: <process group>\n"
            )
            failed_call = client.start_send(r="violate protocol")
            client.receive(failed_call)
            assert not process_exists(worker_group), (
                "sandbox launcher did not reap the failed generation's relay"
            )
            assert not process_group_exists(worker_group), (
                "failed worker generation survived sandbox manager retirement"
            )
            result = failed_call["result"]
            assert result["isError"] is True
            actual = result["content"][0]["text"]
            assert actual == (
                "zod output before protocol failure\n"
                "[worker sent an unexpected ready message]\n"
                "[worker terminated by signal 9]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), repr(actual)
            restarted_call = client.start_send(r="complete silently")
            client.receive(restarted_call)
            assert last_tool_text(client) == "[done]"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(worker_group)
                stop_process(client.process)


@requires(SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_restarts_after_worker_exit_with_partial_sideband(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with (
        tempfile.TemporaryDirectory() as temporary_directory,
        ZodFixtureControl(Path(temporary_directory)) as control,
        observed_sandbox_descendants(
            Path(temporary_directory), environment
        ) as wait_for_descendant,
    ):
        temporary_path = Path(temporary_directory)
        startup_control = temporary_path / "zod-startup-control"
        startup_release = temporary_path / "zod-startup-release"
        startup_control.write_text("ready", encoding="utf-8")
        environment["ZOD_STARTUP_CONTROL"] = str(startup_control)
        environment["ZOD_STARTUP_RELEASE"] = str(startup_release)
        control.configure(environment)
        descendant_group = None
        try:
            with (
                McpClient(
                    binary,
                    SANDBOXED.serve("--worker", str(zod)),
                    environment,
                ) as client,
                ExitStack() as cleanup,
            ):
                cleanup.callback(startup_release.touch)
                client.initialize_and_list_tools()
                client.send(r="complete silently")
                assert last_tool_text(client) == "[done]"
                startup_control.write_text("block", encoding="utf-8")
                failed = client.start_send(
                    r="exit after partial sideband descendant",
                    timeout_ms=15_000,
                )
                marker = wait_for_marker(
                    temporary_path,
                    "zod-sideband-descendant-pid",
                    client,
                )
                descendant_group = host_process_id(
                    int(marker.read_text(encoding="utf-8")), client.process.pid
                )
                wait_for_descendant(descendant_group, client.process)
                # Keep the event channel open before exit removes the directory.
                control.connect(client)
                release_partial_sideband(marker, client=client)
                control.wait_for(0, "partial_sideband_written")
                wait_for_marker(
                    temporary_path,
                    "zod-replacement-waiting-ready",
                    client,
                )

                client.receive(failed)
                result = failed["result"]
                assert not process_group_exists(descendant_group), (
                    "partial-sideband descendant outlived sandbox retirement"
                )
                descendant_group = None

                # Retirement is complete while replacement is held before ready.
                # Preserve the failure and each public phase observation.
                assert result == {
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
                }, result
                assert client.send(timeout_ms=0) == {
                    "content": [
                        {
                            "type": "text",
                            "text": "\n[phase: replacement]\n[worker starting]",
                        }
                    ],
                    "isError": False,
                }
                startup_release.touch()
                client.send(timeout_ms=15_000)
                assert last_tool_text(client) == "[idle]"

                client.send(r="echo echo")
                assert client.transcript[-1]["result"] == {
                    "content": [{"type": "text", "text": "zod: echo\n"}],
                    "isError": False,
                }, client.transcript[-2:]
                return client.finish()
        finally:
            stop_process_group(descendant_group)


@requires(SANDBOX, PROCESS_EVENTS)
def test_replaces_worker_after_relay_exit(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        client = McpClient(
            binary,
            SANDBOXED.serve(
                "--worker", str(zod), "--relay", str(zod.with_name("identified_relay"))
            ),
            environment,
        )
        worker_pid = None
        relay_pid = None
        relay_group = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # Establish the worker and relay before testing their retirement.
            client.send(requirements={"action": "get"})
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            client.send(r="kill relay and remain live", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            started = wait_for_marker(
                temporary_path,
                "zod-relay-exit-evaluation-started",
                client,
            )
            reported_worker, reported_relay, reported_group = map(
                int, started.read_text().split()
            )
            worker_pid, relay_pid, relay_group = (
                host_process_id(pid, client.process.pid)
                for pid in (reported_worker, reported_relay, reported_group)
            )
            assert os.getpgid(relay_pid) == relay_group
            assert relay_pid != worker_pid, "worker unexpectedly identified the relay"
            release_fixture_checkpoint(
                started.parent / "zod-release-relay-exit", client=client
            )
            client.send()

            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            text = result["content"][0]["text"]
            assert text.startswith("zod worker pid: "), text
            topology, failure = text.split("\n", 1)
            worker, relay = topology.split("; ")
            assert int(worker.removeprefix("zod worker pid: ")) == reported_worker
            assert int(relay.removeprefix("relay process group: ")) == reported_group
            assert worker_pid != relay_pid, topology
            assert failure == (
                "[worker relay stdout closed before retirement completed]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            ), failure
            result["content"][0]["text"] = (
                "zod worker pid: <worker pid>; "
                "relay process group: <relay process group>\n" + failure
            )
            assert not process_exists(worker_pid), "worker outlived its relay"
            assert not process_exists(relay_pid), "server did not reap the relay"
            assert not process_exists(relay_group), "sandbox runner outlived its relay"
            assert not process_group_exists(relay_group), (
                "relay process group outlived sandbox manager retirement"
            )

            client.send(r="echo echo")
            assert last_tool_text(client) == "zod: echo\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_process_group(relay_group)
                stop_process_id(worker_pid)
                stop_process(client.process)


if __name__ == "__main__":
    run_this_suite(__file__)
