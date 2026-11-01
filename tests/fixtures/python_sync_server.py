"""A stdio peer for public client lifecycle, framing, and warmup tests."""

import json
import os
from pathlib import Path
import signal
import socket
import sys
from threading import Thread


def reply(request: dict, result: dict) -> None:
    print(
        json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": result}),
        flush=True,
    )


def output(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}], "isError": False}


def hold(request: dict) -> None:
    with socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=10) as gate:
        gate.sendall(b"1")
        assert gate.recv(1) == b"1"
    reply(request, output("released"))


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")
    sys.stdout.reconfigure(encoding="utf-8")
    mode = sys.argv[1]
    abandoned = None
    tail = ""
    pending_language = None
    for line in sys.stdin:
        request = json.loads(line)
        with Path("requests.jsonl").open("a", encoding="utf-8") as record:
            record.write(line)
        method = request["method"]
        if method == "initialize":
            reply(
                request,
                {
                    "protocolVersion": mode
                    if mode in {"2025-06-18", "unsupported"}
                    else "2025-11-25",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "sync-fixture", "version": "1.0"},
                },
            )
        elif method == "notifications/initialized":
            continue
        elif method == "tools/list":
            tools = (
                []
                if mode == "empty-tools"
                else [
                    {
                        "name": "send",
                        "description": "Fixture send",
                        "inputSchema": {
                            "type": "object",
                            "properties": {
                                field: {"type": "string"}
                                for field in (
                                    ("r", "python", "sql")
                                    if mode == "warmup-progress"
                                    else ("python",)
                                )
                            },
                        },
                    }
                ]
            )
            reply(request, {"tools": tools})
        elif method == "notifications/cancelled":
            assert request["params"]["requestId"] == abandoned
            sys.stdout.write(tail)
            sys.stdout.flush()
            abandoned = None
        elif method == "tools/call":
            if abandoned is not None:
                sys.stdout.write(tail)
                sys.stdout.flush()
                reply(request, output("missing request cancellation"))
                continue
            arguments = request["params"]["arguments"]
            if mode == "warmup-progress":
                fields = [
                    field for field in ("r", "python", "sql") if field in arguments
                ]
                if fields:
                    assert len(fields) == 1 and pending_language is None, arguments
                    pending_language = fields[0]
                    text = (
                        "\n[phase: startup]\n[worker starting]"
                        if pending_language == "python"
                        else "\n[elapsed: 60.0s since admission; no new output; "
                        "phase: dependency preparation]\n[running; poll with an empty send]"
                    )
                elif pending_language is not None:
                    label = {"r": "R", "python": "Python", "sql": "SQL"}[
                        pending_language
                    ]
                    text = f"mcp-console warmup: {label} OK\n"
                    pending_language = None
                else:
                    text = "\n[idle]"
                reply(
                    request,
                    output(text)
                    | {
                        "_meta": {
                            "io.github.t-kalinowski.mcp-console/warmup-runtime": {
                                "r": True
                            }
                        }
                    },
                )
                continue
            source = arguments.get("python", "")
            if source == "cancel wait":
                abandoned = request["id"]
                encoded = (
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": abandoned,
                            "result": output("x" * 1_000_000),
                        }
                    )
                    + "\n"
                )
                # Exceed pipe capacity before signaling so the client has read
                # an incomplete frame. Its next call must discard this reply.
                sys.stdout.write(encoded[:-2])
                sys.stdout.flush()
                tail = encoded[-2:]
                if mode == "external-interrupt":
                    with socket.create_connection(
                        ("127.0.0.1", int(sys.argv[2])), timeout=10
                    ) as gate:
                        gate.sendall(b"1")
                        assert gate.recv(1) == b"1"
                else:
                    os.kill(os.getppid(), signal.SIGINT)
            elif source == "hold":
                Thread(target=hold, args=(request,)).start()
            elif source == "rpc error":
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": request["id"],
                            "error": {"code": -32602, "message": "fixture error"},
                        }
                    ),
                    flush=True,
                )
            elif source == "exit":
                return
            elif arguments.get("control") == "interrupt":
                reply(request, output("interrupted"))
            else:
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "method": "notifications/message",
                            "params": {"level": "info", "data": "fixture notification"},
                        }
                    ),
                    flush=True,
                )
                reply(request, output(source))
        else:
            raise AssertionError(request)


try:
    main()
finally:
    Path("closed").touch()
