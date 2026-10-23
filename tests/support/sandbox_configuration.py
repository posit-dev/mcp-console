"""Explicit proxy inputs required by the pinned native runner."""

import socket
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager

NATIVE_PROXY = {
    "enabled": True,
    "enableSocks5": True,
    "enableSocks5Udp": False,
    "allowUpstreamProxy": False,
    "dangerouslyAllowAllUnixSockets": False,
    "mode": "full",
    "allowLocalBinding": False,
}


@contextmanager
def host_tcp_ports() -> Iterator[list[int]]:
    # The Linux sandbox has up to two loopback proxy listeners in its own network
    # namespace. Three host listeners leave a port free of either proxy endpoint.
    with ExitStack() as stack:
        listeners = [stack.enter_context(socket.socket()) for _ in range(3)]
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
        yield [listener.getsockname()[1] for listener in listeners]
