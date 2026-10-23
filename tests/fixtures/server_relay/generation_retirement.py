from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from scripted_relay import ScriptedRelay


def failed_during_resolver_callback(relay: ScriptedRelay) -> None:
    relay.ready()
    command = relay.receive()
    if command["kind"] == "shutdown":
        relay.retire(command)
        return
    assert command == {"kind": "evaluate", "language": "r", "source": "42"}
    relay.send({"kind": "resolve_r", "packages": ["blockedretirement"]})
    # The callback is real host preparation. Close command input before its
    # admission receipt, so subsequent public stdin deterministically fails.
    os.close(0)
    relay.make_checkpoint("callback-relay-release")
    relay.wait_for_checkpoint("callback-relay-release")
