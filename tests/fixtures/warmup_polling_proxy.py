"""Force a real warmup cell to yield, then release it on the first poll."""

import json
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient


binary, workspace, capture = map(Path, sys.argv[1:])
with closing(FifoCheckpoint.create(workspace / "release.fifo")) as release:
    with McpClient(
        binary,
        (
            "serve",
            "--no-sandbox",
            "--no-config",
            "-c",
            "languages=[python]",
            "-c",
            f"python={sys.executable}",
        ),
        current_directory=workspace,
    ) as client:
        submitted = False
        try:
            for line in sys.stdin:
                message = json.loads(line)
                if message["method"] == "tools/call":
                    arguments = message["params"]["arguments"]
                    if "python" in arguments:
                        assert not submitted, message
                        # Settle real worker startup before the gated cell so the
                        # first yielded result has the running notice exactly.
                        ready = client.send_message(
                            {
                                **message,
                                "id": "fixture-ready",
                                "params": {
                                    **message["params"],
                                    "arguments": {
                                        "python": "None",
                                        "timeout_ms": 300000,
                                    },
                                },
                            }
                        )
                        client.receive(ready)
                        assert not ready["result"]["isError"], ready
                        arguments["python"] = (
                            f"with open({str(release.path)!r}, 'rb', buffering=0) as gate:\n"
                            "    assert gate.read(1) == b'1'\n" + arguments["python"]
                        )
                        arguments["timeout_ms"] = 0
                        submitted = True
                    else:
                        assert submitted, message
                        release.release()
                        arguments["timeout_ms"] = 300000
                entry = client.send_message(message)
                if "id" in message:
                    client.receive(entry)
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": entry["id"],
                                **{
                                    key: entry[key]
                                    for key in ("result", "error")
                                    if key in entry
                                },
                            }
                        ),
                        flush=True,
                    )
        finally:
            release.release()
        capture.write_text(json.dumps(client.finish()), encoding="utf-8")
