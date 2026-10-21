#!/usr/bin/env python3
"""Controlled worker receipts and native diagnostics at capture boundaries."""

import os
from pathlib import Path

from scripted_relay import ScriptedRelay, write_all


def gate(name: str) -> None:
    with (Path(os.environ["CAPTURE_GATES"]) / name).open("rb", buffering=0) as stream:
        assert stream.read(1) == b"1"


def notify(name: str) -> None:
    with (Path(os.environ["CAPTURE_GATES"]) / name).open("wb", buffering=0) as stream:
        stream.write(b"1")


mode = os.environ["CAPTURE_SCENARIO"]
lost_owner = mode in {"lost_startup_owner", "lost_post_owner"}
if lost_owner:
    marker = Path(os.environ["CAPTURE_GATES"]) / "relay-launched"
    replacement = marker.exists()
    marker.touch()
    if replacement:
        write_all(2, b"replacement launch diagnostic\n")
        notify("launch_sent")
        gate("acquire")
relay = ScriptedRelay(acquired=mode != "process_output")
try:
    if mode == "process_output":
        write_all(2, b"process head\n" + b"p" * 32768 + b"\nprocess tail\n")
        gate("release")
        relay.send({"kind": "worker_started", "pid": 42})
    relay.ready()
    command = relay.receive(
        cell_started=mode not in {"late_entry", "missing_entry", "lost_startup_owner"}
    )
    if command["kind"] == "shutdown":
        relay.retire(command)
    elif mode == "missing_entry":
        gate("release")
        relay.send({"kind": "completed"})
        relay.retire()
    elif lost_owner:
        assert not replacement
        if mode == "lost_post_owner":
            relay.send({"kind": "console_output", "data": "executed cell\n"})
            relay.send({"kind": "completed"})
            command = relay.receive(cell_started=False)
            assert command["kind"] == "evaluate", command
        relay.send({"kind": "console_output", "data": "before relay loss\n"})
        notify("post_sent")
        gate("release")
        # A vanished relay supplies no worker-exit or signal receipt.
    elif mode == "invalid_completion":
        relay.send({"kind": "input_requested", "prompt": "pending> "})
        gate("release")
        relay.send({"kind": "completed"})
        relay.retire()
    elif mode == "late_entry":
        notify("evaluating")
        command = relay.receive()
        assert command["kind"] == "shutdown", command
        relay.send({"kind": "shutdown_started"})
        notify("shutdown")
        gate("release")
        relay.send({"kind": "cell_started"})
        relay.send({"kind": "console_output", "data": "entered during retirement\n"})
        relay.send({"kind": "completed"})
        for kind in ("stdout_closed", "stderr_closed", "worker_sideband_closed"):
            relay.send({"kind": kind})
        relay.send({"kind": "worker_exited", "code": 0})
    elif mode == "ended_owner":
        relay.send({"kind": "completed"})
        relay.send({"kind": "console_output", "data": "post-cell text\n"})
        notify("post_sent")
        relay.retire()
        gate("release")
        write_all(2, b"after worker exit\n")
        gate("drained")
    else:
        assert mode == "process_output", mode
        relay.send({"kind": "completed"})
        relay.retire()
finally:
    relay.close()
