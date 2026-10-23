#!/usr/bin/env python3

"""Fail a public send, retaining the relay until its owner signals retirement."""

import os
import signal
from pathlib import Path

from scripted_relay import EVALUATION, ScriptedRelay


def main() -> None:
    failed = Path(os.environ["MCP_CONSOLE_TEST_PHASE_FAILURE"])
    relay = ScriptedRelay()
    try:
        relay.ready()
        if failed.exists():
            while True:
                command = relay.receive()
                if command.get("kind") == "shutdown":
                    relay.retire(command)
                    return
                assert command == EVALUATION, command
                relay.complete()
        relay.expect(EVALUATION)
        relay.complete()
        relay.expect(EVALUATION)
        failed.touch()
        relay.send({"kind": "fatal", "message": "scripted phase failure"})
        command = relay.receive()
        assert command.get("kind") == "shutdown", command
        relay.send({"kind": "shutdown_started"})
        # The native signal checkpoint holds the server's retirement owner;
        # its release terminates this relay through the ordinary cleanup path.
        while True:
            signal.pause()
    finally:
        relay.close()


if __name__ == "__main__":
    main()
