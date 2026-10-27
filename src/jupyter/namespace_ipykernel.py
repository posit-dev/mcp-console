"""Run stock ipykernel inside the native runner's private Linux network namespace.

libzmq 4.3.5 enumerates interfaces even for numeric TCP bind addresses. That
uses AF_NETLINK, which the pinned sandbox denies. A wildcard bind skips this
enumeration. It is safe here only because all interfaces belong to the private
namespace, with no host loopback or external route. Never use this on the host.
"""

import runpy
from typing import Any

import zmq


class NamespaceSocket(zmq.Socket):
    def bind(self, addr: str | bytes) -> Any:
        if isinstance(addr, bytes):
            addr = addr.decode("utf-8")
        if addr.startswith("tcp://127.0.0.1:"):
            addr = "tcp://*:" + addr.removeprefix("tcp://127.0.0.1:")
        return super().bind(addr)


zmq.Context._socket_class = NamespaceSocket

if __name__ == "__main__":
    runpy.run_module("ipykernel_launcher", run_name="__main__")
