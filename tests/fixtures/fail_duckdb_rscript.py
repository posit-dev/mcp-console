#!/usr/bin/env python3

import json
import os
import subprocess
import sys
from pathlib import Path

arguments = sys.argv[1:]
real_rscript = os.environ["MCP_CONSOLE_TEST_REAL_RSCRIPT"]
real_home = str(Path(real_rscript).parent.parent)
os.environ.update(R_HOME=real_home, RHOME=real_home)

if "-e" in arguments and "duckdb_extensions()" in arguments[arguments.index("-e") + 1]:
    payload = sys.stdin.buffer.read()
    extensions = json.loads(payload)["extensions"]
    failure = os.environ["MCP_CONSOLE_TEST_DUCKDB_FAIL_EXTENSION"]
    if failure in extensions:
        print(f"fixture DuckDB extension {failure} is unavailable", file=sys.stderr)
        raise SystemExit(1)
    raise SystemExit(
        subprocess.run([real_rscript, *arguments], input=payload).returncode
    )

os.execv(real_rscript, [real_rscript, *arguments])
