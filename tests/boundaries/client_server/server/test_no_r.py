#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from support.requirements import POSIX, SQL, requires
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.normalization import normalize_process_diagnostic, code
from support.records import Transcript, TranscriptWithCompanions
from boundaries.client_server.python.test_without_r import (
    environment as without_r_environment,
)
from support.resolvers import bare_runtime_environment, resolve_managed_python
from support.suites import run_this_suite


def no_r_environment(directory: Path) -> dict[str, str]:
    commands = directory / "commands"
    commands.mkdir()
    uv = shutil.which("uv")
    assert uv is not None, "uv is required"
    if os.name == "nt":
        shutil.copy2(uv, commands / "uv.exe")
    else:
        (commands / "uv").symlink_to(uv)
    return without_r_environment(commands)


@contextmanager
def no_r_client(binary: Path, execution: Execution):
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        with McpClient(
            binary, execution.serve(), no_r_environment(workspace), workspace
        ) as client:
            yield client


def symlinked_system_python_environment(workspace: Path) -> dict[str, str]:
    environment = no_r_environment(workspace)
    (workspace / "commands/python3").symlink_to(Path(sys.executable).resolve())
    environment.update(
        UV_PYTHON_INSTALL_DIR=str(workspace / "empty-python-installations"),
        UV_PYTHON_DOWNLOADS="never",
        UV_PYTHON_PREFERENCE="only-system",
    )
    return environment


@executions(DIRECT, SANDBOXED)
@requires(POSIX)
def test_rejects_managed_preparation_with_only_a_symlinked_system_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = symlinked_system_python_environment(workspace)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            result = client.send(python="6 * 7")
            assert result.get("isError"), result
            assert last_result_text(client) == (
                "managed Python version resolution failed: "
                "uv did not report a supported CPython interpreter"
            ), result
            transcript, stderr = client.finish_with_standard_error(
                expected_exit_status=1
            )
            assert stderr == last_result_text(client) + "\n", stderr
            return transcript + [{"exit_status": 1, "stderr": stderr}]


