"""Optional runtimes and connections initialize when public operations need them."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.server.test_no_r import no_r_environment
from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.python import isolated_python
from support.records import Transcript
from support.requirements import R, SQL, command, requires
from support.r import r_test_environment


@requires(R, SQL)
@executions(RUNTIME)
def test_r_defers_python_and_sql_until_use(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        modules = workspace / "modules 日本語"
        modules.mkdir()
        marker = workspace / "python-started"
        (modules / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import sys
                from pathlib import Path

                if "_mcp_console_services" in sys.modules:
                    with Path({str(marker)!r}).open("a") as stream:
                        stream.write("started\\n")
                """),
        )
        environment, _ = r_test_environment()
        environment.update(
            RETICULATE_PYTHONPATH=str(modules),
            PYTHONHOME="unused-python-home",
            PYTHONPLATLIBDIR="unused-python-layout",
            VIRTUAL_ENV="unused-virtualenv",
        )
        arguments = execution.serve(
            "--no-config",
            "-c",
            "python=" + json.dumps(sys.executable),
            "-c",
            "r.packages=[]",
        )
        if execution.name == "sandbox":
            arguments += ("--writable-root", str(workspace))
        with McpClient(binary, arguments, environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect("[1] 2\n", r="1 + 1")
            assert not marker.exists(), "R startup initialized Python"
            client.expect(
                r='stopifnot(!"duckdb" %in% loadedNamespaces())',
            )
            client.expect(
                "Python initialized on demand\n",
                # fmt: python
                python=code("""
                    import os
                    import sys

                    assert "duckdb" not in sys.modules
                    assert "PYTHONHOME" not in os.environ
                    assert "PYTHONPLATLIBDIR" not in os.environ
                    expected_virtualenv = sys.prefix if sys.prefix != sys.base_prefix else None
                    assert os.environ.get("VIRTUAL_ENV") == expected_virtualenv
                    answer = 41
                    print("Python initialized on demand")
                    """),
            )
            assert marker.read_text() == "started\n"
            client.expect("[1] 42\n", r="reticulate::py$answer + 1L")
            client.expect(
                r='stopifnot(!"duckdb" %in% loadedNamespaces())',
            )
            client.expect(sql="CREATE TABLE demand_ready (answer INTEGER)")
            client.expect(
                r='stopifnot("duckdb" %in% loadedNamespaces())',
            )
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect(
                r='stopifnot(!"duckdb" %in% loadedNamespaces())',
            )
            assert marker.read_text() == "started\n"
            return client.finish()


@requires(R)
@executions(RUNTIME)
def test_python_startup_precedes_native_r_without_replacing_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # fmt: python
        startup = code("""
            import ctypes
            import io
            import os
            import sqlite3
            import sys


            def r_initialized():
                if os.name == "nt":
                    kernel = ctypes.WinDLL("kernel32")
                    kernel.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
                    kernel.GetModuleHandleW.restype = ctypes.c_void_p
                    handle = kernel.GetModuleHandleW("R.dll")
                    if not handle:
                        return False
                    library = ctypes.CDLL(None, handle=handle)
                else:
                    library = ctypes.CDLL(None)
                try:
                    return bool(ctypes.c_void_p.in_dll(library, "R_GlobalEnv").value)
                except ValueError:
                    return False


            assert not r_initialized(), "native R preceded configured Python startup"
            persistent = object()
            connection = sqlite3.connect(":memory:")
            connection.execute("CREATE TABLE retained (answer INTEGER)")
            connection.execute("INSERT INTO retained VALUES (42)")
            _console.sql_connection(connection)
            before = (id(persistent), id(connection), sys.executable, sys.prefix, sys.base_prefix)
            original_streams = (sys.stdout, sys.stderr)
            redirected_stdout, redirected_stderr = io.StringIO(), io.StringIO()
            sys.stdout, sys.stderr = redirected_stdout, redirected_stderr
            """)
        environment, _ = r_test_environment()
        with McpClient(
            binary,
            execution.serve(
                "--no-config",
                "-c",
                "languages=[python,sql]",
                "-c",
                "python=" + json.dumps(sys.executable),
                "-c",
                "r.packages=[]",
                "-c",
                "startup=" + json.dumps({"language": "python", "code": startup}),
            ),
            environment,
            workspace,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Configured Python startup selects SQLite before native R starts.",
                    "fixture": "Startup retains Python objects, interpreter paths, and redirected streams for checks after R and its Python bridge initialize.",
                }
            )
            client.initialize_and_list_tools()
            client.expect(
                # fmt: python
                python=code("""
                    assert r_initialized(), "native R startup did not complete before the first cell"
                    assert int(r["sum(c(20, 22))"]) == 42
                    """),
            )
            client.expect(
                "Python state retained after native R startup\n",
                # fmt: python
                python=code("""
                    assert r_initialized()
                    assert sys.stdout is redirected_stdout
                    assert sys.stderr is redirected_stderr
                    print("retained stdout")
                    print("retained stderr", file=sys.stderr)
                    assert redirected_stdout.getvalue() == "retained stdout\\n"
                    assert redirected_stderr.getvalue() == "retained stderr\\n"
                    sys.stdout, sys.stderr = original_streams
                    assert before == (
                        id(persistent),
                        id(connection),
                        sys.executable,
                        sys.prefix,
                        sys.base_prefix,
                    )
                    assert _console.sql_connection() is connection
                    assert connection.execute("SELECT answer FROM retained").fetchone() == (42,)
                    print("Python state retained after native R startup")
                    """),
            )
            client.expect(None, sql="SELECT answer FROM retained")
            assert "42" in last_result_text(client)
            return client.finish()


