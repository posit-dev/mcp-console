import errno
import os
import select
import time
from pathlib import Path
from collections.abc import Callable
from concurrent.futures import CancelledError
from contextlib import nullcontext
from typing import Self

from support.client import McpClient
from support.events import Events


class FifoCheckpoint:
    def __init__(self, path: Path, *, create: bool) -> None:
        self.path = path
        if create:
            os.mkfifo(path)
        # Keep a writer open so an early release cannot strand a later reader
        # in its blocking open.
        self.descriptor = os.open(path, os.O_RDWR | os.O_NONBLOCK)

    @classmethod
    def create(cls, path: Path) -> Self:
        return cls(path, create=True)

    @classmethod
    def attach(cls, path: Path) -> Self:
        return cls(path, create=False)

    def close(self) -> None:
        if self.descriptor is not None:
            descriptor, self.descriptor = self.descriptor, None
            os.close(descriptor)

    def wait(
        self,
        description: str | None = None,
        timeout: float = 10,
        *,
        client: McpClient | None = None,
    ) -> None:
        readable, _, _ = select.select([self.descriptor], [], [], timeout)
        assert readable, checkpoint_evidence(
            f"checkpoint was not reached: {description or self.path.name}", client
        )
        assert os.read(self.descriptor, 1) == b"1"

    def release(self) -> None:
        assert os.write(self.descriptor, b"1") == 1


def checkpoint_evidence(description: str, client: McpClient | None) -> str:
    if client is None:
        return description
    last = client.transcript[-1] if client.transcript else None
    tail = client.stderr.buffer[-4096:].decode("utf-8", errors="replace")
    return f"{description}: last MCP={repr(last)[-4096:]}, process={client.process.poll()!r}, stderr={tail!r}"


def wait_for_checkpoint(
    discover: Callable[[], Path | None],
    description: str,
    *,
    root: Path,
    client: McpClient | None = None,
    timeout: float = 10,
    recursive: bool = False,
    events: Events | None = None,
) -> Path:
    deadline = time.monotonic() + timeout
    with Events() if events is None else nullcontext(events) as events:
        assert client is None or client.process.poll() is None, checkpoint_evidence(
            description, client
        )
        if (
            client is not None
            and events.native
            and client.process.pid not in events.processes
        ):
            try:
                events.watch_process(client.process.pid)
            except ProcessLookupError as error:
                raise AssertionError(
                    checkpoint_evidence(description, client)
                ) from error
        while True:
            if events.cancelled:
                raise CancelledError(checkpoint_evidence(description, client))
            # Owner exit takes precedence over stale markers left by that owner.
            assert client is None or client.process.poll() is None, checkpoint_evidence(
                description, client
            )
            events.watch_tree(root, recursive=recursive)
            if path := discover():
                return path
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(checkpoint_evidence(description, client))
            try:
                events.wait(remaining)
            except CancelledError as error:
                raise CancelledError(
                    checkpoint_evidence(description, client)
                ) from error


def wait_for_path(
    path: Path,
    description: str,
    *,
    client: McpClient | None = None,
    timeout: float = 10,
    events: Events | None = None,
) -> None:
    wait_for_checkpoint(
        lambda: path if path.exists() else None,
        f"{description}: {path}",
        root=path.parent,
        client=client,
        timeout=timeout,
        events=events,
    )


def wait_for_worker_file(root: Path, name: str, client: McpClient) -> Path:
    def discover() -> Path | None:
        paths = list(root.glob(f"**/{name}"))
        if paths:
            assert len(paths) == 1, paths
            return paths[0]
        return None

    return wait_for_checkpoint(
        discover,
        f"worker checkpoint {name}: {root}",
        root=root,
        recursive=True,
        client=client,
    )


def release_fixture_checkpoint(
    path: Path,
    *,
    token: bytes = b"1",
    timeout: float = 10,
    client: McpClient | None = None,
) -> None:
    """Wait for the fixture's reader, without opening a read endpoint ourselves."""
    assert len(token) == 1
    deadline = time.monotonic() + timeout
    while True:
        assert client is None or client.process.poll() is None, checkpoint_evidence(
            f"FIFO reader: {path}", client
        )
        try:
            descriptor = os.open(path, os.O_WRONLY | os.O_NONBLOCK)
            break
        except OSError as error:
            if error.errno != errno.ENXIO:
                raise
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(checkpoint_evidence(f"FIFO reader: {path}", client))
        time.sleep(min(0.01, remaining))
    try:
        assert os.write(descriptor, token) == 1
    finally:
        os.close(descriptor)


def release_partial_sideband(
    marker: Path, *, timeout: float = 10, client: McpClient | None = None
) -> None:
    release = marker.with_name("zod-release-partial-sideband")
    release_fixture_checkpoint(release, token=b"x", timeout=timeout, client=client)
