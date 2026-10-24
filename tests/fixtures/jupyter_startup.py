"""Hold a real ipykernel process before its Jupyter readiness handshake."""

import os
import runpy
from pathlib import Path

gate = Path(os.environ["MCP_CONSOLE_TEST_JUPYTER_STARTUP_GATE"])
with gate.open("rb", buffering=0) as release:
    gate.with_suffix(".pid").write_text(str(os.getpid()), encoding="utf-8")
    gate.with_suffix(".ready").touch()
    assert release.read(1) == b"1", "startup gate closed without release"
runpy.run_module("ipykernel_launcher", run_name="__main__", alter_sys=True)
