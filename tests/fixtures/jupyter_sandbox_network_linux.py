"""Probe host isolation from a kernel in its private Linux network namespace."""

import errno
import http.client
import json
import os
import socket
from contextlib import closing
from urllib.parse import urlsplit

endpoints = json.loads(os.environ["MCP_CONSOLE_TEST_JUPYTER_ENDPOINTS"])
for label, family, address, expected in (
    (
        "host TCP",
        socket.AF_INET,
        ("127.0.0.1", endpoints["tcp_port"]),
        errno.ECONNREFUSED,
    ),
    ("external TCP", socket.AF_INET, ("198.51.100.1", 80), errno.ENETUNREACH),
    ("host Unix", socket.AF_UNIX, endpoints["unix_path"], errno.EPERM),
):
    try:
        with socket.socket(family, socket.SOCK_STREAM) as connection:
            connection.settimeout(1)
            connection.connect(address)
    except OSError as error:
        assert error.errno == expected, (label, error)
        print(f"{label}: {error}", flush=True)
    else:
        raise AssertionError(f"{label} unexpectedly connected outside the sandbox")

proxy = urlsplit(os.environ["HTTP_PROXY"])
assert proxy.hostname == "127.0.0.1", proxy
for label, method, destination in (
    ("external HTTP", "GET", "http://198.51.100.1/"),
    ("external CONNECT", "CONNECT", "198.51.100.1:80"),
    ("host CONNECT", "CONNECT", f"127.0.0.1:{endpoints['tcp_port']}"),
):
    with closing(
        http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=2)
    ) as connection:
        connection.request(method, destination)
        response = connection.getresponse()
        assert response.status == 403, (label, response.status, response.read())
        print(f"{label} through proxy: denied (403)", flush=True)
