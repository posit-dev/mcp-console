"""Record sequential SDK exchanges through the shared MCP test client.

This fixture supports one outstanding request at a time, without server
notifications. McpClient validates response IDs and owns server cleanup.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from support.client import McpClient


parser = argparse.ArgumentParser()
parser.add_argument("--workspace", type=Path)
parser.add_argument("capture", type=Path)
parser.add_argument("command", type=Path)
parser.add_argument("arguments", nargs=argparse.REMAINDER)
arguments = parser.parse_args()
with McpClient(
    arguments.command,
    tuple(arguments.arguments),
    current_directory=arguments.workspace,
) as client:
    for line in sys.stdin:
        message = json.loads(line)
        entry = client.send_message(message)
        if "id" in message:
            client.receive(entry)
            response = {
                "jsonrpc": "2.0",
                "id": entry["id"],
                **{key: entry[key] for key in ("result", "error") if key in entry},
            }
            print(json.dumps(response), flush=True)
    transcript = client.finish()
arguments.capture.write_text(json.dumps(transcript), encoding="utf-8")
