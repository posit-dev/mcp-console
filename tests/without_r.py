# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12>=0.2.0"]
# ///

"""Run the public sans-R cases against an installed build on an R-free host.

Use tests/fixtures/python_without_r.Dockerfile for a reproducible Linux host.
The container must permit native namespaces (for example, docker --privileged).
Its direct cases run as an unprivileged user; native sandbox cases run as root
for hosts that restrict unprivileged network namespace configuration.
"""

import argparse
import importlib.util
import os
import shutil
from pathlib import Path

from support.snapshots import check_recording

assert "R_HOME" not in os.environ, "unset R_HOME on the R-free host"
assert all(shutil.which(name) is None for name in ("R", "Rscript", "ir"))
assert not any(
    Path(root).exists() for root in ("/usr/lib/R", "/usr/local/lib/R", "/opt/R")
)
binary = Path(shutil.which("mcp-console") or "").resolve()
assert binary.is_file(), "install the wheel before running acceptance"
suite = Path(__file__).parent / "boundaries/client_server/python/test_without_r.py"
spec = importlib.util.spec_from_file_location("sans_r_acceptance", suite)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--execution", choices=("direct", "sandbox"))
execution_name = parser.parse_args().execution

for name, case in vars(module).items():
    if not name.startswith("test_"):
        continue
    for execution in case.executions:
        if execution_name is not None and execution.name != execution_name:
            continue
        print(f"{name}[{execution.name}]", flush=True)
        check_recording(
            "client_server/python/test_without_r",
            name.removeprefix("test_"),
            case(binary, execution),
            update=False,
            execution=execution.name,
        )
print("Installed R-free public MCP acceptance passed", flush=True)
