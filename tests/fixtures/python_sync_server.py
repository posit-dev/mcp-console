"""A stdio peer for public Python client lifecycle and framing tests."""

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
                                "code" if mode == "kernel" else "python": {
                                    "type": "string"
                                }
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
            source = arguments.get("code" if mode == "kernel" else "python", "")
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