@requires(R)
@executions(RUNTIME)
def test_r_startup_preserves_captured_python_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        original = workspace / "original"
        original.mkdir()
        selected, _ = isolated_python(original)
        replacement = workspace / "replacement"
        replacement.mkdir()
        other, _ = isolated_python(replacement)
        profile = workspace / "profile.R"
        profile.write_text(
            # fmt: r
            code(f"""
                setwd({
                  json.dumps(str(replacement))
                })
                Sys.setenv(RETICULATE_PYTHON = {
                  json.dumps(str(other))
                })
                """),
        )
        environment, _ = r_test_environment()
        environment["R_PROFILE_USER"] = str(profile)
        environment["MCP_CONSOLE_TEST_EXPECTED_PREFIX"] = str(original / "python")
        arguments = execution.serve(
            "--no-config",
            "-c",
            "python=" + json.dumps(str(selected)),
        )
        with McpClient(
            binary, arguments, environment, workspace, use_r_startup_files=True
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "captured Python selection\n",
                # fmt: python
                python=code("""
                    import os
                    import sys

                    assert os.path.samefile(sys.prefix, os.environ["MCP_CONSOLE_TEST_EXPECTED_PREFIX"])
                    print("captured Python selection")
                    """),
            )
            return client.finish()


@requires(R, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_r_first_python_use_preserves_captured_selection_after_profile_changes_hint(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        selected, site = isolated_python(workspace)
        subprocess.run(
            ["uv", "pip", "install", "--python", selected, "numpy"],
            check=True,
            capture_output=True,
        )
        (site / "captured_fixture.py").write_text("answer = 42\n")
        profile = workspace / "profile.R"
        profile.write_text(
            # fmt: r
            code(f"""
            Sys.setenv(RETICULATE_PYTHON = {
              json.dumps(str(workspace / "missing-python"))
            })
                """)
        )
        environment, _ = r_test_environment()
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "--no-config",
                "-c",
                "r.packages=[]",
                "-c",
                "python=" + json.dumps(str(selected)),
            ),
            environment,
            workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[1] 42\n",
                # fmt: r
                r=code("""
                    stopifnot(identical(
                      as.integer(reticulate::py_eval("__import__('numpy').array([40, 2])")),
                      c(40L, 2L)
                    ))
                    reticulate::py_eval("__import__('captured_fixture').answer")
                    """),
            )
            client.expect(
                "42\n", python="import captured_fixture; captured_fixture.answer"
            )
            return client.finish()


@requires(R)
@executions(RUNTIME)
def test_adopts_captured_python_initialized_by_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        profile = workspace / "profile.R"
        profile.write_text('reticulate::py_run_string("profile_value = 41")\n')
        environment, _ = r_test_environment()
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "--no-config", "-c", "python=" + json.dumps(sys.executable)
            ),
            environment,
            workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="profile_value + 1")
            client.expect("[1] 42\n", r="reticulate::py$profile_value + 1L")
            return client.finish()


@requires(R)
@executions(RUNTIME)
def test_rejects_r_startup_replacing_captured_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        replacement = workspace / "replacement"
        replacement.mkdir()
        other, _ = isolated_python(replacement)
        profile = workspace / "profile.R"
        profile.write_text(
            # fmt: r
            code(f"""
                Sys.setenv(RETICULATE_PYTHON = {
                  json.dumps(str(other))
                })
                reticulate::py_require(character(), action = "set")
                reticulate::py_run_string("profile_value = 41")
                """),
        )
        environment, _ = r_test_environment()
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "--no-config", "-c", "python=" + json.dumps(sys.executable)
            ),
            environment,
            workspace,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            output = wait_for_evaluation_output(
                client,
                None,
                "R startup rejects a different Python selection",
                expected_error=True,
                completion_timeout_seconds=client.response_timeout,
                r="1 + 1",
            )
            assert "Python interpreter differs from the captured selection" in output
            return client.finish()


@requires(SQL, command("uv"))
@executions(RUNTIME)
def test_python_without_r_defers_managed_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = no_r_environment(workspace)
        with McpClient(
            binary, execution.serve("--no-config"), environment, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "2\n",
                # fmt: python
                python=code("""
                    import sys

                    assert "duckdb" not in sys.modules
                    1 + 1
                    """),
            )
            client.expect(
                "Count\n-----\n[0 rows]\n",
                sql="CREATE TABLE demand_ready (answer INTEGER)",
            )
            client.expect(python='assert "duckdb" in sys.modules')
            client.expect(
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            client.expect(
                # fmt: python
                python=code("""
                    import sys

                    assert "duckdb" not in sys.modules
                    """),
            )
            return client.finish()
