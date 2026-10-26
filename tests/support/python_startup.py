"""Public startup scenarios shared by transcript and native acceptance tests."""

import json
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory

from support.client import McpClient
from support.execution import Execution
from support.normalization import code
from support.python import isolated_python
from support.r import r_test_environment


def site_hook_calls_r(
    binary: Path,
    execution: Execution,
    *,
    start_r_in_hook: bool,
    expected_profile_error: str | None = None,
) -> list:
    assert expected_profile_error is None or start_r_in_hook
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        python, site = isolated_python(root)
        subprocess.run(
            ["uv", "pip", "install", "--python", python, "numpy"],
            check=True,
            capture_output=True,
        )
        (site / "console-callback.pth").write_text(
            "import builtins, sys; builtins.pth_calls = getattr(builtins, 'pth_calls', 0) + "
            "int('_mcp_console_services' in sys.modules)\n"
        )
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code("""
                import builtins
                import sys

                if "_mcp_console_services" in sys.modules:
                    assert "numpy" not in sys.modules
                    builtins.site_calls = getattr(builtins, "site_calls", 0) + 1
                    builtins.pth_calls_before_r = builtins.pth_calls
                    builtins.site_answer = int(builtins.r["40L + 2L"])
                    assert builtins.site_answer == 42
                """)
        )
        environment, _ = r_test_environment()
        # fmt: r
        initialize_counter = code("""
            initialization_calls <- 0L
            options(reticulate.python.afterInitialized = function() {
              initialization_calls <<- initialization_calls + 1L
            })
            """)
        arguments = execution.serve(
            "--no-config", "-c", "python=" + json.dumps(str(python))
        )
        if start_r_in_hook:
            profile = root / "profile.R"
            profile.write_text(
                initialize_counter
                + 'reticulate::py_run_string("profile_started = True")\n'
            )
            environment["R_PROFILE_USER"] = str(profile)
            arguments += (
                "-c",
                "startup="
                + json.dumps(
                    {
                        "language": "python",
                        "code": "import sqlite3; _console.sql_connection(sqlite3.connect(':memory:'))",
                    }
                ),
            )
        with McpClient(
            binary,
            arguments,
            environment,
            root,
            use_r_startup_files=start_r_in_hook,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                expected_profile_error or "[1] 42\n",
                r=("" if start_r_in_hook else initialize_counter)
                # fmt: r
                + code("""
                    answer <- reticulate::py_eval("__import__('builtins').site_answer")
                    stopifnot(initialization_calls == 1L)
                    answer
                    """),
            )
            if expected_profile_error is not None:
                return client.finish()
            if start_r_in_hook:
                client.expect("True\n", python="profile_started")
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import builtins
                    import numpy as np

                    assert builtins.site_calls == 1
                    assert builtins.pth_calls == builtins.pth_calls_before_r > 0
                    assert int(r["initialization_calls"]) == 1
                    r.callback_array = np.array([40, 2])
                    int(r["sum(callback_array)"])
                    """),
            )
            return client.finish()
