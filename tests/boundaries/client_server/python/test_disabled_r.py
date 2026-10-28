"""Python and SQL stay usable when native R is explicitly disabled."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SQL, command, requires
from support.r import r_test_environment


@executions(RUNTIME)
def test_disabled_r_uses_existing_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # A broken R hint must be irrelevant when the runtime is disabled.
        environment = {**os.environ, "R_HOME": str(workspace / "absent-r")}
        with McpClient(
            binary,
            execution.serve(
                "--no-config",
                "-c",
                "r.enabled=false",
                "-c",
                "languages=[python,sql]",
                "-c",
                "python=" + json.dumps(sys.executable),
            ),
            environment,
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("2\n", python="1 + 1")
            client.expect(
                "[done]",
                # fmt: python
                python=code("""
                    import sqlite3

                    _console.sql_connection(sqlite3.connect(":memory:"))
                    """),
            )
            client.expect(sql="CREATE TABLE answers AS SELECT 1 + 1 AS answer")
            client.expect(
                "2\n",
                python='print(_console.sql_connection().execute("SELECT answer FROM answers").fetchone()[0])',
            )
            client.send(control="restart")
            client.expect("2\n", python="1 + 1")
            return client.finish()


@requires(R)
@executions(RUNTIME)
def test_disabled_installed_r_never_runs_native_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        profile = workspace / "profile.R"
        profile.write_text('stop("disabled R startup must not run")\n')
        environment, _ = r_test_environment()
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "--no-config",
                "-c",
                "r.enabled=false",
                "-c",
                "python=" + json.dumps(sys.executable),
            ),
            environment,
            workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("2\n", python="answer = 1 + 1; answer")
            result = client.send(r="1 + 1")
            assert result.get("isError"), result
            assert (
                last_result_text(client)
                == "R cells are unavailable in Python sessions without R"
            )
            client.expect("2\n", python="answer")
            return client.finish()


@requires(SQL, command("uv"))
@executions(RUNTIME)
def test_disabled_r_uses_managed_python_and_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = {**os.environ, "R_HOME": str(workspace / "absent-r")}
        environment.pop("RETICULATE_PYTHON", None)
        environment.pop("MCP_CONSOLE_LANGUAGES", None)
        with McpClient(
            binary,
            execution.serve("--no-config", "-c", "r.enabled=false"),
            environment,
            workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "2\n",
                # fmt: python
                python=code("""
                    import sys
                    import numpy as np

                    assert "duckdb" not in sys.modules
                    answer = int(np.sum([1, 1]))
                    answer
                    """),
            )
            client.expect(
                "Count\n-----\n[0 rows]\n",
                sql="CREATE TABLE managed_answers (answer INTEGER)",
            )
            client.expect(
                # fmt: python
                python=code("""
                    assert "duckdb" in sys.modules
                    connection = _console.sql_connection()
                    assert connection.execute("SELECT count(*) FROM managed_answers").fetchone() == (0,)
                    """),
            )
            result = client.send(r="1 + 1")
            assert result.get("isError"), result
            assert last_result_text(client) == (
                "R cells are unavailable in Python sessions without R"
            )
            client.expect("2\n", python="answer")
            return client.finish()