@executions(DIRECT, SANDBOXED)
@requires(POSIX)
def test_explicitly_selects_a_symlinked_system_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = symlinked_system_python_environment(workspace)
        configuration = workspace / ".agents/console/config.yaml"
        configuration.parent.mkdir(parents=True)
        configuration.write_text("python: commands/python3\n")
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="6 * 7")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_python_without_r_dependencies(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary, execution.serve(), no_r_environment(workspace), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="6 * 7")
            assert last_result_text(client) == "42\n", last_result_text(client)
            transcript = client.finish()
        session = next((workspace / ".agents/console/sessions").iterdir())
        quarto = (session / "sessions/session-001.qmd").read_text()
        assert "original-working-directory:" in quarto and "root.dir:" not in quarto, (
            quarto
        )
        assert "\nir:\n  isolated: true\n  packages: []\n" in quarto, quarto
        assert "execute:\n  eval: false" not in quarto, quarto
        assert "review before executing" in quarto
        assert "# From a working copy, run `ir render session-001.qmd`" in quarto
        assert "# IR rendering requires R on the render host" in quarto
        assert "```{python}" in quarto, quarto
        return TranscriptWithCompanions(
            transcript=transcript,
            companions={
                "qmd": quarto.replace(str(workspace.resolve()), "<workspace>").replace(
                    str(workspace), "<workspace>"
                )
            },
        )


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_no_r_user_selected_python_is_bare(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment = no_r_environment(directory)
        python = resolve_managed_python(
            binary, execution, directory, environment=environment
        )
        environment = bare_runtime_environment(environment, directory / "library")
        environment["PATH"] = str(directory / "empty-path")
        Path(environment["PATH"]).mkdir()
        retain_system_bwrap(Path(environment["PATH"]))
        environment["RETICULATE_PYTHON"] = str(python)
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
                "properties"
            ]
            assert {"r", "python", "sql"} <= properties.keys(), properties
            assert properties["requirements"]["properties"]["action"]["enum"] == [
                "get",
                "add",
                "set",
                "reset",
            ], properties["requirements"]
            client.send(
                # fmt: python
                python=code("""
                    answer = 41
                    answer + 1
                    """)
            )
            assert last_result_text(client) == "42\n", last_result_text(client)
            result = client.send(
                requirements={"python": ["mcp-console-definitely-missing-package"]}
            )
            assert result["isError"], result
            assert last_result_text(client) == (
                "Python requirements are unavailable in this non-managed Python session; "
                "install packages before starting the session"
            ), result
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_result_text(client), last_result_text(client)
            return client.finish()


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_python_and_sql_without_r(binary: Path, execution: Execution) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        # fmt: python
        python = code("""
            import ctypes
            import os

            original_pid = os.getpid()
            answer = 41
            import numpy as np
            import pandas as pd

            registered = pd.DataFrame({"value": np.array([20, 22])})
            _console.sql_connection().register("registered", registered)
            if os.name == "nt":
                assert ctypes.windll.kernel32.GetModuleHandleW("R.dll") == 0
            else:
                assert not hasattr(ctypes.CDLL(None), "Rf_initialize_R")
            assert _console.sql_connection().execute("SELECT answer FROM answers").fetchone() == (
                42,
            )
            answer + 1
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", last_result_text(client)
        client.send(sql="SELECT sum(value) AS answer FROM registered")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            # fmt: python
            python=code("""
                import sqlite3

                _console.sql_connection(sqlite3.connect(":memory:"))
                """)
        )
        client.send(sql="SELECT 7 AS temporary_answer")
        assert "7" in last_result_text(client)
        client.send(python="_console.sql_connection(None)")
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(r="1 + 1")
        assert (
            last_result_text(client)
            == "R cells are unavailable in Python sessions without R"
        ), client.transcript[-1]
        client.send(requirements={"r": ["praise"]})
        assert (
            last_result_text(client)
            == "R requirements are unavailable in Python sessions without R"
        ), client.transcript[-1]
        client.send(python="(os.getpid() == original_pid, answer)")
        assert last_result_text(client) == "(True, 41)\n"
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_no_r_interrupt_requirements_reject_before_control_and_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        wait_for_evaluation_output(
            client,
            '[input requested: "original cell> "]\n[waiting for stdin]',
            "original cell input checkpoint",
            completion_timeout_seconds=client.response_timeout,
            # fmt: python
            python=code("""
                import os

                original_pid = os.getpid()
                received = input("original cell> ")
                print(received)
                """),
        )
        assert "[waiting for stdin]" in last_result_text(client), last_result_text(
            client
        )
        for requirements, expected in (
            (
                {"r": ["praise"]},
                "R requirements are unavailable in Python sessions without R",
            ),
            (
                {"python": ["numpy"]},
                (
                    "[Python requirements cannot accompany control: interrupt; "
                    "prepare before first use or with control: restart]"
                ),
            ),
        ):
            result = client.send(
                control="interrupt",
                requirements=requirements,
                stdin="rejected input\n",
                python="interrupt_followup_ran = True",
            )
            assert result["isError"], result
            assert last_result_text(client) == expected, result
            client.send(session="default")
            assert "[waiting for stdin]" in last_result_text(client), last_result_text(
                client
            )
        client.expect("fresh input\n", stdin="fresh input\n")
        client.expect(
            "original cell and worker retained\n",
            # fmt: python
            python=code("""
                assert os.getpid() == original_pid
                assert received == "fresh input"
                assert "interrupt_followup_ran" not in globals()
                print("original cell and worker retained")
                """),
        )
        return client.finish()[3:]


# Native DuckDB query cancellation uses Unix SIGINT; Windows covers cooperative
# DB-API callbacks and native R queries in its acceptance suite.
@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_no_r_sql_interrupt_and_worker_crash(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        # Managed input belongs to the main worker thread, including a SQL UDF.
        client.send(sql="SET threads = 1")
        client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_result_text(client) == "[prepared]"
        # fmt: python
        python = code("""
            def sql_gate(value):
                input("SQL gate> ")
                return value


            _ = _console.sql_connection().create_function(
                "sql_gate", sql_gate, ["BIGINT"], "BIGINT", side_effects=True
            )
            """)
        client.send(python=python)
        client.send(sql="SELECT sql_gate(answer) FROM answers")
        assert "[waiting for stdin]" in last_result_text(client), last_result_text(
            client
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_result_text(client), last_result_text(client)
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            sql="SELECT sum(sqrt(a.i + b.i)) FROM range(1000000000) a(i), range(1000000000) b(i)",
            timeout_ms=100,
        )
        assert last_result_text(client).endswith("[running; poll with an empty send]")
        client.send(control="interrupt")
        assert last_result_text(client) == "Error: Query interrupted\n", (
            last_result_text(client)
        )
        client.send(sql="SELECT answer FROM answers")
        assert "42" in last_result_text(client), last_result_text(client)
        client.send(
            # fmt: python
            python=code("""
                import os

                os._exit(17)
                """)
        )
        output = last_result_text(client)
        assert "[worker exited with status 17]" in output, output
        assert "[starting new worker]" in output, output
        client.send(
            # fmt: python
            python=code("""
                import yaml12

                "sql_gate" in globals()
                """)
        )
        assert last_result_text(client) == "False\n"
        client.send(
            sql="SELECT count(*) AS count FROM information_schema.tables WHERE table_name = 'answers'"
        )
        assert "0" in last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_no_r_extension_preparation_uses_candidate_provider(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import os
            import sys

            original_pid = os.getpid()
            original_executable = sys.executable
            answer = 41
            original_connection = _console.sql_connection()
            """)
        client.send(python=python)
        client.send(
            control="restart",
            requirements={
                "action": "set",
                "python": ["numpy", "pandas", "duckdb==1.4.4"],
                "duckdb": ["not_a_real_duckdb_extension"],
            },
        )
        output = last_result_text(client)
        platform = re.search(r"/v1\.4\.4/([^/]+)/not_a_real_duckdb_extension", output)
        assert platform is not None, output
        assert f"platform={platform[1]}&" in output, output
        assert client.transcript[-1]["result"]["isError"] is True
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_process_diagnostic(output).replace(
                platform[1], "<duckdb platform>"
            )
        )
        # fmt: python
        python = code("""
            assert os.getpid() == original_pid
            assert sys.executable == original_executable
            assert _console.sql_connection() is original_connection
            answer + 1
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", last_result_text(client)
        return client.finish()


@requires(SQL)
@executions(DIRECT)
def test_no_r_extension_preparation_accepts_older_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        client.expect("42\n", python="retained = 42; retained")
        client.send(
            control="restart",
            requirements={
                "action": "set",
                "python": ["numpy<2", "pandas", "duckdb==0.9.2"],
                "python_version": ["<3.12"],
                "duckdb": ["not_a_real_duckdb_extension"],
            },
        )
        output = last_result_text(client)
        platform = re.search(r"/v0\.9\.2/([^/]+)/not_a_real_duckdb_extension", output)
        assert platform is not None, output
        assert client.transcript[-1]["result"]["isError"] is True
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_process_diagnostic(output).replace(
                platform[1], "<duckdb platform>"
            )
        )
        client.expect("42\n", python="retained")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
