#!/bin/sh
""":"
exec "${MCP_CONSOLE_TEST_PYTHON:?}" "$0" "$@"
":"""

import codecs
import json
import os
import select
import stat
import subprocess
import tempfile
from pathlib import Path
from typing import Any, BinaryIO, TextIO

READ_FD_ENV = "MCP_CONSOLE_SIDEBAND_READ_FD"
WRITE_FD_ENV = "MCP_CONSOLE_SIDEBAND_WRITE_FD"
WORKER_ENV = "MCP_CONSOLE_MITM_WORKER_COMMAND"
CAPTURE_STDIN_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_STDIN_CLOSE"
CAPTURE_WORKER_SIDEBAND_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_WORKER_SIDEBAND_CLOSE"
CAPTURE_NAME = "mcp-console-worker-wire.jsonl"
BUFFER_SIZE = 64 * 1024


def take_sideband() -> tuple[BinaryIO, BinaryIO]:
    assert "MCP_CONSOLE_SIDEBAND_FD" not in os.environ
    read_fd = int(os.environ.pop(READ_FD_ENV))
    write_fd = int(os.environ.pop(WRITE_FD_ENV))
    assert read_fd != write_fd
    for descriptor in (read_fd, write_fd):
        assert stat.S_ISFIFO(os.fstat(descriptor).st_mode)
        os.set_inheritable(descriptor, False)
    return os.fdopen(read_fd, "rb", buffering=0), os.fdopen(write_fd, "wb", buffering=0)


def write_all(descriptor: int, data: bytes) -> None:
    remaining = memoryview(data)
    while remaining:
        remaining = remaining[os.write(descriptor, remaining) :]


def send(endpoint: BinaryIO, message: dict[str, Any]) -> None:
    frame = json.dumps(message, separators=(",", ":")).encode("utf-8")
    write_all(endpoint.fileno(), frame + b"\n")


def record(stream: TextIO, event: dict[str, Any]) -> None:
    stream.write(json.dumps(event, separators=(",", ":")) + "\n")
    stream.flush()


def proxy(
    relay: tuple[BinaryIO, BinaryIO],
    worker: tuple[BinaryIO, BinaryIO],
    stdout: BinaryIO,
    stderr: BinaryIO,
    capture: TextIO,
    capture_stdin_close: bool,
    capture_worker_sideband_close: bool,
) -> None:
    streams = {
        stdout.fileno(): ("stdout", 1),
        stderr.fileno(): ("stderr", 2),
    }
    decoders = {
        descriptor: codecs.getincrementaldecoder("utf-8")(errors="replace")
        for descriptor in streams
    }
    pending: dict[str, list[str]] = {"stdout": [], "stderr": []}
    relay_descriptor = relay[0].fileno()
    worker_descriptor = worker[0].fileno()
    active = {relay_descriptor, worker_descriptor, *streams}
    buffers = {"relay": bytearray(), "worker": bytearray()}

    def read_stream(descriptor: int) -> None:
        chunk = os.read(descriptor, BUFFER_SIZE)
        name, destination = streams[descriptor]
        if chunk:
            write_all(destination, chunk)
            text = decoders[descriptor].decode(chunk)
        else:
            active.remove(descriptor)
            text = decoders[descriptor].decode(b"", final=True)
        if text:
            pending[name].append(text)

    def read_ready_streams() -> None:
        descriptors = [descriptor for descriptor in streams if descriptor in active]
        if not descriptors:
            return
        ready, _, _ = select.select(descriptors, [], [], 0)
        for descriptor in streams:
            if descriptor in ready:
                read_stream(descriptor)

    def flush_streams() -> None:
        event = {
            name: "".join(pending[name])
            for name in ("stdout", "stderr")
            if pending[name]
        }
        if event:
            record(capture, event)
            for name in event:
                pending[name].clear()

    def forward_frames(direction: str, destination: BinaryIO) -> bool:
        buffer = buffers[direction]
        shutdown = False
        while b"\n" in buffer:
            frame, _, remainder = buffer.partition(b"\n")
            buffers[direction] = buffer = bytearray(remainder)
            message = json.loads(frame.decode("utf-8"))
            assert isinstance(message, dict), message
            if direction == "worker":
                read_ready_streams()
                flush_streams()
            record(capture, {direction: message})
            frame_is_shutdown = direction == "relay" and message == {"kind": "shutdown"}
            if frame_is_shutdown and capture_stdin_close:
                assert os.read(0, 1) == b"", "worker stdin contained data at shutdown"
                record(capture, {"stdin": {"closed": True}})
            send(destination, message)
            shutdown = shutdown or frame_is_shutdown
        return shutdown

    while worker_descriptor in active or any(
        descriptor in active for descriptor in streams
    ):
        ready, _, _ = select.select(active, [], [])
        for descriptor in streams:
            if descriptor in ready:
                read_stream(descriptor)

        if worker_descriptor in ready:
            read_ready_streams()
            chunk = worker[0].read(BUFFER_SIZE)
            if chunk:
                buffers["worker"].extend(chunk)
                forward_frames("worker", relay[1])
            else:
                active.remove(worker_descriptor)
                if capture_worker_sideband_close:
                    record(capture, {"worker_sideband": {"closed": True}})
                relay[1].close()

        if relay_descriptor in ready:
            chunk = relay[0].read(BUFFER_SIZE)
            if chunk:
                buffers["relay"].extend(chunk)
                shutdown = forward_frames("relay", worker[1])
            else:
                shutdown = True
            if shutdown:
                active.remove(relay_descriptor)
                worker[1].close()

    assert not buffers["relay"], "relay stopped during a sideband frame"
    assert not buffers["worker"], "worker stopped during a sideband frame"
    flush_streams()
    for stream in (*relay, *worker):
        stream.close()


def main() -> None:
    relay = take_sideband()
    worker_read, proxy_write = os.pipe()
    proxy_read, worker_write = os.pipe()
    proxy_endpoint = (
        os.fdopen(proxy_read, "rb", buffering=0),
        os.fdopen(proxy_write, "wb", buffering=0),
    )
    environment = os.environ.copy()
    command = json.loads(environment.pop(WORKER_ENV))
    capture_stdin_close = environment.pop(CAPTURE_STDIN_CLOSE_ENV, None) == "1"
    capture_worker_sideband_close = (
        environment.pop(CAPTURE_WORKER_SIDEBAND_CLOSE_ENV, None) == "1"
    )
    environment[READ_FD_ENV] = str(worker_read)
    environment[WRITE_FD_ENV] = str(worker_write)
    process = subprocess.Popen(
        command,
        env=environment,
        pass_fds=(worker_read, worker_write),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    os.close(worker_read)
    os.close(worker_write)
    assert process.stdout is not None
    assert process.stderr is not None

    with (
        tempfile.TemporaryDirectory(
            prefix="worker-wire-", dir=os.environ["TMPDIR"]
        ) as directory,
        Path(directory, CAPTURE_NAME).open("w", encoding="utf-8") as capture,
    ):
        proxy(
            relay,
            proxy_endpoint,
            process.stdout,
            process.stderr,
            capture,
            capture_stdin_close,
            capture_worker_sideband_close,
        )
    process.stdout.close()
    process.stderr.close()
    raise SystemExit(process.wait())


if __name__ == "__main__":
    main()
