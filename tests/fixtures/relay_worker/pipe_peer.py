import fcntl
import json
import os
import signal
import stat
import sys
from pathlib import Path


root = Path(os.environ["TMPDIR"])
assert "MCP_CONSOLE_SIDEBAND_FD" not in os.environ
reader = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD"))
writer = int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD"))
assert reader != writer and min(reader, writer) > 2
for descriptor, direction in ((reader, os.O_RDONLY), (writer, os.O_WRONLY)):
    assert stat.S_ISFIFO(os.fstat(descriptor).st_mode)
    assert fcntl.fcntl(descriptor, fcntl.F_GETFL) & os.O_ACCMODE == direction
    assert os.get_inheritable(descriptor)
    os.set_inheritable(descriptor, False)
    # Only the two worker endpoints may survive the relay's exec.
    identity = os.fstat(descriptor)
    for other in map(int, os.listdir("/dev/fd")):
        if other == descriptor:
            continue
        try:
            candidate = os.fstat(other)
        except OSError:
            continue
        assert (candidate.st_dev, candidate.st_ino) != (
            identity.st_dev,
            identity.st_ino,
        )


def send(message: dict[str, object]) -> None:
    frame = memoryview(json.dumps(message, separators=(",", ":")).encode() + b"\n")
    while frame:
        frame = frame[os.write(writer, frame) :]


send({"kind": "ready"})
assert sys.stdin.readline() == "start\n"
mode = sys.argv[1]
if mode == "exchange":
    with os.fdopen(reader, "rb") as source:
        message = json.loads(source.readline())
        assert message == {
            "kind": "evaluate",
            "language": "r",
            "source": "precise 👩🏽‍💻" * 8192,
        }
        send({"kind": "cell_started"})
        send({"kind": "console_output", "data": message["source"]})
        send({"kind": "completed"})
        assert json.loads(source.readline()) == {"kind": "shutdown"}
elif mode == "retained":
    # Retain both directions after the direct worker exits. No descendant reads
    # from the full relay-to-worker pipe or closes the worker-to-relay pipe.
    assert os.read(reader, 1) == b"{"
    child = os.fork()
    if child == 0:
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        while True:
            signal.pause()
    (root / "holder-pid").write_text(str(child))
    with (root / "worker-exit").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
elif mode == "closed":
    os.close(reader)
    send({"kind": "console_output", "data": "reader closed\n"})
    assert os.read(0, 1) == b""
else:
    assert mode == "partial"
    os.close(reader)
    assert os.write(writer, b'{"kind":"console_output"') > 0
    os.close(writer)
    assert os.read(0, 1) == b""
