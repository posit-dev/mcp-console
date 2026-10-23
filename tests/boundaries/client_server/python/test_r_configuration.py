"""Built-in R configuration does not require an R installation."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.records import Transcript


@executions(DIRECT, SANDBOXED)
def test_vanilla_without_r(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        programs = root / "bin"
        programs.mkdir()
        retain_system_bwrap(programs)
        home = root / "home"
        home.mkdir()
        environment = dict(
            os.environ, PATH=str(programs), HOME=str(home), R_USER=str(home)
        )
        for name in (
            "R_HOME",
            "RETICULATE_PYTHON",
            "RETICULATE_PYTHONPATH",
            "PYTHONPATH",
        ):
            environment.pop(name, None)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            f"python: {sys.executable}\nlanguages: [python]\nr:\n  vanilla: true\n"
        )
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="21 * 2")
            client.send(control="restart")
            client.expect("42\n", python="21 * 2")
            client.finish()
            return [{"r_vanilla_without_r": True, "restart": True}]
