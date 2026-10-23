"""Raw stdin and output pipe operations used by worker scenarios."""

import array
import fcntl
import os
import select
import termios


LARGE_OUTPUT_SIZE = 2 * 1024 * 1024
PIPE_TAIL_PREFIX_SIZE = 16 * 1024


def queued_stdin_bytes() -> int:
    available = array.array("i", [0])
    fcntl.ioctl(0, termios.FIONREAD, available, True)
    return available[0]


def discard_stdin_bytes(length: int) -> None:
    remaining = length
    while remaining:
        chunk = os.read(0, remaining)
        assert chunk, "worker stdin closed during the pending-write probe"
        remaining -= len(chunk)


def probe_pending_stdin(expected_bytes: int, consumed_bytes: int) -> tuple[int, int]:
    readable, _, _ = select.select([0], [], [], 15)
    assert readable, "Zod did not receive worker stdin"
    queued_bytes = queued_stdin_bytes()
    assert 0 < queued_bytes <= expected_bytes - consumed_bytes, (
        queued_bytes,
        consumed_bytes,
        expected_bytes,
    )
    if consumed_bytes + queued_bytes == expected_bytes:
        return consumed_bytes, queued_bytes

    discard_stdin_bytes(queued_bytes)
    consumed_bytes += queued_bytes
    readable, _, _ = select.select([0], [], [], 15)
    assert readable, "worker stdin writer did not resume after space became available"
    queued_bytes = queued_stdin_bytes()
    assert 0 < queued_bytes <= expected_bytes - consumed_bytes, (
        queued_bytes,
        consumed_bytes,
        expected_bytes,
    )
    return consumed_bytes, queued_bytes


def write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        written = os.write(descriptor, remaining)
        remaining = remaining[written:]


def emit_large_output(descriptor: int, prefix: bytes) -> None:
    write_all(descriptor, prefix + (b"x" * LARGE_OUTPUT_SIZE))
    # Completing a second pipe-sized write proves the preceding payload was read.
    write_all(descriptor, b"y" * LARGE_OUTPUT_SIZE)


def fill_nonblocking(descriptor: int, data: bytes) -> int:
    assert len(data) > PIPE_TAIL_PREFIX_SIZE
    # Cover the retained preview tail even when the pipe accepts few additional
    # bytes. The nonblocking remainder still measures the native pipe fill.
    write_all(descriptor, data[:PIPE_TAIL_PREFIX_SIZE])
    flags = fcntl.fcntl(descriptor, fcntl.F_GETFL)
    fcntl.fcntl(descriptor, fcntl.F_SETFL, flags | os.O_NONBLOCK)
    written = PIPE_TAIL_PREFIX_SIZE
    try:
        while written < len(data):
            try:
                written += os.write(descriptor, data[written:])
            except BlockingIOError:
                break
    finally:
        fcntl.fcntl(descriptor, fcntl.F_SETFL, flags)
    assert written > 0
    return written
