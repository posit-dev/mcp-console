# /// script
# requires-python = ">=3.11"
# dependencies = ["httpx>=0.28,<0.29", "websockets>=15,<16", "psutil>=7,<8", "jupyter-client>=8,<9", "pyzmq>=27,<28"]
# ///
"""Exercise the public kernel entry point through an unmodified Kallichore.

Run with `uv run tests/kallichore.py --help`. This harness uses Kallichore's
existing REST and channel interfaces; it isn't a Console client frontend.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import socket
import subprocess
import sys
import tempfile
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import psutil
from websockets.asyncio.client import connect


class Gateway:
    def __init__(self, base: str, token: str, process: asyncio.subprocess.Process):
        self.client = httpx.AsyncClient(
            base_url=base,
            headers={"Authorization": f"Bearer {token}"},
            timeout=180,
        )
        self.websocket_base = base.replace("http://", "ws://")
        self.token = token
        self.process = process

    async def request(self, method: str, path: str, data: dict | None = None):
        response = await self.client.request(method, path, json=data)
        assert response.is_success, (method, path, response.status_code, response.text)
        return response.json() if response.content else None

    def channels(self, session: str):
        return connect(
            f"{self.websocket_base}/sessions/{session}/channels",
            additional_headers={"Authorization": f"Bearer {self.token}"},
            max_size=32 * 1024 * 1024,
        )


@asynccontextmanager
async def server(executable: Path, root: Path):
    assert os.name == "posix", "The harness uses Kallichore's Unix handshake socket"
    handshake_path = root / "handshake.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(handshake_path))
    listener.listen(1)
    listener.setblocking(False)
    log = (root / "kallichore.log").open("wb")
    process = await asyncio.create_subprocess_exec(
        str(executable),
        "--handshake-socket",
        str(handshake_path),
        "--port",
        "0",
        "--resource-sample-interval",
        "0",
        stdout=log,
        stderr=log,
    )
    gateway = None
    try:
        loop = asyncio.get_running_loop()
        connection, _ = await asyncio.wait_for(loop.sock_accept(listener), 20)
        with connection:
            chunks = []
            while data := await loop.sock_recv(connection, 65536):
                chunks.append(data)
        details = json.loads(b"".join(chunks))
        gateway = Gateway(details["base_path"], details["bearer_token"], process)
        yield gateway
    finally:
        listener.close()
        if gateway is not None:
            if process.returncode is None:
                await gateway.request("POST", "/shutdown")
            await gateway.client.aclose()
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), 15)
            except TimeoutError:
                process.kill()
                await process.wait()
        log.close()


async def message(ws) -> dict:
    return json.loads(await asyncio.wait_for(ws.recv(), 180))


async def ready(
    gateway: Gateway, session: str, ws, previous_pid: int | None = None
) -> dict:
    while True:
        state = await gateway.request("GET", f"/sessions/{session}")
        if (
            state["status"] in ("idle", "busy")
            and state.get("process_id") != previous_pid
        ):
            return state
        assert previous_pid is not None or state["status"] in (
            "uninitialized",
            "starting",
            "ready",
        ), state
        # Kallichore broadcasts lifecycle changes on this existing channel.
        await message(ws)


def wire(kind: str, content: dict, channel: str = "shell", parent=None) -> dict:
    return {
        "header": {"msg_id": str(uuid.uuid4()), "msg_type": kind},
        "parent_header": parent,
        "channel": channel,
        "content": content,
        "metadata": {},
        "buffers": [],
    }


async def channel_execute(ws, code: str, *, input_value: str | None = None, **flags):
    request = wire(
        "execute_request",
        {
            "code": code,
            "silent": False,
            "store_history": True,
            "allow_stdin": input_value is not None,
            "stop_on_error": True,
            "user_expressions": {},
            **flags,
        },
    )
    await ws.send(json.dumps(request))
    messages = []
    reply = None
    idle = False
    while reply is None or not idle:
        received = await message(ws)
        if received.get("kind") != "jupyter":
            continue
        if (received.get("parent_header") or {}).get("msg_id") != request["header"][
            "msg_id"
        ]:
            continue
        messages.append(received)
        kind = received["header"]["msg_type"]
        if kind == "input_request":
            assert input_value is not None, received
            await ws.send(
                json.dumps(
                    wire(
                        "input_reply",
                        {"value": input_value},
                        "stdin",
                        received["header"],
                    )
                )
            )
        elif kind == "execute_reply":
            reply = received["content"]
        elif kind == "status" and received["content"]["execution_state"] == "idle":
            idle = True
    states = [
        m["content"]["execution_state"]
        for m in messages
        if m["header"]["msg_type"] == "status"
    ]
    assert states == ["busy", "idle"], states
    return reply, messages


def stdout(reply: dict) -> str:
    return "".join(
        item.get("text", "") for item in reply["output"] if item["type"] == "stream"
    )


async def retired(processes: list[psutil.Process]) -> None:
    _, alive = await asyncio.to_thread(psutil.wait_procs, processes, timeout=15)
    assert not alive, [(p.pid, p.name()) for p in alive]


async def shutdown(gateway: Gateway, session: str, ws, kernel: psutil.Process) -> None:
    owned = [kernel, *kernel.children(recursive=True)]
    request = wire("shutdown_request", {"restart": False}, "control")
    await ws.send(json.dumps(request))
    replied = exited = False
    while not (replied and exited):
        received = await message(ws)
        if (
            received.get("kind") == "jupyter"
            and (received.get("parent_header") or {}).get("msg_id")
            == request["header"]["msg_id"]
            and received["header"]["msg_type"] == "shutdown_reply"
        ):
            assert received["content"]["restart"] is False, received
            replied = True
        if received.get("kind") == "kernel" and "exited" in received:
            assert received["exited"] == 0, received
            exited = True
    await retired(owned)
    await gateway.request("DELETE", f"/sessions/{session}")


async def create_session(
    gateway: Gateway, binary: Path, config: Path, root: Path, session: str
) -> str:
    route = f"/sessions/{session}"
    await gateway.request(
        "PUT",
        "/sessions",
        {
            "session_id": session,
            "display_name": "Console Python integration",
            "language": "python",
            "username": getpass.getuser(),
            "input_prompt": ">>> ",
            "continuation_prompt": "... ",
            "session_mode": "console",
            "startup_environment": "none",
            "working_directory": str(root / "workspace"),
            "env": [
                {
                    "action": "replace",
                    "name": "MCP_CONSOLE_HOME",
                    "value": str(root / "console-home"),
                }
            ],
            "argv": [
                str(binary),
                "kernel",
                "--jupyter-python",
                sys.executable,
                "--language",
                "python",
                "--connection-file",
                "{connection_file}",
                "--config-file",
                str(config),
            ],
            "interrupt_mode": "message",
            "protocol_version": "5.3",
            "connection_timeout": 30,
        },
    )
    await gateway.request("POST", route + "/start")
    return route


async def exercise(gateway: Gateway, binary: Path, config: Path, root: Path) -> dict:
    session = "console-integration"
    route = await create_session(gateway, binary, config, root, session)
    async with gateway.channels(session) as ws:
        state = await ready(gateway, session, ws)
        assert state["kernel_info"]["implementation"] == "mcp-console", state
        assert state["kernel_info"]["language_info"]["name"] == "python", state
        kernel = psutil.Process(state["process_id"])
        result = await gateway.request(
            "POST", route + "/execute", {"code": "answer = 6 * 7\nprint(answer)"}
        )
        assert result["status"] == "ok" and "42\n" in stdout(result), result
        result = await gateway.request(
            "POST", route + "/execute", {"code": "print(answer)"}
        )
        assert result["status"] == "ok" and "42\n" in stdout(result), result
        result = await gateway.request("POST", route + "/execute", {"code": "6 * 7"})
        assert (
            result["status"] == "ok"
            and result.get("data", {}).get("text/plain") == "42"
        ), result
        result = await gateway.request(
            "POST",
            route + "/execute",
            {
                "code": "import matplotlib.pyplot as plt\nplt.plot([1, 2], [3, 4])\nplt.show()"
            },
        )
        assert result["status"] == "ok", result
        assert any("image/png" in item.get("data", {}) for item in result["output"]), (
            result
        )
        result = await gateway.request(
            "POST",
            route + "/execute",
            {"code": "raise ValueError('language-error-probe')"},
        )
        assert result["status"] == "error" and result["error_name"] == "ValueError", (
            result
        )
        assert result["error_traceback"], result
        denied = root / "outside-workspace.txt"
        result = await gateway.request(
            "POST",
            route + "/execute",
            {
                "code": f"from pathlib import Path\nPath({str(denied)!r}).write_text('denied')"
            },
        )
        assert (
            result["status"] == "error" and result["error_name"] == "PermissionError"
        ), result
        assert not denied.exists(), denied
        result = await gateway.request(
            "POST",
            route + "/execute",
            {
                "code": "from pathlib import Path\nPath('inside-workspace.txt').write_text('allowed')"
            },
        )
        assert result["status"] == "ok", result
        assert (root / "workspace" / "inside-workspace.txt").read_text() == "allowed"
        result = await gateway.request(
            "POST", route + "/execute", {"code": "input('disabled> ')"}
        )
        assert result["status"] == "error", result
        result, messages = await channel_execute(
            ws, "print(input('name> '))", input_value="café"
        )
        assert result["status"] == "ok", result
        assert any(m["header"]["msg_type"] == "input_request" for m in messages), (
            messages
        )
        assert "café\n" in "".join(m["content"].get("text", "") for m in messages), (
            messages
        )
        count = result["execution_count"]
        result, messages = await channel_execute(
            ws, "answer = 43\nprint('hidden')", silent=True, store_history=True
        )
        assert result["status"] == "ok" and result["execution_count"] == count, result
        assert {m["header"]["msg_type"] for m in messages} == {
            "status",
            "execute_reply",
        }, messages
        result, _ = await channel_execute(ws, "print(answer)", store_history=False)
        assert result["execution_count"] == count, result
        pending = asyncio.create_task(
            gateway.request(
                "POST",
                route + "/execute",
                {
                    "code": "import time\nprint('interrupt-ready', flush=True)\nwhile True:\n    time.sleep(1)"
                },
            )
        )
        while True:
            received = await message(ws)
            if (
                received.get("kind") == "jupyter"
                and received["header"]["msg_type"] == "stream"
                and "interrupt-ready" in received["content"]["text"]
            ):
                break
        await gateway.request("POST", route + "/interrupt")
        result = await asyncio.wait_for(pending, 15)
        assert result["status"] == "error", result
        result = await gateway.request(
            "POST", route + "/execute", {"code": "print(answer)"}
        )
        assert result["status"] == "ok" and "43\n" in stdout(result), result
    # Closing the channel is a frontend disconnect, not a kernel shutdown.
    async with gateway.channels(session) as ws:
        result = await gateway.request(
            "POST", route + "/execute", {"code": "print(answer)"}
        )
        assert result["status"] == "ok" and "43\n" in stdout(result), result
        old_processes = [kernel, *kernel.children(recursive=True)]
        await gateway.request("POST", route + "/restart", {})
        state = await ready(gateway, session, ws, previous_pid=kernel.pid)
        await retired(old_processes)
        assert state["process_id"] != kernel.pid, state
        result = await gateway.request(
            "POST", route + "/execute", {"code": "print(answer)"}
        )
        assert result["status"] == "error" and result["error_name"] == "NameError", (
            result
        )
        result = await gateway.request(
            "POST", route + "/execute", {"code": "print(6 * 7)"}
        )
        assert result["status"] == "ok" and "42\n" in stdout(result), result
        kernel = psutil.Process(state["process_id"])
        await shutdown(gateway, session, ws, kernel)
    recordings = list((root / "console-home").glob("sessions/*/transcript.md"))
    assert len(recordings) == 2, recordings
    assert any("answer = 6 * 7" in p.read_text() for p in recordings), recordings
    for transcript in recordings:
        recording = transcript.parent
        events = [
            json.loads(line)
            for line in (recording / "capture/events.jsonl").read_text().splitlines()
        ]
        assert any(event["event"] == "worker_ended" for event in events), events[-10:]
        assert all(
            path.stat().st_mode & 0o222 == 0
            for path in recording.rglob("*")
            if path.is_file()
        ), recording
    status = await gateway.request("GET", "/status")
    return {
        "kallichore": status["version"],
        "recordings": len(recordings),
        "checks": [
            "session API",
            "ordinary source",
            "expression result",
            "persistent objects",
            "PNG",
            "structured errors",
            "sandbox writes",
            "stdin channels",
            "stdin disabled",
            "silent",
            "store_history",
            "message interrupt",
            "reconnect",
            "restart",
            "process cleanup",
            "recordings",
        ],
    }


async def preparation(gateway: Gateway, binary: Path, root: Path) -> None:
    for name, selection, source, expected in [
        (
            "managed-package",
            {"version": "3.13", "packages": ["py-yaml12"], "resolution": "fixed"},
            "import yaml12\nprint('prepared package imported')",
            "ok",
        ),
        (
            "unavailable-package",
            {"version": "3.13", "packages": ["py-yaml12<0"], "resolution": "fixed"},
            "print('must not execute')",
            "error",
        ),
    ]:
        directory = root / name
        (directory / "workspace").mkdir(parents=True)
        config = directory / "console.yaml"
        config.write_text(
            json.dumps(
                {
                    "r": {"packages": [], "resolution": "disabled", "vanilla": True},
                    "python": selection,
                    "sandbox": {"profile": "workspace"},
                }
            )
        )
        route = await create_session(gateway, binary, config, directory, name)
        async with gateway.channels(name) as ws:
            state = await ready(gateway, name, ws)
            assert state["kernel_info"]["implementation"] == "mcp-console", state
            result = await gateway.request("POST", route + "/execute", {"code": source})
            assert result["status"] == expected, result
            if expected == "ok":
                assert "prepared package imported" in stdout(result), result
            else:
                assert result["error_message"], result
                assert "must not execute" not in stdout(result), result
            kernel = psutil.Process(state["process_id"])
            await shutdown(gateway, name, ws, kernel)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--kallichore", type=Path, required=True, help="Unmodified kcserver executable"
    )
    parser.add_argument(
        "--binary", type=Path, required=True, help="Built mcp-console executable"
    )
    parser.add_argument(
        "--python", type=Path, help="Existing Python environment with matplotlib"
    )
    parser.add_argument(
        "--config",
        type=Path,
        help="Console launch configuration; default uses --python",
    )
    args = parser.parse_args()
    assert args.config or args.python, "Supply --config or --python"
    binary = args.binary.absolute()
    await asyncio.to_thread(
        subprocess.run,
        [str(binary), "kernel", "--help"],
        check=True,
        stdout=subprocess.DEVNULL,
    )
    with tempfile.TemporaryDirectory(prefix="console-kc-") as temporary:
        root = Path(temporary)
        (root / "workspace").mkdir()
        config = args.config.absolute() if args.config else root / "console.yaml"
        if not args.config:
            config.write_text(
                json.dumps(
                    {
                        "r": {
                            "packages": [],
                            "resolution": "disabled",
                            "vanilla": True,
                        },
                        "python": str(args.python.absolute()),
                        "sandbox": {"profile": "workspace"},
                    }
                )
            )
        try:
            async with server(args.kallichore.absolute(), root) as gateway:
                report = await exercise(gateway, binary, config, root)
                await preparation(gateway, binary, root)
                report["checks"].extend(["managed startup package", "startup failure"])
            print(json.dumps(report, indent=2))
        except BaseException:
            print((root / "kallichore.log").read_text(errors="replace")[-16000:])
            raise


if __name__ == "__main__":
    asyncio.run(main())
