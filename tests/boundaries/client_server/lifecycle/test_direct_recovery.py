#!/usr/bin/env -S uv run --script

import os
import signal
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.events import Events
from support.execution import DIRECT
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
    signal_process,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, WORKER, requires
from support.suites import run_this_suite


@requires(WORKER, PROCESS_EVENTS)
def test_replaces_direct_relay_after_sigkill(binary: Path) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as directory, Events() as events:
        temporary = Path(directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = directory
        client = McpClient(
            binary,
            DIRECT.serve("--worker", str(zod)),
            environment,
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"
            server = capture_process_identity(client.process.pid)
            (relay,) = child_process_identities(server)
            (worker,) = child_process_identities(relay)
            identities.extend((relay, worker))

            events.watch_file(temporary)
            client.send(r="stall", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            deadline = time.monotonic() + 10
            while not (temporary / "zod-stalled").exists():
                remaining = deadline - time.monotonic()
                assert remaining > 0, "worker did not enter its stalled evaluation"
                assert events.wait(remaining), "worker did not stall"

            signal_process(relay, signal.SIGKILL)
            result = client.send()
            assert result["isError"] is True, result
            assert result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker relay stdout closed before retirement completed]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], result
            assert live_processes((relay,)) == []
            (replacement,) = child_process_identities(server)
            assert replacement != relay
            identities.append(replacement)
            identities.extend(child_process_identities(replacement))
            client.send(r="echo recovered")
            assert last_tool_text(client) == "zod: recovered\n"
            return client.finish()
        finally:
            stop_client(client)
            kill_processes(identities)


@requires(WORKER, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_replaces_direct_relay_killed_during_cell_dispatch(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        root = Path(directory)
        gates = {
            name: stack.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in (
                "exit-reached",
                "exit-release",
                "dispatch-reached",
                "exit-settled",
                "write-release",
            )
        }
        environment = {
            **os.environ,
            LOADER_VARIABLE: str(build_interposer(root, "relay_exit_dispatch")),
            "MCP_CONSOLE_TEST_EXIT_ROOT": str(root),
        }
        client = McpClient(
            binary, DIRECT.serve("--worker", str(fixtures / "zod")), environment
        )
        identities = []
        try:
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            assert last_tool_text(client) == "zod: ready\n"
            server = capture_process_identity(client.process.pid)
            (relay,) = child_process_identities(server)
            identities.extend((relay, *child_process_identities(relay)))
            (root / "armed").touch()
            signal_process(relay, signal.SIGKILL)
            gates["exit-reached"].wait(
                "relay-exit dispatcher before failure settlement"
            )

            failed = client.start_send(r="echo no-replay")
            gates["dispatch-reached"].wait(
                "cell dispatch or rejected command retirement"
            )
            gates["exit-release"].release()
            gates["exit-settled"].wait("relay-exit dispatcher settled and exited")
            gates["write-release"].release()
            client.receive(failed)
            result = failed["result"]
            assert result["isError"] is True, result
            output = result["content"][0]["text"]
            assert "[worker stopped: in-memory state lost]" in output, output
            assert "[starting new worker]" in output, output
            assert "worker launcher terminated by signal 9" not in output, output
            assert "zod: no-replay" not in output, output
            assert live_processes((relay,)) == []
            (replacement,) = child_process_identities(server)
            assert replacement != relay
            identities.extend((replacement, *child_process_identities(replacement)))
            client.send(r="echo recovered")
            assert last_tool_text(client) == "zod: recovered\n"
            return client.finish()
        finally:
            gates["exit-release"].release()
            gates["write-release"].release()
            stop_client(client)
            kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
