"""Exercise cached inspection hooks without running the probe in the worker."""

import os
import runpy

if "MCP_CONSOLE_LOCAL_RUNTIME" not in os.environ:
    runpy.run_path(os.environ["PREPARATION_PROBE"])
