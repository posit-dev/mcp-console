"""Jupyter transport for a process-owned native Console controller.

The shim never evaluates source. The native controller owns preparation,
execution, interruption, worker retirement, and recording settlement.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import signal
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    import zmq
    import zmq.asyncio
    from jupyter_client import protocol_version
    from jupyter_client.session import Session
except ImportError as error:
    raise SystemExit(
        f"Jupyter transport dependencies are unavailable in {sys.executable}: {error}. "
        "Install 'mcp-console[jupyter]' in that Python environment and select it "
        "with --jupyter-python."
    ) from error


@dataclass
class Execution:
    identities: list[bytes]
    request: dict[str, Any]
    count: int
    error: dict[str, Any] | None = None

    @property
    def id(self) -> str:
        return self.request["header"]["msg_id"]

    @property
    def silent(self) -> bool:
        return self.request["content"].get("silent", False)


class Kernel:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        connection = json.loads(Path(options.connection_file).read_text())
        if connection["transport"] != "tcp":
            raise ValueError("kernel requires a TCP Jupyter connection file")
        self.session = Session(
            key=connection["key"].encode(),
            signature_scheme=connection["signature_scheme"],
            username="kernel",
        )
        self.context = zmq.asyncio.Context()
        self.sockets = {}
        self.poller = zmq.asyncio.Poller()
        for channel, kind in (
            ("shell", zmq.ROUTER),
            ("control", zmq.ROUTER),
            ("stdin", zmq.ROUTER),
            ("iopub", zmq.XPUB),
            ("hb", zmq.REP),
        ):
            socket = self.context.socket(kind)
            if channel == "iopub":
                socket.setsockopt(zmq.XPUB_VERBOSE, 1)
                socket.setsockopt(zmq.SNDHWM, 100000)
            socket.bind(f"tcp://{connection['ip']}:{connection[channel + '_port']}")
            self.sockets[channel] = socket
            self.poller.register(socket, zmq.POLLIN)
        self.wake = self.context.socket(zmq.PAIR)
        self.wake.bind("inproc://console-kernel-events")
        self.notify = self.context.socket(zmq.PAIR)
        self.notify.connect("inproc://console-kernel-events")
        self.poller.register(self.wake, zmq.POLLIN)
        self.events: deque[dict[str, Any]] = deque()
        self.queue: deque[tuple[list[bytes], dict[str, Any]]] = deque()
        self.controls: dict[str, tuple[list[bytes], dict[str, Any]]] = {}
        self.input_commands: set[str] = set()
        self.active: Execution | None = None
        self.previous: Execution | None = None
        self.input_request: tuple[str, list[bytes], str] | None = None
        self.shutdown: tuple[list[bytes], dict[str, Any]] | None = None
        self.shutdown_reply: dict[str, Any] | None = None
        self.ready: dict[str, Any] | None = None
        self.count = 0
        self.history: list[list[Any]] = []
        self.stop = asyncio.Event()
        self.closing = False

    async def send(
        self,
        channel: str,
        kind: str,
        content: dict[str, Any],
        parent: dict[str, Any] | None = None,
        identities: list[bytes] | None = None,
    ) -> dict[str, Any]:
        message = self.session.msg(kind, content, parent=parent)
        identity = kind.encode() if channel == "iopub" else identities
        await self.sockets[channel].send_multipart(
            self.session.serialize(message, ident=identity)
        )
        return message

    async def receive(
        self, channel: str, flags: int = 0
    ) -> tuple[list[bytes], dict[str, Any]] | None:
        frames = await self.sockets[channel].recv_multipart(flags=flags)
        try:
            identities, message = self.session.feed_identities(frames)
            return identities, self.session.deserialize(message)
        except ValueError as error:
            print(f"kernel {channel}: {error}", file=sys.stderr)
            return None

    async def status(self, request: dict[str, Any], state: str) -> None:
        await self.send("iopub", "status", {"execution_state": state}, request)

    async def command(self, command: dict[str, Any]) -> None:
        assert self.backend.stdin is not None
        self.backend.stdin.write(
            (json.dumps(command, ensure_ascii=False) + "\n").encode()
        )
        await self.backend.stdin.drain()

    async def post_event(self, event: dict[str, Any]) -> None:
        if self.closing:
            return
        self.events.append(event)
        # Notifications coalesce; a queued wake already makes all events visible.
        try:
            await self.notify.send(b"", flags=zmq.DONTWAIT)
        except zmq.Again:
            pass

    async def read_backend(self) -> None:
        assert self.backend.stdout is not None
        pending = bytearray()
        try:
            while chunk := await self.backend.stdout.read(65536):
                if self.closing:
                    continue
                pending.extend(chunk)
                while (end := pending.find(b"\n")) >= 0:
                    line = bytes(pending[:end])
                    del pending[: end + 1]
                    await self.post_event(json.loads(line))
            if pending and not self.closing:
                raise ValueError(
                    "kernel controller closed with an incomplete JSONL message"
                )
            await self.post_event(
                {"event": "exited", "returncode": await self.backend.wait()}
            )
        # Report reader-task failures to the transport owner, which retires the
        # native process before propagating the error.
        except Exception as error:  # noqa: BLE001
            await self.post_event({"event": "failed", "error": error})

    async def control(self, identities: list[bytes], request: dict[str, Any]) -> None:
        kind = request["header"]["msg_type"]
        if self.shutdown is not None:
            return
        if kind not in {"interrupt_request", "shutdown_request"}:
            return
        request_id = request["header"]["msg_id"]
        if kind == "shutdown_request":
            self.shutdown = identities, request
            self.poller.unregister(self.sockets["shell"])
        self.input_request = None
        self.controls[request_id] = identities, request
        await self.command({"id": request_id, "op": kind.removesuffix("_request")})

    async def stdin(self, identities: list[bytes], request: dict[str, Any]) -> None:
        if request["header"]["msg_type"] != "input_reply" or self.input_request is None:
            return
        execution_id, expected_identities, input_id = self.input_request
        parent = request["parent_header"].get("msg_id")
        # jupyter_client sends parentless replies; a supplied parent must match.
        if identities != expected_identities or (
            parent is not None and parent != input_id
        ):
            return
        self.input_request = None
        request_id = request["header"]["msg_id"]
        self.input_commands.add(request_id)
        await self.command(
            {
                "id": request_id,
                "op": "input",
                "execution_id": execution_id,
                # Jupyter supplies a line. Console accepts exact UTF-8 bytes.
                "data": request["content"]["value"] + "\n",
            }
        )

    async def backend_event(self, event: dict[str, Any]) -> None:
        kind = event["event"]
        if kind == "ready":
            assert self.ready is None and event["language"] == self.options.language
            self.ready = event
            return
        if kind == "failed":
            raise event["error"]
        if kind == "exited":
            if self.shutdown_reply is None or self.shutdown is None:
                raise RuntimeError(
                    f"kernel controller exited unexpectedly ({event['returncode']})"
                )
            identities, request = self.shutdown
            reply = {
                "status": "ok"
                if self.shutdown_reply["ok"] and event["returncode"] == 0
                else "error",
                "restart": request["content"]["restart"],
            }
            await self.send("control", "shutdown_reply", reply, request, identities)
            self.stop.set()
            return
        if kind == "reply":
            if event["id"] in self.input_commands:
                self.input_commands.remove(event["id"])
                if not event["ok"]:
                    print(f"kernel input: {event['error']}", file=sys.stderr)
                return
            identities, request = self.controls.pop(event["id"])
            if request["header"]["msg_type"] == "shutdown_request":
                self.shutdown_reply = event
            else:
                reply = (
                    {"status": "ok"}
                    if event["ok"]
                    else self.error("ConsoleError", event["error"])
                )
                await self.send(
                    "control", "interrupt_reply", reply, request, identities
                )
            return
        execution = (
            self.active
            if self.active and self.active.id == event["id"]
            else self.previous
        )
        assert execution is not None and execution.id == event["id"], (
            "unknown execution event"
        )
        if kind == "input_ended":
            if self.input_request and self.input_request[0] == execution.id:
                self.input_request = None
        elif kind == "input":
            assert execution.request["content"].get("allow_stdin", True)
            if self.shutdown is None:
                assert self.input_request is None, (
                    "managed input is already outstanding"
                )
                message = await self.send(
                    "stdin",
                    "input_request",
                    {"prompt": event["prompt"], "password": False},
                    execution.request,
                    execution.identities,
                )
                self.input_request = (
                    execution.id,
                    execution.identities,
                    message["header"]["msg_id"],
                )
        elif kind == "complete":
            assert execution is self.active
            if event["status"] == "error":
                assert execution.error is not None
                reply = {"status": "error", **execution.error}
            else:
                assert event["status"] == "ok" and execution.error is None
                reply = {
                    "status": "ok",
                    "user_expressions": {
                        key: self.error(
                            "NotImplementedError", "user_expressions are not supported"
                        )
                        for key in execution.request["content"].get(
                            "user_expressions", {}
                        )
                    },
                }
            reply["execution_count"] = execution.count
            await self.send(
                "shell", "execute_reply", reply, execution.request, execution.identities
            )
            await self.status(execution.request, "idle")
            self.input_request = None
            self.active, self.previous = None, execution
            if execution.error and execution.request["content"].get(
                "stop_on_error", True
            ):
                await self.abort_queued()
        elif kind == "error":
            assert execution.error is None
            execution.error = {
                key: event[key] for key in ("ename", "evalue", "traceback")
            }
            if not execution.silent:
                await self.send("iopub", "error", execution.error, execution.request)
        elif not execution.silent:
            if kind == "stream":
                content = {"name": event["name"], "text": event["text"]}
                message_type = "stream"
            elif kind == "display":
                content = {"data": {event["mime_type"]: event["data"]}, "metadata": {}}
                message_type = "display_data"
            elif kind == "result":
                content = {
                    "data": {"text/plain": event["text"].removesuffix("\n")},
                    "metadata": {},
                    "execution_count": execution.count,
                }
                message_type = "execute_result"
            else:
                raise ValueError(f"unknown controller event: {kind}")
            await self.send("iopub", message_type, content, execution.request)

    async def abort_queued(self) -> None:
        while self.sockets["shell"].getsockopt(zmq.EVENTS) & zmq.POLLIN:
            if request := await self.receive("shell", flags=zmq.DONTWAIT):
                self.queue.append(request)
        retained = deque()
        while self.queue:
            identities, request = self.queue.popleft()
            if request["header"]["msg_type"] != "execute_request":
                retained.append((identities, request))
                continue
            await self.status(request, "busy")
            await self.send(
                "shell",
                "execute_reply",
                {"status": "aborted", "execution_count": self.count},
                request,
                identities,
            )
            await self.status(request, "idle")
        self.queue = retained

    @staticmethod
    def error(name: str, value: str) -> dict[str, Any]:
        return {"status": "error", "ename": name, "evalue": value, "traceback": []}

    def history_reply(self, content: dict[str, Any]) -> dict[str, Any]:
        kind = content["hist_access_type"]
        if content.get("output", False) or kind not in {"tail", "range"}:
            return self.error(
                "NotImplementedError",
                "history supports source-only tail and range requests for this kernel process",
            )
        if kind == "tail":
            count = max(0, content.get("n", 0))
            entries = self.history[-count:] if count else []
        elif content.get("session", 0) not in {0, 1}:
            entries = []
        else:
            entries = [
                entry
                for entry in self.history
                if content.get("start", 0) <= entry[1] < content.get("stop", 0)
            ]
        return {"status": "ok", "history": entries}

    async def next_request(self) -> None:
        while (
            self.queue
            and self.active is None
            and self.shutdown is None
            and self.ready is not None
        ):
            identities, request = self.queue.popleft()
            kind = request["header"]["msg_type"]
            content = request["content"]
            if kind not in {
                "execute_request",
                "kernel_info_request",
                "complete_request",
                "inspect_request",
                "is_complete_request",
                "history_request",
                "comm_info_request",
            }:
                continue
            await self.status(request, "busy")
            if kind == "execute_request":
                if content.get("store_history", True) and not content.get(
                    "silent", False
                ):
                    self.count += 1
                    self.history.append([1, self.count, content["code"]])
                self.active = Execution(identities, request, self.count)
                if not self.active.silent:
                    await self.send(
                        "iopub",
                        "execute_input",
                        {"code": content["code"], "execution_count": self.count},
                        request,
                    )
                await self.command(
                    {
                        "id": self.active.id,
                        "op": "execute",
                        "code": content["code"],
                        "allow_stdin": content.get("allow_stdin", True),
                    }
                )
                return
            if kind == "kernel_info_request":
                mime, suffix = {
                    "r": ("text/x-r-source", ".r"),
                    "python": ("text/x-python", ".py"),
                    "sql": ("text/x-sql", ".sql"),
                }[self.options.language]
                reply = {
                    "status": "ok",
                    "protocol_version": protocol_version,
                    "implementation": "mcp-console",
                    "implementation_version": self.ready["implementation_version"],
                    "language_info": {
                        "name": self.options.language,
                        "version": "",
                        "mimetype": mime,
                        "file_extension": suffix,
                    },
                    "banner": f"MCP Console — {self.options.language}",
                    "help_links": [],
                    "debugger": False,
                    "supported_features": ["iopub_welcome"],
                }
            elif kind == "complete_request":
                reply = {
                    "status": "ok",
                    "matches": [],
                    "cursor_start": content["cursor_pos"],
                    "cursor_end": content["cursor_pos"],
                    "metadata": {},
                }
            elif kind == "inspect_request":
                reply = {"status": "ok", "found": False, "data": {}, "metadata": {}}
            elif kind == "is_complete_request":
                reply = {"status": "unknown", "indent": ""}
            elif kind == "history_request":
                reply = self.history_reply(content)
            else:
                reply = {"status": "ok", "comms": {}}
            await self.send(
                "shell",
                kind.removesuffix("request") + "reply",
                reply,
                request,
                identities,
            )
            await self.status(request, "idle")

    async def run(self) -> None:
        arguments = self.options.arguments
        if arguments[:1] == ["--"]:
            arguments = arguments[1:]
        # All Jupyter sockets are bound before the controller can prepare runtimes.
        self.backend = await asyncio.create_subprocess_exec(
            self.options.binary,
            "kernel-controller",
            "--language",
            self.options.language,
            *arguments,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
        )
        reader = asyncio.create_task(self.read_backend())
        stopped = asyncio.create_task(self.stop.wait())
        previous_signal = signal.signal(signal.SIGTERM, lambda *_: self.stop.set())
        try:
            while not self.stop.is_set():
                polling = asyncio.ensure_future(self.poller.poll())
                done, _ = await asyncio.wait(
                    (polling, stopped), return_when=asyncio.FIRST_COMPLETED
                )
                if stopped in done:
                    polling.cancel()
                    await asyncio.gather(polling, return_exceptions=True)
                    break
                ready = dict(await polling)
                if self.sockets["hb"] in ready:
                    await self.sockets["hb"].send_multipart(
                        await self.sockets["hb"].recv_multipart()
                    )
                if self.sockets["iopub"] in ready:
                    subscription = await self.sockets["iopub"].recv()
                    if subscription[:1] == b"\x01":
                        await self.send(
                            "iopub",
                            "iopub_welcome",
                            {"subscription": subscription[1:].decode()},
                        )
                if self.sockets["control"] in ready and (
                    request := await self.receive("control")
                ):
                    await self.control(*request)
                if (
                    self.sockets["shell"] in ready
                    and self.shutdown is None
                    and (request := await self.receive("shell"))
                ):
                    self.queue.append(request)
                if self.sockets["stdin"] in ready and (
                    request := await self.receive("stdin")
                ):
                    await self.stdin(*request)
                if self.wake in ready:
                    await self.wake.recv()
                    while self.events:
                        await self.backend_event(self.events.popleft())
                await self.next_request()
        finally:
            signal.signal(signal.SIGTERM, previous_signal)
            self.closing = True
            self.backend.stdin.close()
            # EOF asks the native owner to retire its processes and recordings.
            # Continue draining stdout while it does so, including on SIGTERM.
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)
            while await self.backend.stdout.read(65536):
                pass
            await self.backend.wait()
            stopped.cancel()
            await asyncio.gather(stopped, return_exceptions=True)
            for socket in [*self.sockets.values(), self.wake, self.notify]:
                # Bound transport drainage after native cleanup; linger=0 can
                # discard the shutdown reply immediately after it was queued.
                socket.close(linger=1000)
            self.context.term()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--connection-file", required=True)
    parser.add_argument("--language", required=True, choices=("r", "python", "sql"))
    parser.add_argument("--binary", required=True)
    parser.add_argument("arguments", nargs=argparse.REMAINDER)
    asyncio.run(Kernel(parser.parse_args()).run())


if __name__ == "__main__":
    main()
