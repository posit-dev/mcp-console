#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.allocations import AllocationProfile
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.processes import host_process_id, process_exists, stop_process_id
from support.progress import without_elapsed
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, requires
from support.suites import run_this_suite


MEMORY_BANNER = (
    "[Session stopped by the memory guard: worker process tree used 950 MiB, "
    "exceeding 90% of host RAM (1024 MiB). Session objects were lost. "
    "Use a smaller dataset or process it in chunks before trying again.]"
)


def memory_case(binary: Path, execution: Execution, *, active: bool) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory, Events() as events:
        root = Path(directory)
        environment = {
            **os.environ,
            "TMPDIR": directory,
            "MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS": str(root / "cells.jsonl"),
            "MCP_CONSOLE_TEST_MEMORY_ROOT": directory,
            LOADER_VARIABLE: str(build_interposer(root, "worker_memory")),
        }
        descendant = None
        try:
            writable_root = (
                ("--writable-root", directory) if execution == SANDBOXED else ()
            )
            with (
                closing(FifoCheckpoint.create(root / "sampled")) as sampled,
                closing(AllocationProfile(root)) as allocations,
                McpClient(
                    binary,
                    execution.serve("--worker", str(fixtures / "zod"), *writable_root),
                    {
                        **environment,
                        **allocations.environment,
                        LOADER_VARIABLE: environment[LOADER_VARIABLE]
                        + ":"
                        + allocations.environment[LOADER_VARIABLE],
                    },
                ) as client,
            ):
                client.transcript.append(
                    {
                        "scenario": (
                            "The memory guard stops an active worker whose child exceeds the limit."
                            if active
                            else "The memory guard stops an idle worker before the next client call."
                        ),
                        "fixture": (
                            "A custom worker executes Python. Native host observations report "
                            "1024 MiB of RAM and host-controlled resident memory in one "
                            "selected process. The failing sample is 950 MiB; the idle case "
                            "first verifies that 900 MiB survives. No large allocation is made."
                        ),
                    }
                )
                client.initialize_and_list_tools()
                client.send(
                    # fmt: python
                    python=code("""
                        import os
                        import subprocess
                        import sys
                        import tempfile
                        from pathlib import Path

                        session_marker = "original session"
                        print(os.getpid())
                        """),
                )
                worker = host_process_id(
                    int(last_tool_text(client)), client.process.pid
                )
                client.transcript[-1]["result"]["content"][0]["text"] = "<worker pid>\n"
                target = worker
                if not active:
                    allocations.start()
                    (root / "target").write_text(f"{worker} 900", encoding="utf-8")
                    sampled.wait(
                        "first below-limit memory sample", timeout=15, client=client
                    )
                    # The next sample establishes that the preceding scan finished
                    # without terminating the worker. No sleep orders this check.
                    sampled.wait(
                        "second below-limit memory sample", timeout=15, client=client
                    )
                    # The first full scan must fit the existing recovery
                    # allocation budget regardless of unrelated host processes.
                    _, largest = allocations.stop()
                    assert largest <= 64 * 1024, largest
                    client.send(python='assert session_marker == "original session"')
                    assert last_tool_text(client) == "[done]"
                if active:
                    client.send(
                        # fmt: python
                        python=code("""
                            child = subprocess.Popen(
                                [sys.executable, "-c", "import signal; signal.pause()"],
                                stdin=subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL,
                            )
                            print(child.pid)
                            """),
                    )
                    descendant = host_process_id(
                        int(last_tool_text(client)), client.process.pid
                    )
                    target = descendant
                    client.transcript[-1]["result"]["content"][0]["text"] = (
                        "<child pid>\n"
                    )
                    client.send(
                        # fmt: python
                        python=code("""
                            Path(tempfile.gettempdir(), "memory-cell-running").touch()
                            os.read(0, 1)
                            raise AssertionError("blocked cell resumed")
                            """),
                        timeout_ms=0,
                    )
                    assert without_elapsed(last_tool_text(client)) == (
                        "\n[running; poll with an empty send]"
                    )
                    wait_for_worker_file(root, "memory-cell-running", client)
                events.watch_process(worker)
                (root / "target").write_text(f"{target} 950", encoding="utf-8")
                assert events.wait(15) == {worker}, (
                    "memory guard did not stop the worker without a client poll"
                )
                assert client.process.poll() is None, "memory guard stopped the server"
                result = client.send()
                expected = (
                    f"{MEMORY_BANNER}\n"
                    "[worker terminated by signal 9]\n"
                    "[worker stopped: in-memory state lost]"
                )
                if active:
                    expected += "\n[starting new worker]\n[idle]"
                assert result == {
                    "content": [
                        {
                            "type": "text",
                            "text": expected,
                        }
                    ],
                    "isError": True,
                }, result
                assert not process_exists(worker), (
                    "memory guard left the old worker alive"
                )
                if descendant is not None:
                    assert not process_exists(descendant), (
                        "memory guard left the worker's child alive"
                    )
                    descendant = None
                client.send(
                    # fmt: python
                    python=code("""
                        assert "session_marker" not in globals()
                        print("replacement ready")
                        """),
                )
                assert last_tool_text(client) == (
                    "replacement ready\n"
                    if active
                    else "[starting new worker]\nreplacement ready\n"
                )
                return client.finish()
        finally:
            stop_process_id(descendant)


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
@executions(SANDBOXED)
def test_stops_active_worker_for_descendant_memory(
    binary: Path, execution: Execution
) -> Transcript:
    return memory_case(binary, execution, active=True)


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
@executions(DIRECT, SANDBOXED)
def test_stops_idle_worker_and_retains_reason(
    binary: Path, execution: Execution
) -> Transcript:
    return memory_case(binary, execution, active=False)


if __name__ == "__main__":
    run_this_suite(__file__)
