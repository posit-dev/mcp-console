#!/usr/bin/env python3

"""Fail retirement after restart takes ownership of an already failing worker."""

import os
import signal
from pathlib import Path

from scripted_relay import ScriptedRelay


def main() -> None:
    failed = Path(os.environ["MCP_CONSOLE_TEST_GENERATION_FAILED"])
    exit_status = os.environ.get("MCP_CONSOLE_TEST_RETIREMENT_EXIT_STATUS")
    relay = ScriptedRelay()
    try:
        relay.make_checkpoint("retirement-fault-release")
        relay.make_checkpoint("retirement-fault-sent")
        if failed.exists():
            relay.ready()
            while True:
                command = relay.receive()
                if command["kind"] == "shutdown":
                    relay.retire(command)
                    return
                assert command["kind"] == "evaluate", command
                relay.send({"kind": "stdout", "data": "replacement cell executed\n"})
                relay.complete()
        if "MCP_CONSOLE_TEST_INITIAL_FAILURE" in os.environ:
            relay.send({"kind": "fatal", "message": "scripted startup failure"})
        else:
            relay.ready()
            assert relay.receive()["kind"] == "evaluate"
            relay.send({"kind": "fatal", "message": "scripted evaluation failure"})
        assert relay.receive()["kind"] == "shutdown"
        relay.send({"kind": "shutdown_started"})
        relay.wait_for_checkpoint("retirement-fault-release")
        relay.send({"kind": "fatal", "message": "scripted retirement failure"})
        relay.notify_checkpoint("retirement-fault-sent")
        if exit_status is not None:
            raise SystemExit(int(exit_status))
        while True:
            signal.pause()
    finally:
        relay.close()


if __name__ == "__main__":
    main()
