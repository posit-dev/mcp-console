"""Native Unix worker peer for complete-frame admission and decoding."""

import json
import os
import signal
import sys
from pathlib import Path
from threading import Thread

reader = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD"))
writer = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD"))
root = Path(os.environ["TMPDIR"])


def write(bytes_: bytes) -> None:
    remaining = memoryview(bytes_)
    while remaining:
        remaining = remaining[os.write(writer, remaining) :]


write(b'{"kind":"ready"}\n')
mode = sys.argv[1]
if mode == "commands":
    with os.fdopen(reader, "rb") as source:
        for line in source:
            command = json.loads(line)
            if command["kind"] == "shutdown":
                break
            (root / "dispatched").write_text(command["source"])
            write(
                json.dumps(
                    {"kind": "console_output", "data": command["source"]},
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode()
                + b"\r\n"
            )
            write(b'{"kind":"completed"}\n')
elif mode == "stdin":

    def observe_stdin() -> None:
        if os.read(0, 1):
            (root / "dispatched").write_text("stdin")

    Thread(target=observe_stdin, daemon=True).start()
    with os.fdopen(reader, "rb") as source:
        assert json.loads(source.readline()) == {"kind": "shutdown"}
else:
    assert sys.stdin.readline() == "start\n"
    if mode == "fragmented":
        frame = b'{"kind":"console_output","data":"fragmented ' + "🦀".encode()
        write(frame[:-3])
        with (root / "suffix").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
        write(frame[-3:] + b'"}\r\n{"kind":"completed"}\n{"kind":')
    elif mode == "malformed":
        write(b'{"kind":\n')
    elif mode == "empty":
        write(b"\r\n")
    else:
        raise AssertionError(mode)
    # Keep the direct worker alive until the relay initiates shutdown. Its
    # final incomplete semantic tail must be abandoned during retirement.
    with os.fdopen(reader, "rb") as source:
        assert json.loads(source.readline()) == {"kind": "shutdown"}
    if mode == "fragmented":
        child = os.fork()
        if child == 0:
            for descriptor in (0, 1, 2):
                os.close(descriptor)
            while True:
                signal.pause()
        (root / "holder-pid").write_text(str(child))
