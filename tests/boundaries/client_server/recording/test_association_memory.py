"""Settled source associations do not retain historical call state."""

import mmap
import os
import struct
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import LINUX_NATIVE, requires
from support.suites import run_this_suite


@requires(LINUX_NATIVE)
@executions(DIRECT)
def test_settled_calls_release_association_memory(
    binary: Path, execution: Execution
) -> Transcript:
    worker = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        heap = workspace / "heap"
        heap.write_bytes(bytes(8))
        with (
            heap.open("r+b") as stream,
            mmap.mmap(stream.fileno(), 8) as mapping,
            McpClient(
                binary,
                execution.serve("--worker", str(worker)),
                {
                    **os.environ,
                    LOADER_VARIABLE: str(
                        build_interposer(workspace, "association_allocations")
                    ),
                    "MCP_CONSOLE_TEST_ASSOCIATION_HEAP": str(heap),
                    # Measure live state rather than per-thread allocator caches.
                    "MALLOC_ARENA_MAX": "1",
                    "GLIBC_TUNABLES": "glibc.malloc.tcache_count=0",
                },
                current_directory=workspace,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.send(r="echo ready")
            # Warm allocator/thread caches with an equal-sized batch before
            # checking that settled-call state stops growing.
            for _ in range(16384):
                client.send(session="default")
            client.request("ping")
            baseline = struct.unpack_from("=Q", mapping)[0]
            for arguments in ({}, {"requirements": {"action": "get"}}):
                for _ in range(16384):
                    result = client.send(session="default", **arguments)
                    assert not result.get("isError"), result
                client.request("ping")
                growth = struct.unpack_from("=Q", mapping)[0] - baseline
                assert growth <= 256 * 1024, growth
            client.finish()
            return [
                *client.transcript[:3],
                {
                    "association memory": "settled calls released while the worker survives"
                },
            ]


if __name__ == "__main__":
    run_this_suite(__file__)
