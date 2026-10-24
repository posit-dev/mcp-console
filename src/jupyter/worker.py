"""Translate Console's private worker protocol to a local Jupyter kernel.

The adapter owns the kernel process, but never evaluates submitted source itself.
Keep socket access on one event loop; bounded queues backpressure channel readers.
Kernel execution ends only after its shell reply AND matching IOPub idle message.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import secrets
import signal
import string
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from jupyter_client import AsyncKernelClient


def error_text(content: dict) -> str:
    traceback = content.get("traceback", [])
    return (
        "\n".join(traceback)
        if traceback
        else f"{content.get('ename', 'Error')}: {content.get('evalue', '')}"
    ) + "\n"


@dataclass
class Execution:
    request_id: str
    idle: bool = False
    reply: dict | None = None
    error_seen: bool = False


class Adapter:
    def __init__(self, options: argparse.Namespace) -> None:
        self.options = options
        self.read_fd = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD"))
        self.write_fd = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD"))
        os.set_inheritable(self.read_fd, False)
        os.set_inheritable(self.write_fd, False)
        self.events: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=64)
        self.stopping = asyncio.Event()
        self.interrupted = asyncio.Event()
        self.child: asyncio.subprocess.Process | None = None
        self.client: AsyncKernelClient | None = None
        self.pending_reads: dict[tuple[AsyncKernelClient, str], asyncio.Task[dict]] = {}
        self.current: Execution | None = None
        self.started = False
        self.input_pending = False
        self.input_buffer = bytearray()
        self.writer: asyncio.StreamWriter | None = None
        self.channels_started = False
        self.kernel_ready = False

    async def emit(self, kind: str, **content: Any) -> None:
        assert self.writer is not None
        self.writer.write((json.dumps({"kind": kind, **content}) + "\n").encode())
        await self.writer.drain()

    async def read_pipe(self, descriptor: int, commands: bool) -> None:
        reader = asyncio.StreamReader()
        transport, _ = await asyncio.get_running_loop().connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(descriptor, "rb")
        )
        pending = bytearray()
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    if commands and pending:
                        raise ValueError("partial Console command at EOF")
                    self.stopping.set()
                    return
                if commands:
                    pending.extend(data)
                    while b"\n" in pending:
                        line, _, rest = pending.partition(b"\n")
                        pending = bytearray(rest)
                        command = json.loads(line)
                        if command["kind"] == "shutdown":
                            self.stopping.set()
                            return
                        await self.events.put(("command", command))
                else:
                    await self.events.put(("input", data))
        finally:
            transport.close()

    def pending_read(
        self, client: AsyncKernelClient, channel: str
    ) -> asyncio.Task[dict]:
        # A ZeroMQ receive belongs to the generation, not its current consumer.
        # Transfer the live read and any received message from readiness through
        # dispatch; cancel it only when the generation retires.
        key = (client, channel)
        if key not in self.pending_reads:
            receive = getattr(client, f"get_{channel}_msg")
            self.pending_reads[key] = asyncio.create_task(receive())
        return self.pending_reads[key]

    async def receive_message(self, client: AsyncKernelClient, channel: str) -> dict:
        message = await asyncio.shield(self.pending_read(client, channel))
        del self.pending_reads[(client, channel)]
        return message

    async def read_channel(self, client: AsyncKernelClient, name: str) -> None:
        while True:
            message = await self.receive_message(client, name)
            await self.events.put((name, message))

    async def send_message(
        self, client: AsyncKernelClient, channel: str, kind: str, **content: Any
    ) -> str:
        # Session.send shadows async sockets with synchronous sockets, bypassing
        # pyzmq's pending-receive wakeups. Sign with Session, but keep both socket
        # directions on the async API so sending cannot strand a queued reply.
        message = client.session.msg(kind, content)
        frames = client.session.serialize(message)
        socket = getattr(client, f"{channel}_channel").socket
        await socket.send_multipart(
            frames, copy=max(map(len, frames)) < client.session.copy_threshold
        )
        return message["header"]["msg_id"]

    async def interrupt(self) -> None:
        while True:
            await self.interrupted.wait()
            self.interrupted.clear()
            if not self.kernel_ready:
                raise RuntimeError("Jupyter startup interrupted")
            await self.events.put(("interrupt", None))

    async def watch_child(self) -> None:
        assert self.child is not None
        status = await self.child.wait()
        raise RuntimeError(f"Jupyter kernel exited with status {status}")

    def startup_output(self, message: dict) -> None:
        # The private protocol accepts console events only after ready. Raw
        # process streams retain startup diagnostics and initialization errors.
        kind, content = message["msg_type"], message["content"]
        if kind == "stream":
            stream = sys.stderr if content["name"] == "stderr" else sys.stdout
            stream.write(content["text"])
            stream.flush()
        elif kind == "error":
            sys.stderr.write(error_text(content))
            sys.stderr.flush()

    async def wait_ready(self, client: AsyncKernelClient) -> dict:
        # A live child can take time to bind its heartbeat socket. Its process
        # observer owns death detection; readiness requires both Jupyter channels.
        # Retry because PUB/SUB can drop messages before subscription, retaining
        # each request's progress: a late reply must not become stale on retry.
        pending: set[str] = set()
        replies: dict[str, dict] = {}
        idle: set[str] = set()
        receivers = {
            self.pending_read(client, "shell"): "shell",
            self.pending_read(client, "iopub"): "iopub",
        }
        loop = asyncio.get_running_loop()
        retry = loop.time()
        async with asyncio.timeout(30):
            while True:
                if loop.time() >= retry:
                    pending.add(
                        await self.send_message(client, "shell", "kernel_info_request")
                    )
                    retry = loop.time() + 1
                ready, _ = await asyncio.wait(
                    receivers,
                    timeout=max(0, retry - loop.time()),
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in ready:
                    channel = receivers.pop(task)
                    message = await self.receive_message(client, channel)
                    request = message.get("parent_header", {}).get("msg_id")
                    kind = message["msg_type"]
                    if channel == "iopub":
                        self.startup_output(message)
                    if request in pending:
                        if channel == "shell" and kind == "kernel_info_reply":
                            replies[request] = message["content"]
                        elif (
                            channel == "iopub"
                            and kind == "status"
                            and message["content"]["execution_state"] == "idle"
                        ):
                            idle.add(request)
                    receivers[self.pending_read(client, channel)] = channel
                for request in replies.keys() & idle:
                    return replies[request]

    async def display(self, data: dict) -> None:
        # MCP has no HTML/widget display surface. Prefer one supported image;
        # otherwise preserve the kernel's plain-text representation.
        for mime in ("image/png", "image/jpeg", "image/gif", "image/webp"):
            if mime in data:
                value = "".join(data[mime].split())
                # Ark publishes unpadded base64. Console's private image
                # protocol requires standard padding; retain its validation.
                value += "=" * (-len(value) % 4)
                await self.emit("image", data=value, mime_type=mime)
                return
        text = data.get("text/plain")
        if text is not None:
            await self.emit(
                "console_output", data=text + ("" if text.endswith("\n") else "\n")
            )
        elif data:
            await self.emit(
                "console_diagnostic",
                data="[Jupyter display omitted: " + ", ".join(sorted(data)) + "]\n",
            )

    async def error(self, content: dict) -> None:
        await self.emit("console_diagnostic", data=error_text(content))

    async def input_reply(self) -> None:
        if not self.input_pending or b"\n" not in self.input_buffer:
            return
        assert self.client is not None
        line, _, rest = self.input_buffer.partition(b"\n")
        self.input_buffer = bytearray(rest)
        await self.send_message(
            self.client,
            "stdin",
            "input_reply",
            value=line.decode("utf-8").removesuffix("\r"),
        )
        self.input_pending = False
        await self.emit("input_received")

    async def cancel_input(self) -> None:
        if self.input_pending:
            self.input_pending = False
            await self.emit("input_cancelled")

    async def evaluate(self, command: dict) -> None:
        assert self.current is None, "overlapping Jupyter execution"
        assert command["kind"] == "evaluate", "unsupported Jupyter worker command"
        assert command["language"] == "kernel", (
            "unsupported source language for this kernel"
        )
        assert self.client is not None
        self.started = False
        request = await self.send_message(
            self.client,
            "shell",
            "execute_request",
            code=command["source"],
            silent=False,
            store_history=True,
            user_expressions={},
            allow_stdin=True,
            stop_on_error=True,
        )
        self.current = Execution(request)

    async def dispatch(self) -> None:
        assert self.client is not None and self.child is not None
        while True:
            channel, message = await self.events.get()
            if channel == "command":
                await self.evaluate(message)
            elif channel == "input":
                self.input_buffer.extend(message)
                await self.input_reply()
            elif channel == "interrupt":
                await self.cancel_input()
                if self.child.returncode is None:
                    if self.interrupt_mode == "message":
                        await self.send_message(
                            self.client, "control", "interrupt_request"
                        )
                    else:
                        self.child.send_signal(signal.SIGINT)
            else:
                kind, content = message["msg_type"], message["content"]
                parent = message.get("parent_header", {}).get("msg_id")
                current = (
                    self.current
                    if self.current and self.current.request_id == parent
                    else None
                )
                if channel == "iopub":
                    if kind == "stream":
                        await self.emit(
                            "console_diagnostic"
                            if content["name"] == "stderr"
                            else "console_output",
                            data=content["text"],
                        )
                    elif kind in {
                        "display_data",
                        "execute_result",
                        "update_display_data",
                    }:
                        if kind == "update_display_data":
                            await self.emit(
                                "console_diagnostic", data="[Jupyter display updated]\n"
                            )
                        await self.display(content["data"])
                    elif kind == "clear_output":
                        await self.emit(
                            "console_diagnostic",
                            data="[Jupyter clear_output; earlier output retained]\n",
                        )
                    elif kind == "error":
                        await self.error(content)
                        if current is not None:
                            current.error_seen = True
                    elif current and kind == "status":
                        if content["execution_state"] == "busy" and not self.started:
                            self.started = True
                            await self.emit("cell_started")
                        elif content["execution_state"] == "idle":
                            current.idle = True
                elif current and channel == "shell" and kind == "execute_reply":
                    current.reply = content
                elif current and channel == "stdin" and kind == "input_request":
                    assert not self.input_pending, "overlapping Jupyter input requests"
                    self.input_pending = True
                    await self.emit("input_requested", prompt=content["prompt"])
                    await self.input_reply()
            if self.current and self.current.idle and self.current.reply is not None:
                assert self.started, "kernel completed without publishing busy status"
                await self.cancel_input()
                reply = self.current.reply
                if reply["status"] == "error" and not self.current.error_seen:
                    await self.error(reply)
                elif reply["status"] == "aborted":
                    await self.emit(
                        "console_diagnostic", data="[Jupyter execution aborted]\n"
                    )
                await self.emit("completed")
                self.current = None

    async def serve(self, directory: str) -> None:
        import zmq.asyncio
        from jupyter_client import AsyncKernelClient
        from jupyter_client.connect import write_connection_file
        from jupyter_client.kernelspec import KernelSpecManager

        spec = KernelSpecManager().get_kernel_spec(self.options.kernel)
        provisioner = spec.metadata.get("kernel_provisioner", {}).get(
            "provisioner_name", "local-provisioner"
        )
        if provisioner != "local-provisioner":
            raise ValueError("Jupyter mode supports local kernelspecs only")
        self.interrupt_mode = spec.interrupt_mode
        connection_file = str(Path(directory) / "connection.json")
        _, connection = write_connection_file(
            connection_file,
            transport=self.options.transport,
            ip="127.0.0.1"
            if self.options.transport == "tcp"
            else str(Path(directory) / "k"),
            key=secrets.token_hex(32).encode(),
        )
        if self.options.transport == "ipc":
            # Published ark uses prefix:port where jupyter_client uses
            # prefix-port. Both resolve inside this private directory. Kernels
            # using the standard name unlink the alias when binding it.
            for channel in ("shell", "iopub", "stdin", "control", "hb"):
                port = connection[f"{channel}_port"]
                Path(f"{connection['ip']}-{port}").symlink_to(f"k:{port}")
        substitutions = {
            "connection_file": connection_file,
            "resource_dir": spec.resource_dir,
            "prefix": sys.prefix,
        }
        command = [
            os.path.expanduser(
                re.sub(
                    r"\{(connection_file|resource_dir|prefix)\}",
                    lambda m: substitutions[m[1]],
                    arg,
                )
            )
            for arg in spec.argv
        ]
        if command[0] in {
            "python",
            f"python{sys.version_info.major}",
            f"python{sys.version_info.major}.{sys.version_info.minor}",
        }:
            command[0] = sys.executable
        if self.options.namespace_ipykernel and "-m" in command:
            module = command.index("-m")
            if command[module + 1 : module + 2] == ["ipykernel_launcher"]:
                # Preserve interpreter options such as -Xfrozen_modules=off in
                # the published installer. Other modules keep their own launch.
                command[module : module + 2] = ["-c", self.options.namespace_ipykernel]
        environment = os.environ.copy()
        environment.update(
            {
                key: string.Template(value).safe_substitute(environment)
                for key, value in spec.env.items()
            }
        )
        if spec.language.lower().startswith("python"):
            environment.pop("PYTHONEXECUTABLE", None)
        environment["JPY_PARENT_PID"] = str(os.getpid())
        # KernelManager's launcher unconditionally detaches POSIX sessions. Keep
        # this child in Console's process tree and own its wait/termination here.
        context = zmq.asyncio.Context()
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command, stdin=asyncio.subprocess.DEVNULL, env=environment
            )
        )
        try:
            # Cancellation cannot discard a process acquired by a pending spawn.
            try:
                self.child = await asyncio.shield(launch)
            except asyncio.CancelledError:
                self.child = await launch
                raise
            self.client = AsyncKernelClient(
                connection_file=connection_file, context=context
            )
            self.client.load_connection_file()
            self.client.start_channels(hb=False)
            self.channels_started = True
            async with asyncio.TaskGroup() as group:
                group.create_task(self.watch_child())
                await self.wait_ready(self.client)
                self.kernel_ready = True
                await self.emit("ready")
                for channel in ("shell", "iopub", "stdin"):
                    group.create_task(self.read_channel(self.client, channel))
                group.create_task(self.dispatch())
        finally:
            # Fit inside the relay's one-second worker retirement grace. The
            # child is reaped before the adapter closes its own sideband pipes.
            if self.child is not None and self.child.returncode is None:
                try:
                    if self.channels_started:
                        try:
                            async with asyncio.timeout(0.2):
                                await self.send_message(
                                    self.client,
                                    "control",
                                    "shutdown_request",
                                    restart=False,
                                )
                        except TimeoutError:
                            pass
                finally:
                    # A failed socket setup/shutdown must still retire the child.
                    for action in (None, self.child.terminate, self.child.kill):
                        if action is not None and self.child.returncode is None:
                            action()
                        try:
                            await asyncio.wait_for(self.child.wait(), timeout=0.2)
                            break
                        except TimeoutError:
                            continue
                    if self.child.returncode is None:
                        raise RuntimeError(
                            "Jupyter kernel did not retire after SIGKILL"
                        )
            for task in self.pending_reads.values():
                task.cancel()
            await asyncio.gather(*self.pending_reads.values(), return_exceptions=True)
            self.pending_reads.clear()
            if self.client is not None:
                self.client.stop_channels()
            context.destroy(linger=0)

    async def run(self) -> None:
        loop = asyncio.get_running_loop()
        transport, protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(self.write_fd, "wb")
        )
        self.writer = asyncio.StreamWriter(transport, protocol, None, loop)
        loop.add_signal_handler(signal.SIGINT, self.interrupted.set)
        loop.add_signal_handler(signal.SIGTERM, self.stopping.set)
        # Unix-domain socket names include the runner's private TMPDIR prefix.
        with tempfile.TemporaryDirectory(prefix="j-") as directory:
            if self.options.private_runtime:
                # Set these before importing jupyter_client, which imports
                # IPython's path discovery. Explicit selections remain intact.
                os.environ.setdefault("IPYTHONDIR", str(Path(directory) / "ipython"))
                os.environ.setdefault(
                    "MPLCONFIGDIR", str(Path(directory) / "matplotlib")
                )
            tasks = [
                asyncio.create_task(self.read_pipe(self.read_fd, True)),
                asyncio.create_task(self.read_pipe(os.dup(sys.stdin.fileno()), False)),
                asyncio.create_task(self.interrupt()),
                asyncio.create_task(self.serve(directory)),
                asyncio.create_task(self.stopping.wait()),
            ]
            # Child death must wake an idle adapter too. The serve task installs
            # its process observer immediately after acquiring the subprocess.
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                outcomes = await asyncio.gather(*tasks, return_exceptions=True)
                transport.close()
                for outcome in outcomes:
                    if isinstance(outcome, Exception):
                        raise outcome


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--kernel", required=True)
    parser.add_argument("--transport", choices=("tcp", "ipc"), default="tcp")
    parser.add_argument("--private-runtime", action="store_true")
    parser.add_argument("--namespace-ipykernel")
    options = parser.parse_args()
    asyncio.run(Adapter(options).run())


if __name__ == "__main__":
    main()
