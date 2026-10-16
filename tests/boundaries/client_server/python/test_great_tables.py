#!/usr/bin/env -S uv run --script

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.execution import SANDBOXED, Execution, executions
from support.normalization import code
from support.previews import normalize_preview_paths
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import MACOS_SANDBOX, command, requires
from support.suites import run_this_suite


@requires(command("uv"), MACOS_SANDBOX)
@executions(SANDBOXED)
def test_shows_table_repr_and_sandbox_save_error(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        selected = Path(directory).resolve() / "selected"
        environment = dict(os.environ)
        environment.pop("PYTHONPATH", None)
        environment.pop("RETICULATE_PYTHONPATH", None)
        subprocess.run(
            ["uv", "venv", "--python", "3.14", str(selected)],
            env=environment,
            check=True,
            capture_output=True,
        )
        python = virtualenv_python(selected)
        subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--python",
                str(python),
                "great-tables==1.1.0",
                "pandas==3.0.6",
                "selenium==4.51.0",
            ],
            env=environment,
            check=True,
            capture_output=True,
        )
        environment["RETICULATE_PYTHON"] = str(python)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(client, "Great Tables readiness")
            client.send(
                # fmt: python
                python=code("""
                    import pandas as pd
                    from great_tables import GT

                    sales = pd.DataFrame(
                        {
                            "Region": ["North", "South"],
                            "Revenue": [1234.5, 9876.0],
                        }
                    )
                    table = GT(sales).tab_header(title="Quarterly sales").fmt_currency(columns="Revenue")
                    table
                    """),
                timeout_ms=600_000,
            )
            content = client.transcript[-1]["result"]["content"]
            assert len(content) == 1 and content[0]["type"] == "text", content
            assert content[0]["text"].startswith("GT(_tbl_data="), content
            content[0]["text"] = re.sub(
                r" at 0x[0-9a-f]+>", " at <address>>", content[0]["text"]
            )

            # Keep the legacy export path to exercise Selenium's socket permissions.
            client.send(python='table.save("quarterly-sales.png")', timeout_ms=600_000)
            content = client.transcript[-1]["result"]["content"]
            assert len(content) == 1 and content[0]["type"] == "text", content
            output = content[0]["text"]
            assert 'free_socket.bind(("127.0.0.1", 0))' in output, output
            assert 'free_socket.bind(("::1", 0))' in output, output
            assert (
                output.count("PermissionError: [Errno 1] Operation not permitted") == 2
            ), output
            assert output.endswith(
                "RuntimeError: Can't find free port (Unable to bind to IPv4 or IPv6)\n"
            ), output
            content[0]["text"] = output.replace(str(selected), "<selected Python>")
            normalize_preview_paths(client)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
