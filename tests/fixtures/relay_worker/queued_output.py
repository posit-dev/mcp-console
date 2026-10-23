import json
import os
import signal
import sys
import threading
from pathlib import Path


root = Path(os.environ["TMPDIR"])
reader = os.fdopen(int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD")), "rb")
writer = os.fdopen(int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD")), "wb")
interrupted = os.open(root / "worker-interrupted", os.O_WRONLY)
shutdown = os.open(root / "worker-shutdown", os.O_WRONLY)


def send(frame: bytes) -> None:
    writer.write(frame)
    writer.flush()


def acknowledge_interrupts() -> None:
    while True:
        assert signal.sigwait({signal.SIGINT}) == signal.SIGINT
        assert os.write(interrupted, b"1") == 1


# All threads inherit the mask. Receive SIGINT independently of the main
# thread's blocking pipe reads and Python's deferred signal callbacks.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
threading.Thread(target=acknowledge_interrupts, daemon=True).start()
(root / "worker-pid").write_text(str(os.getpid()))
send(b'{"kind":"ready"}\n')
assert json.loads(reader.readline())["kind"] == "evaluate"
first_size, frame_size, frame_count = map(int, sys.argv[1:4])


def produce_output() -> None:
    empty = b'{"kind":"console_output","data":""}\n'
    for index in range(frame_count):
        size = first_size if index == 0 else frame_size
        prefix = f"{index:04d}:"
        data = prefix + "x" * (size - len(empty) - len(prefix))
        frame = (
            json.dumps(
                {"kind": "console_output", "data": data}, separators=(",", ":")
            ).encode()
            + b"\n"
        )
        assert len(frame) == size
        send(frame)
        if index == 0:
            with (root / "continue-output").open("rb", buffering=0) as release:
                assert release.read(1) == b"1"


producer = threading.Thread(target=produce_output, daemon=True)
producer.start()
if sys.argv[4] == "natural":
    producer.join()
    with (root / "worker-exit").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
else:
    assert json.loads(reader.readline()) == {"kind": "shutdown"}
    assert os.write(shutdown, b"1") == 1
os._exit(0)
