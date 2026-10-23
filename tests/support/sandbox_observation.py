"""Observe native descendant registration in the macOS runner."""

import os
import select
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

TIMEOUT = 10


class RunnerObservations:
    def __init__(self, path: Path) -> None:
        os.mkfifo(path)
        self.path = path
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        self.buffer = bytearray()

    def close(self) -> None:
        os.close(self.descriptor)

    def wait_for(self, process_id: int, process: subprocess.Popen) -> None:
        deadline = time.monotonic() + TIMEOUT
        while True:
            newline = self.buffer.find(b"\n")
            if newline >= 0:
                observed = int(self.buffer[:newline])
                del self.buffer[: newline + 1]
                if observed == process_id:
                    return
                continue

            assert process.poll() is None, (
                "mcp-console stopped before runner observation"
            )
            remaining = deadline - time.monotonic()
            assert remaining > 0, (
                f"runner did not observe sandbox descendant {process_id}"
            )
            readable, _, _ = select.select([self.descriptor], [], [], remaining)
            assert readable, f"runner did not observe sandbox descendant {process_id}"
            chunk = os.read(self.descriptor, 4096)
            assert chunk, "runner observation stream closed"
            self.buffer.extend(chunk)


def build_runner_observation_interposer(directory: Path) -> Path:
    library = directory / "manager-observation-interposer.dylib"
    source = (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "native"
        / "manager_observation_interposer.c"
    )
    architectures = []
    if os.uname().machine == "arm64":
        # The loader variable also reaches host helpers before the runner.
        # Host executables can use the arm64e ABI.
        architectures = ["-arch", "arm64", "-arch", "arm64e"]
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            "-dynamiclib",
            *architectures,
            "-o",
            library,
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


@contextmanager
def observed_sandbox_descendants(
    directory: Path, environment: dict[str, str]
) -> Iterator[Callable[[int, subprocess.Popen], None]]:
    """Keep detached children discoverable before a test orphans them.

    Linux owns the entire PID namespace; macOS must first register the child.
    """
    if sys.platform == "linux":
        yield lambda process_id, process: None
        return
    observations = RunnerObservations(directory / "runner-observations")
    try:
        library = build_runner_observation_interposer(directory)
        environment["DYLD_INSERT_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_MANAGER_OBSERVATIONS"] = str(observations.path)
        yield observations.wait_for
    finally:
        observations.close()
