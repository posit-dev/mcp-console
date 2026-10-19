"""Built-in convenience defaults must not block unrelated evaluation."""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from boundaries.client_server.requirements.test_actions import inspect
from support.assertions import last_result_text, last_tool_text
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import normalize_python_resolution_error
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import recording_ir_environment, recording_uv_environment
from support.resolvers import preseeded_duckdb_python
from support.r import r_test_environment
from boundaries.client_server.sql.test_catalog import extension_failure_environment


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_failed_r_default_preserves_other_defaults_and_explicit_failures(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = recording_ir_environment(root, fail_requirement="tidyverse")
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A built-in R convenience package cannot be installed.",
                    "fixture": "The ir wrapper fails every request containing tidyverse; other preparation uses real ir.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                python="answer = 40; print(answer + 2)",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default R package `tidyverse`" in text, text
            assert "synthetic `ir` failure for tidyverse" in text, text
            assert text.endswith("42\n"), text
            assert "tidyverse" not in accepted["requirements"]["r"], accepted
            assert "ggplot2" in accepted["requirements"]["r"], accepted
            assert "nanoarrow" in accepted["runtime_requirements"]["r"], accepted
            client.expect(
                "R default available\n",
                r="stopifnot(requireNamespace('ggplot2', quietly = TRUE)); cat('R default available\\n')",
            )
            result = client.send(
                requirements={"r": ["tidyverse"]},
                python="answer = 0",
            )
            assert result.get("isError"), result
            assert "synthetic `ir` failure for tidyverse" in last_result_text(client)
            assert inspect(client) == accepted
            client.expect("42\n", python="answer + 2")
            client.send(control="restart")
            assert inspect(client) == accepted
            client.expect("2\n", python="1 + 1")
            client.send(control="restart", requirements={"action": "set"})
            result = client.send(
                control="restart",
                requirements={"action": "reset"},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            assert "skipping default R package `tidyverse`" in last_tool_text(client)
            assert inspect(client) == accepted
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_failed_python_default_preserves_other_defaults_and_explicit_failures(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, _ = recording_uv_environment(root, fail_requirement="plotnine")
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A built-in Python convenience package cannot be installed, without R.",
                    "fixture": "The uv wrapper fails every request containing plotnine; other preparation uses real uv.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                python="answer = 40; print(answer + 2)",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default Python package `plotnine`" in text, text
            assert "synthetic uv failure" in text, text
            assert text.endswith("42\n"), text
            result["content"][0]["text"] = normalize_python_resolution_error(text)
            assert accepted["requirements"]["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
                "duckdb",
            ], accepted
            client.expect(
                "Python default available\n",
                python="import numpy; print('Python default available')",
            )
            result = client.send(
                requirements={"python": ["plotnine"]}, python="answer = 0"
            )
            assert result.get("isError"), result
            error = last_result_text(client)
            assert "synthetic uv failure" in error
            resolver_input = json.loads(
                error.split("resolver input:\n", 1)[1].split("\nuv output:", 1)[0]
            )
            assert Path(resolver_input["python"]).is_file(), resolver_input
            result["content"][0]["text"] = normalize_python_resolution_error(
                error, executable=resolver_input["python"]
            )
            assert inspect(client) == accepted
            client.expect("42\n", python="answer + 2")
            client.send(control="restart")
            assert inspect(client) == accepted
            client.expect("2\n", python="1 + 1")
            client.send(control="restart", requirements={"action": "set"})
            result = client.send(
                control="restart",
                requirements={"action": "reset"},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default Python package `plotnine`" in text
            result["content"][0]["text"] = normalize_python_resolution_error(text)
            assert inspect(client) == accepted
            return client.finish()


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_failed_python_default_with_r(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = r_test_environment()
        recorded, _ = recording_uv_environment(root, fail_requirement="plotnine")
        env.update(recorded)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "r.packages=[]"),
            env,
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Python defaults remain optional when R is available.",
                    "fixture": "The uv wrapper rejects plotnine; the R bridge remains required.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                r="1 + 1", timeout_ms=int(client.response_timeout * 1_000)
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default Python package `plotnine`" in text, text
            assert text.endswith("[1] 2\n"), text
            result["content"][0]["text"] = normalize_python_resolution_error(text)
            assert accepted["requirements"]["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
            ]
            client.expect("2\n", python="1 + 1")
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_reset_without_configured_duckdb_keeps_live_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, _ = recording_uv_environment(root)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                "-c",
                "python.managed.packages=[]",
                "-c",
                "python.managed.resolution=fixed",
            ),
            env,
            root,
        ) as client:
            client.initialize_and_list_tools()
            accepted = inspect(client)
            assert accepted["requirements"]["python"] == [], accepted
            assert accepted["requirements"]["duckdb"] == [], accepted
            client.expect("42\n", python="answer = 42; print(answer)")
            client.expect("[prepared]", requirements={"action": "reset"})
            client.expect("42\n", python="answer")
            return client.finish()


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_fixed_r_reset_retries_skipped_defaults(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = recording_ir_environment(root, fail_requirement="tidyverse")
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                "-c",
                "r.resolution=fixed",
                "-c",
                "python.managed.packages=[]",
            ),
            env,
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Fixed R resolution lets reset retry a skipped startup default.",
                    "fixture": "The ir wrapper rejects tidyverse during startup and reset; deliberate add and set remain forbidden.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                r="answer <- 42; cat(answer, '\\n', sep = '')",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            assert "skipping default R package `tidyverse`" in last_tool_text(client)
            assert "tidyverse" not in accepted["requirements"]["r"], accepted
            for action in ("add", "set"):
                result = client.send(
                    control="restart",
                    requirements={"action": action, "r": ["tidyverse"]},
                    r="answer <- 0",
                )
                assert result.get("isError"), result
                assert "r.resolution=fixed" in last_result_text(client)
            client.expect("42\n", r="cat(answer, '\\n', sep = '')")
            result = client.send(
                control="restart",
                requirements={"action": "reset"},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            assert "skipping default R package `tidyverse`" in last_tool_text(client)
            assert inspect(client) == accepted
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_reset_restores_extensions_after_python_duckdb_default_recovers(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, _ = recording_uv_environment(root, fail_requirement="duckdb")
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary,
            execution.serve(
                "-c", "cache=host", "-c", "python.managed.resolution=fixed"
            ),
            env,
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Fixed Python resolution lets reset restore the DuckDB default and its extensions after recovery.",
                    "fixture": "The uv wrapper rejects duckdb during startup; its failure marker is removed before plain restart and reset.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                python="answer = 42; print(answer)",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default Python package `duckdb`" in text, text
            assert "synthetic uv failure" in text, text
            assert text.endswith("42\n"), text
            result["content"][0]["text"] = normalize_python_resolution_error(text)
            assert accepted["requirements"]["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
            ], accepted
            assert accepted["requirements"]["duckdb"] == [], accepted

            for action in ("add", "set"):
                result = client.send(
                    control="restart",
                    requirements={"action": action, "python": ["duckdb"]},
                    python="answer = 0",
                )
                assert result.get("isError"), result
                assert "python.managed.resolution=fixed" in last_result_text(client)
            client.expect("42\n", python="answer")
            Path(recorded["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).unlink()
            client.send(control="restart")
            assert inspect(client) == accepted
            client.expect("2\n", python="1 + 1")
            result = client.send(
                control="restart",
                requirements={"action": "reset"},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            restored = inspect(client)
            assert restored == {
                **accepted,
                "requirements": {
                    **accepted["requirements"],
                    "python": [*accepted["requirements"]["python"], "duckdb"],
                    "duckdb": ["icu", "json", "sqlite"],
                },
            }, restored
            client.expect(
                "sqlite available\n",
                python="import duckdb; duckdb.sql('LOAD sqlite'); print('sqlite available')",
            )
            return client.finish()


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_runtime_r_requirement_failure_stays_fatal(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = recording_ir_environment(root, fail_requirement="nanoarrow")
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "Required R infrastructure cannot be installed.",
                    "fixture": "The ir wrapper rejects nanoarrow, including in the required-only declaration.",
                }
            )
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('must not execute')",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert result.get("isError"), result
            assert "synthetic `ir` failure for nanoarrow" in last_result_text(client)
            assert "skipping default" not in last_result_text(client)
            return client.finish()


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_configured_r_default_stays_required(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = recording_ir_environment(root, fail_requirement="tidyverse")
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", 'r.packages=["tidyverse"]'),
            env,
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A configured package stays required even when it is a built-in default.",
                    "fixture": "r.packages explicitly requests tidyverse; the ir wrapper rejects it.",
                }
            )
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('must not execute')",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert result.get("isError"), result
            assert "synthetic `ir` failure for tidyverse" in last_result_text(client)
            assert "skipping default" not in last_result_text(client)
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_configured_python_default_stays_required(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, _ = recording_uv_environment(root, fail_requirement="plotnine")
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary,
            execution.serve(
                "-c", "cache=host", "-c", 'python.managed.packages=["plotnine"]'
            ),
            env,
            root,
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A configured Python package stays required even when it is a built-in default.",
                    "fixture": "python.managed.packages explicitly requests plotnine; the uv wrapper rejects it.",
                }
            )
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('must not execute')",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert result.get("isError"), result
            text = last_result_text(client)
            assert "synthetic uv failure" in text and "skipping default" not in text, (
                text
            )
            result["content"][0]["text"] = normalize_python_resolution_error(text)
            transcript, _ = client.finish_with_standard_error(expected_exit_status=1)
            return transcript


@requires(POSIX, R, command("ir"), command("uv"))
@executions(DIRECT)
def test_failed_default_duckdb_extension_stays_explicitly_required(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env = extension_failure_environment(root)
        env["MCP_CONSOLE_TEST_DUCKDB_FAIL_EXTENSION"] = "sqlite"
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A default DuckDB extension cannot be installed.",
                    "fixture": "The selected Rscript wrapper rejects sqlite extension preparation.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                python="answer = 40; print(answer + 2)",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default DuckDB extension `sqlite`" in text, text
            assert "fixture DuckDB extension sqlite is unavailable" in text, text
            assert text.endswith("42\n"), text
            assert accepted["requirements"]["duckdb"] == ["icu", "json"], accepted
            result = client.send(
                requirements={"duckdb": ["sqlite"]}, python="answer = 0"
            )
            assert result.get("isError"), result
            assert "fixture DuckDB extension sqlite is unavailable" in last_result_text(
                client
            )
            assert inspect(client) == accepted
            client.expect("42\n", python="answer + 2")
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_failed_python_duckdb_default_stays_explicitly_required(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        provider = preseeded_duckdb_python(root)
        site = next((root / "provider/lib").glob("python*/site-packages"))
        fixture = Path(__file__).resolve().parents[3] / "fixtures/fail_duckdb_python.py"
        (site / "sitecustomize.py").write_text(fixture.read_text())
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, _ = recording_uv_environment(root)
        recorded["MCP_CONSOLE_TEST_UV_PYTHON"] = str(provider)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.transcript.append(
                {
                    "scenario": "A default extension fails in a Python-only session.",
                    "fixture": "uv selects a seeded DuckDB environment whose sitecustomize fixture rejects sqlite only in extension preparation.",
                }
            )
            client.initialize_and_list_tools()
            accepted = inspect(client)
            result = client.send(
                python="answer = 40; print(answer + 2)",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not result.get("isError"), result
            text = last_tool_text(client)
            assert "skipping default DuckDB extension `sqlite`" in text, text
            assert "fixture DuckDB extension sqlite is unavailable" in text, text
            assert text.endswith("42\n"), text
            assert accepted["requirements"]["duckdb"] == ["icu", "json"], accepted
            result = client.send(
                requirements={"duckdb": ["sqlite"]}, python="answer = 0"
            )
            assert result.get("isError"), result
            assert "fixture DuckDB extension sqlite is unavailable" in last_result_text(
                client
            )
            assert inspect(client) == accepted
            client.expect("42\n", python="answer + 2")
            return client.finish()
