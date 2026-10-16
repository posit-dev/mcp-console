"""Configured startup declarations through the public MCP interface."""

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.assertions import wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import expose_uv, recording_uv_environment
from support.r import r_test_environment
from boundaries.client_server.requirements.test_actions import inspect
from boundaries.client_server.python.test_selection import configure, venv


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_empty_python_startup_and_reset(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded_env, _ = recording_uv_environment(root, fail_requirement="six")
        (tools / "uv").symlink_to(recorded_env["RETICULATE_UV"])
        env = environment(tools)
        env.update(
            {
                name: value
                for name, value in recorded_env.items()
                if name.startswith("MCP_CONSOLE_TEST_")
                or name in ("RETICULATE_UV", "UV_TOOL_DIR")
            }
        )
        console_home = root / "console-home"
        console_home.mkdir()
        (console_home / "config.yaml").write_text(
            json.dumps(
                {"python": {"managed": {"version": "9.99", "packages": ["numpy"]}}}
            )
        )
        configure(root, {"managed": {"packages": ["packaging"]}})
        env["MCP_CONSOLE_HOME"] = str(console_home)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                "-c",
                'python.managed.version="3.13"',
                "-c",
                "python.managed.packages=[]",
            ),
            env,
            root,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            # fmt: python
            check = code("""
                import importlib.util
                import sys

                assert sys.version_info[:2] == (3, 13)
                assert importlib.util.find_spec("duckdb") is None
                assert importlib.util.find_spec("numpy") is None
                assert importlib.util.find_spec("packaging") is None
                print("configured Python available")
                """)
            client.expect("configured Python available\n", python=check)
            result = client.send(requirements={"action": "get"})
            assert result["structuredContent"]["requirements"] == {
                "r": [],
                "python": [],
                "duckdb": [],
                "python_version": ["3.13"],
                "exclude_newer": None,
            }, result
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["packaging"]},
            )
            client.expect(
                "packaging available\n",
                python="import packaging; import os; original_pid = os.getpid(); print('packaging available')",
            )
            accepted = inspect(client)
            result = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "python": ["six"],
                    "python_version": ["3.13"],
                },
            )
            assert result.get("isError") and "synthetic uv failure" in str(result), (
                result
            )
            assert inspect(client) == accepted
            client.expect(
                "worker retained\n",
                python="assert os.getpid() == original_pid; print('worker retained')",
            )
            client.send(control="restart", requirements={"action": "reset"})
            client.expect("configured Python available\n", python=check)
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_conditional_duckdb_and_managed_fallback(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        expose_uv(tools)
        packages = [
            "packaging",
            "duckdb; python_version < '2'",
            "duckdb; sys_platform == 'win32'",
        ]
        configure(
            root,
            {
                "first_available": [
                    {"existing": "missing"},
                    {"managed": {"version": ">=3.12,<3.14", "packages": packages}},
                ]
            },
            cache="host",
            r={"packages": []},
        )
        with McpClient(binary, execution.serve(), environment(tools), root) as client:
            client.initialize_and_list_tools()
            # fmt: python
            check = code("""
                import importlib.util
                import sys

                assert sys.version_info.major == 3 and 12 <= sys.version_info.minor < 14
                assert importlib.util.find_spec("duckdb") is None
                assert importlib.util.find_spec("packaging") is not None
                assert "packaging" not in sys.modules
                print("conditional startup available")
                """)
            client.expect("conditional startup available\n", python=check)
            startup = inspect(client)
            assert startup["requirements"]["python"] == sorted(packages), startup
            assert startup["requirements"]["duckdb"] == [], startup
            result = client.send(requirements={"duckdb": ["json"]})
            assert result.get(
                "isError"
            ) and "include duckdb in requirements.python" in str(result), result
            assert inspect(client) == startup
            client.send(control="restart", requirements={"action": "set"})
            client.send(control="restart", requirements={"action": "reset"})
            client.expect("conditional startup available\n", python=check)
            assert inspect(client) == startup
            return client.finish()


@requires(POSIX)
@executions(DIRECT)
def test_unreached_managed_options_are_not_prepared(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        selected = venv(root)
        tools = root / "tools"
        tools.mkdir()
        choice = {
            "first_available": [
                {"existing": str(selected)},
                {
                    "managed": {
                        "version": "9.99",
                        "packages": ["mcp-console-unavailable-fixture"],
                    }
                },
            ]
        }
        configure(root, choice)
        with McpClient(binary, execution.serve(), environment(tools), root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "selected existing Python\n", python="print('selected existing Python')"
            )
            assert inspect(client)["requirements"]["python_version"] == []
            result = client.send(control="restart")
            assert not result.get("isError"), result
            client.expect(
                "selected existing Python\n", python="print('selected existing Python')"
            )
            return client.finish()


@requires(R, command("uv"), command("ir"))
@executions(DIRECT)
def test_configured_mixed_startup_additions_restart_reset_and_recording(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, _ = r_test_environment()
        env.pop("RETICULATE_PYTHON", None)
        configure(
            root,
            {"managed": {"version": "3.13", "packages": ["packaging"]}},
            r={"packages": ["praise"]},
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code("""
                stopifnot(
                  !("praise" %in% loadedNamespaces()),
                  nzchar(find.package("praise")),
                  !("package:praise" %in% search())
                )
                cat("configured R available\\n")
                """)
            # fmt: python
            python = code("""
                import importlib.util
                import sys

                assert sys.version_info[:2] == (3, 13)
                assert importlib.util.find_spec("packaging") is not None
                assert "packaging" not in sys.modules
                print("configured Python available")
                """)
            client.expect("configured R available\n", r=r)
            client.expect("configured Python available\n", python=python)
            startup = inspect(client)
            assert startup["requirements"]["r"] == ["praise"], startup
            assert startup["requirements"]["python"] == ["packaging"], startup
            assert startup["requirements"]["python_version"] == ["3.13"], startup
            client.expect(
                "addition available\n",
                python="import idna; print('addition available')",
                requirements={"r": ["zeallot"], "python": ["idna"]},
            )
            accepted = inspect(client)
            assert accepted["requirements"]["r"] == ["praise", "zeallot"], accepted
            assert accepted["requirements"]["python"] == ["idna", "packaging"], accepted
            configure(root, {"managed": {"packages": []}}, r={"packages": []})
            client.send(control="restart")
            assert inspect(client) == accepted
            client.expect(
                "addition retained\n", python="import idna; print('addition retained')"
            )
            client.send(control="restart", requirements={"action": "set"})
            assert inspect(client)["requirements"]["python"] == []
            client.send(control="restart", requirements={"action": "reset"})
            assert inspect(client) == startup
            client.expect("configured R available\n", r=r)
            client.expect("configured Python available\n", python=python)
            transcript = client.finish()
        (session,) = (root / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (session / "internal/events.jsonl").read_text().splitlines()
        ]
        (recorded,) = [
            event["startup_requirements"]
            for event in events
            if event["event"] in ("session_started", "environment_discovered")
            and event["startup_requirements"] is not None
        ]
        assert recorded == startup["requirements"], recorded
        quarto = (session / "transcript.qmd").read_text()
        assert "    - praise\n" in quarto and "    - packaging\n" in quarto, quarto
        assert "    - tidyverse\n" not in quarto and "    - plotnine\n" not in quarto, (
            quarto
        )
        return transcript


@requires(R, POSIX, command("uv"), command("ir"))
@executions(DIRECT)
def test_layered_r_packages_and_omitted_python_defaults(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        console_home = root / "console-home"
        console_home.mkdir()
        (console_home / "config.yaml").write_text(
            json.dumps({"r": {"packages": ["praise", "zeallot"]}})
        )
        configure(root, {"managed": {}}, r={"packages": ["praise"]})
        env, _ = r_test_environment()
        env["MCP_CONSOLE_HOME"] = str(console_home)
        executable = Path(env["R_HOME"]) / "bin/R"
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r=" + json.dumps(str(executable)),
                "-c",
                "r.packages=[]",
            ),
            env,
            root,
            use_home_configuration=True,
        ) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            assert startup["requirements"]["r"] == [], startup
            assert startup["requirements"]["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
            ], startup
            client.expect(
                "Python defaults available\n",
                python="import importlib.util; assert importlib.util.find_spec('numpy') is not None; print('Python defaults available')",
            )
            client.send(control="restart", requirements={"action": "set"})
            client.send(control="restart", requirements={"action": "reset"})
            assert inspect(client) == startup
            return client.finish()


@requires(R, command("uv"), command("ir"))
@executions(DIRECT)
def test_early_set_retains_configured_reset_baseline(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        configure(root, {"managed": {"packages": []}}, r={"packages": ["praise"]})
        env, _ = r_test_environment()
        env.pop("RETICULATE_PYTHON", None)
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("[prepared]", requirements={"action": "set"})
            assert inspect(client)["requirements"]["r"] == []
            wait_for_worker_ready(client, "worker after early requirements set")
            client.expect("[prepared]", requirements={"action": "reset"})
            startup = inspect(client)
            assert startup["requirements"]["r"] == ["praise"], startup
            assert startup["requirements"]["python"] == [], startup
            client.expect(
                "configured R available\n",
                r="stopifnot(requireNamespace('praise', quietly = TRUE)); cat('configured R available\\n')",
            )
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT)
def test_requested_r_packages_require_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        selected = venv(root)
        tools = root / "tools"
        tools.mkdir()
        environments = {"without R": environment(tools)}
        bare, _ = r_test_environment()
        bare.update(
            PATH=os.defpath,
            R_LIBS=str(root / "empty-library"),
            R_LIBS_USER=str(root / "empty-library"),
            R_LIBS_SITE=str(root / "empty-library"),
        )
        bare.pop("RETICULATE_UV", None)
        (root / "empty-library").mkdir()
        environments["without R preparation"] = bare
        for label, env in environments.items():
            configure(root, {"existing": str(selected)}, r={"packages": ["praise"]})
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                result = client.send(python="raise AssertionError('must not execute')")
                assert result.get("isError"), result
                assert (
                    "r.packages: requested startup packages require R and R preparation support"
                    in str(result)
                ), result
                client.finish_with_standard_error(expected_exit_status=1)
                records.append({"configuration": label, "result": result})
        return records
