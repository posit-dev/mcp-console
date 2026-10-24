"""Exercise ordinary host sockets from public kernel code in a macOS sandbox."""

import errno
import json
import os
import socket
import tempfile
from pathlib import Path

endpoints = json.loads(os.environ["MCP_CONSOLE_TEST_JUPYTER_ENDPOINTS"])
with tempfile.TemporaryDirectory(prefix="") as directory:
    private = Path(directory) / "s"
    with (
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener,
        socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection,
    ):
        listener.bind(str(private))
        listener.listen()
        connection.connect(str(private))
        accepted, _ = listener.accept()
        with accepted:
            message = b"private IPC"
            connection.sendall(message)
            with accepted.makefile("rb") as stream:
                assert stream.read(len(message)) == message
    print("private Unix: roundtrip", flush=True)

host_alias = Path(tempfile.gettempdir()) / "alias"
host_alias.symlink_to(endpoints["unix_path"])
for label, family, address in (
    ("host TCP", socket.AF_INET, ("127.0.0.1", endpoints["tcp_port"])),
    ("external TCP", socket.AF_INET, ("198.51.100.1", 80)),
    ("host Unix", socket.AF_UNIX, endpoints["unix_path"]),
    ("host Unix through private symlink", socket.AF_UNIX, str(host_alias)),
):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(address)
    except OSError as error:
        assert error.errno in {errno.EPERM, errno.EACCES}, (label, error)
        print(f"{label}: {error}", flush=True)
    else:
        raise AssertionError(f"{label} unexpectedly connected outside the sandbox")
host_alias.unlink()
