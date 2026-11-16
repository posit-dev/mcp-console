"""Integrator control-socket client shared by boundary harnesses."""

import json
import socket
import time
from pathlib import Path
from typing import Any, Self

from support.client import McpClient, TextReader


class ControlClient:
    def __init__(self, path: Path) -> None:
        self.socket = socket.socket(socket.AF_UNIX)
        self.socket.settimeout(30)
        self.socket.connect(str(path))
        self.reader = TextReader(self.socket)
        self.transcript: list[dict[str, Any]] = []

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        self.reader.close()

    def start(self, **request: Any) -> dict[str, Any]:
        request = {"id": str(len(self.transcript) + 1), **request}
        self.socket.sendall(json.dumps(request).encode() + b"\n")
        entry = {"request": request}
        self.transcript.append(entry)
        return entry

    def receive(
        self,
        entry: dict[str, Any],
        *,
        invalid: bool = False,
        timeout: float = 60,
    ) -> dict[str, Any]:
        response = json.loads(self.reader.readline(timeout=timeout))
        assert response["id"] == (None if invalid else entry["request"]["id"]), response
        assert response.keys() in ({"id", "result"}, {"id", "error"}), response
        entry["response"] = response
        return response

    def request(
        self, *, response_timeout: float = 60, **request: Any
    ) -> dict[str, Any]:
        return self.receive(self.start(**request), timeout=response_timeout)

    def wait_for_idle(self, client: McpClient, *, session: str = "default") -> None:
        """Observe delivery settlement before a nonwaiting integrator operation."""
        deadline = client.response_deadline()
        first = len(self.transcript)
        while True:
            remaining = deadline - time.monotonic()
            assert remaining > 0, "MCP response delivery did not settle"
            response = self.request(
                op="status", session=session, response_timeout=remaining
            )
            status = response["result"]
            if status["state"] == "idle":
                assert status == {"state": "idle", "idle": True, "evaluation": None}, (
                    status
                )
                break
            assert status["state"] in {"busy", "awaiting_result"}, status
        # Every intermediate observation was checked. Retain the exact final
        # status while keeping scheduling-dependent poll counts out of snapshots.
        observed = self.transcript[first]
        observed["response"] = {**response, "id": observed["request"]["id"]}
        self.transcript[first:] = [observed]
