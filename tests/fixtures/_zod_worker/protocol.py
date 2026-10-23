"""Split-pipe worker sideband adoption and JSONL messages."""

import fcntl
import json
import os
import stat
from enum import StrEnum
from typing import Any, TextIO


READ_FD_ENV = "MCP_CONSOLE_SIDEBAND_READ_FD"
WRITE_FD_ENV = "MCP_CONSOLE_SIDEBAND_WRITE_FD"


class ConsoleKind(StrEnum):
    OUTPUT = "console_output"
    DIAGNOSTIC = "console_diagnostic"


def open_sideband() -> tuple[TextIO, TextIO]:
    assert "MCP_CONSOLE_SIDEBAND_FD" not in os.environ
    read_fd = int(os.environ.pop(READ_FD_ENV))
    write_fd = int(os.environ.pop(WRITE_FD_ENV))
    assert read_fd != write_fd and min(read_fd, write_fd) > 2
    for descriptor, direction in ((read_fd, os.O_RDONLY), (write_fd, os.O_WRONLY)):
        assert stat.S_ISFIFO(os.fstat(descriptor).st_mode)
        assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == direction
        os.set_inheritable(descriptor, False)
    return (
        os.fdopen(read_fd, "r", encoding="utf-8"),
        os.fdopen(write_fd, "w", encoding="utf-8"),
    )


def close_sideband(reader: TextIO, writer: TextIO) -> None:
    reader.close()
    writer.close()


def send(stream: TextIO, message: dict[str, Any]) -> None:
    stream.write(json.dumps(message, separators=(",", ":")) + "\n")
    stream.flush()


def send_batch(stream: TextIO, messages: list[dict[str, Any]]) -> None:
    stream.writelines(
        json.dumps(message, separators=(",", ":")) + "\n" for message in messages
    )
    stream.flush()


def send_output(
    stream: TextIO,
    data: str,
    kind: ConsoleKind = ConsoleKind.OUTPUT,
) -> None:
    send(
        stream,
        {
            "kind": kind,
            "data": data,
        },
    )


def wait_for_server_to_process_sideband(reader: TextIO, writer: TextIO) -> None:
    """Round-trip through the server after preceding sideband frames."""
    send(
        writer,
        {
            "kind": "resolve_python_version",
            "request": {"constraints": [">=3.11"]},
        },
    )
    response = json.loads(reader.readline())
    assert response["kind"] == "python_version_resolution_failed", response
