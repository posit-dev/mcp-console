"""Record native Unix worker input without using Console's command translation."""

import json
import os
import signal
import sys
from pathlib import Path


signal.signal(signal.SIGINT, signal.SIG_IGN)
marker = Path(os.environ["TEST_DISPATCHED"])
reader = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD"))
writer = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD"))
with os.fdopen(reader, "rb") as source, os.fdopen(writer, "wb", buffering=0) as sink:
    sink.write(b'{"kind":"ready"}\n')
    with marker.open("wb") as records:
        for line in source:
            records.write(line)
            records.flush()
            if json.loads(line)["kind"] == "shutdown":
                marker.with_suffix(".stdin").write_bytes(sys.stdin.buffer.read())
                break
            sink.write(b'{"kind":"completed"}\n')
