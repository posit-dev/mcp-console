#!/usr/bin/env python3
import json
import os
import subprocess
import sys

config = {
    "sandbox": {"profile": "read_only", "network": "restricted"},
    "environment": {"MESSAGE": 'value: café 雪, "quotes", $() and spaces'},
}
console = sys.argv[1] if len(sys.argv) > 1 else "mcp-console"
subprocess.run(
    [
        console,
        "sandbox",
        "--config-env",
        "CONSOLE_CONFIG",
        "--",
        "/bin/sh",
        "-c",
        'printf "%s\\n" "$MESSAGE" "$1"',
        "sh",
        "literal argument: * ; $(echo no)",
    ],
    env={**os.environ, "CONSOLE_CONFIG": json.dumps(config, ensure_ascii=False)},
    check=True,
)
