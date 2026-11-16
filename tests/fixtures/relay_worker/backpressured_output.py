import json
import os
import signal
import sys
import threading
import traceback
from pathlib import Path


root = Path(os.environ["TMPDIR"])
reader = os.fdopen(int(os.environ.pop("MCP_CONSOLE_SIDEBAND_READ_FD")), "rb")
writer = os.fdopen(int(os.environ.pop("MCP_CONSOLE_SIDEBAND_WRITE_FD")), "wb")
interrupted = os.open(root / "worker-interrupted", os.O_WRONLY)
shutdown = os.open(root / "worker-shutdown", os.O_WRONLY)


def send(frame: bytes) -> None:
    writer.write(frame)
    writer.flush()


def produce_output() -> None:
    frame = b'{"kind":"console_output","data":"blocked output\\n"}\n'
    for _ in range(4096):
        send(frame)


def serve() -> None:
    try:
        (root / "worker-pid").write_text(str(os.getpid()))
        send(b'{"kind":"ready"}\n')
        assert json.loads(reader.readline())["kind"] == "evaluate"
        threading.Thread(target=produce_output, daemon=True).start()
        if sys.argv[1] == "natural":
            with (root / "worker-exit").open("rb", buffering=0) as release:
                assert release.read(1) == b"1"
        else:
            assert json.loads(reader.readline()) == {"kind": "shutdown"}
            assert os.write(shutdown, b"1") == 1
    except BaseException:
        traceback.print_exc()
        os._exit(1)
    os._exit(0)


# The relay sends SIGINT to the initial thread. All threads inherit the mask,
# so this thread's sigwait receives it independently of the protocol thread's
# blocking pipe reads and Python's deferred signal callbacks.
signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
threading.Thread(target=serve, daemon=True).start()
while True:
    assert signal.sigwait({signal.SIGINT}) == signal.SIGINT
    assert os.write(interrupted, b"1") == 1
