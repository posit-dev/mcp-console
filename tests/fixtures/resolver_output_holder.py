import json
import os
import signal
from pathlib import Path

root = Path(os.environ["TEST_RESOLVER_ROOT"])


def checkpoint(name: str, mode: str) -> None:
    with (root / name).open(mode, buffering=0) as pipe:
        if mode == "wb":
            assert pipe.write(b"1") == 1
        else:
            assert pipe.read(1) == b"1"


(root / "leader").write_text(str(os.getpid()))
if os.environ["TEST_RESOLVER_MODE"] == "input-failure":
    os.write(2, b"fixture closed stdin before replying\n")
    checkpoint("ready", "wb")
    checkpoint("fail", "rb")
    os.close(0)
    signal.pause()
    os._exit(92)
if os.fork() == 0:
    # This fixture-owned process escapes group cleanup solely to prove I/O
    # retirement does not require inherited descriptors to reach EOF.
    os.setsid()
    (root / "holder").write_text(str(os.getpid()))
    checkpoint("ready", "wb")
    if os.environ["TEST_RESOLVER_MODE"] == "stream":
        os.write(2, b"streaming diagnostic\n" * 256)
        checkpoint("streaming", "wb")
        while True:
            try:
                os.write(2, b"streaming diagnostic\n" * 256)
            except BrokenPipeError:
                break
    checkpoint("probe", "rb")
    if os.environ["TEST_RESOLVER_MODE"] == "stdin":
        received = bytearray()
        while chunk := os.read(0, 8192):
            received.extend(chunk)
        assert received.startswith(b'{"extensions":')
        try:
            json.loads(received)
        except json.JSONDecodeError:
            pass
        else:
            raise AssertionError("cancelled writer finished the requirements JSON")
    for descriptor in (1, 2):
        try:
            os.write(descriptor, b"unexpected output after completion")
        except BrokenPipeError:
            pass
        else:
            os._exit(91)
    checkpoint("closed", "wb")
    os._exit(0)

checkpoint("exit", "rb")
if os.environ["TEST_RESOLVER_MODE"] == "failed":
    os.write(2, b"fixture materialization failed before replying\n")
    os._exit(42)
print(
    json.dumps(
        [
            {
                "version": "3.12.7",
                "version_parts": {"major": 3, "minor": 12, "patch": 7},
                "symlink": None,
                "variant": "default",
                "implementation": "cpython",
            }
        ]
    ),
    flush=True,
)
os._exit(0)
