#!/usr/bin/env python3

import os
from pathlib import Path

from scripted_relay import ScriptedRelay


def checkpoint(name: str, *, wait: bool = False) -> None:
    path = Path(os.environ["MCP_CONSOLE_TEST_ADMISSION_ROOT"]) / name
    with path.open("rb" if wait else "wb", buffering=0) as pipe:
        if wait:
            assert pipe.read(1) == b"1"
        else:
            assert pipe.write(b"1") == 1


def main() -> None:
    failed = Path(os.environ["MCP_CONSOLE_TEST_ADMISSION_ROOT"]) / "failed"
    relay = ScriptedRelay()
    try:
        relay.ready()
        if failed.exists():
            checkpoint("replacement-ready")
            while True:
                command = relay.receive()
                if command["kind"] == "shutdown":
                    relay.retire(command)
                    return
                assert command["kind"] == "evaluate", command
                relay.send({"kind": "console_output", "data": command["source"] + "\n"})
                relay.complete()
        else:
            relay.expect({"kind": "evaluate", "language": "r", "source": "first cell"})
            checkpoint("first-started")
            relay.expect({"kind": "interrupt", "request_id": 0})
            checkpoint("interrupt-selected")
            if "MCP_CONSOLE_TEST_ADMISSION_LOST_ACK" not in os.environ:
                relay.send({"kind": "interrupt_result", "request_id": 0})
            checkpoint("fault-release", wait=True)
            failed.touch()
            relay.send({"kind": "fatal", "message": "selected worker failed"})
            relay.retire()
    finally:
        relay.close()


if __name__ == "__main__":
    main()
