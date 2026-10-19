"""Actual preparation effects, including a subprocess that inherits enforcement."""

import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit


def probe():
    protected = Path(os.environ["PREPARATION_PROTECTED"])
    assert (protected / "readable").read_text() == "host read"
    host = os.environ.get("PREPARATION_HOST") == "1"
    for path in (
        protected / "escape",
        protected / ".agents/control",
        Path(os.environ["PREPARATION_CACHE_CONTROL"]),
        Path(os.environ["PREPARATION_ALIAS"]) / "escape",
    ):
        try:
            path.write_text("changed")
        except PermissionError:
            assert not host
        else:
            assert host, f"preparation wrote outside its grants: {path}"
    cache = Path(os.environ["UV_CACHE_DIR"])
    cache.mkdir(parents=True, exist_ok=True)
    # Refresh a host-created entry as installers do, including atomic replacement.
    cached = cache / "cache-refresh"
    cached.write_text("cache refresh")
    refreshed = cache / f"refresh-{os.getpid()}"
    refreshed.write_text("cache refresh")
    refreshed.replace(cached)
    assert cached.read_text() == "cache refresh"
    temporary = Path(tempfile.gettempdir())
    (temporary / f"preparation-{os.getpid()}").write_text("private temporary write")
    if not host:
        try:
            (temporary.parent / "control-escape").write_text("changed")
        except PermissionError:
            pass
        else:
            raise AssertionError("preparation wrote beside its private temporary data")
        # These launch-control values must be consumed before package code runs.
        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        assert "MCP_CONSOLE_SANDBOX_SETTINGS" not in os.environ
        with socket.socket() as connection:
            connection.settimeout(5)
            try:
                connection.connect(("192.0.2.1", 443))
            except OSError as error:
                assert error.winerror == 10013, error
            else:
                raise AssertionError("preparation bypassed the managed proxy")
    excluded = os.environ.get("PREPARATION_EXCLUDED_CACHE")
    if excluded:
        try:
            Path(excluded).mkdir(parents=True, exist_ok=True)
            Path(excluded, "escape").write_text("excluded cache")
        except PermissionError:
            pass
        else:
            raise AssertionError(
                "explicit filesystem retained a generated cache write grant"
            )
    if not host and not os.environ.get("PREPARATION_NO_DOWNLOADS"):
        proxy = urlsplit(os.environ["HTTP_PROXY"])
        connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)
        connection.request("GET", "http://denied.invalid/preparation")
        response = connection.getresponse()
        assert response.status == 403, (response.status, response.read())
        response.read()
        connection.close()
        allowed = os.environ.get("PREPARATION_ALLOWED_URL")
        if allowed:
            connection = http.client.HTTPConnection(
                proxy.hostname, proxy.port, timeout=10
            )
            connection.request("GET", allowed)
            response = connection.getresponse()
            assert (response.status, response.read()) == (200, b"allowed preparation")
            connection.close()
    (cache / f"probe-{os.getpid()}.json").write_text(
        json.dumps(
            {
                "temporary": str(temporary),
                "host": host,
                "build": os.environ.get("PREPARATION_BUILD") == "1",
                "arguments": sys.argv[1:],
            }
        )
    )


probe()
if "--blocked-child" in sys.argv:
    print("ready", flush=True)
    threading.Event().wait()
elif "--descendant" not in sys.argv:
    blocked = "mcp-console-block==1.0" in sys.argv
    child = subprocess.Popen(
        [
            sys.executable,
            "-I",
            __file__,
            "--blocked-child" if blocked else "--descendant",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
    )
    if blocked:
        assert child.stdout.readline().strip() == b"ready"
        with socket.create_connection(
            ("127.0.0.1", int(os.environ["PREPARATION_GATE"])), timeout=60
        ) as gate:
            gate.sendall(f"{os.getpid()} {child.pid}\n".encode())
            gate.recv(1)
        raise AssertionError("cancelled preparation was released")
    assert child.wait(timeout=30) == 0
