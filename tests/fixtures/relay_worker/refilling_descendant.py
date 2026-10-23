import os
import sys
from pathlib import Path

root = Path(os.environ["TMPDIR"])
sideband = int(os.environ["MCP_CONSOLE_SIDEBAND_WRITE_FD"])
assert os.write(sideband, b'{"kind":"ready"}\n') > 0
assert sys.stdin.readline() == "start\n"
if os.fork() != 0:
    with (root / "worker-exit").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
    os._exit(0)

(root / "descendant-pid").write_text(str(os.getpid()))
stream = sys.argv[1]
descriptor = {"stdout": 1, "stderr": 2, "sideband": sideband}[stream]
payload = os.environ["MCP_CONSOLE_TEST_REFILL_MATCH"].encode()
request = os.open(root / "refill-request", os.O_RDONLY)
done = os.open(root / "refill-done", os.O_WRONLY)
assert os.write(descriptor, payload) == len(payload)
while os.read(request, 1) == b"1":
    assert os.write(descriptor, payload) == len(payload)
    assert os.write(done, b"1") == 1
