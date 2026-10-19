"""Independent dependency policies at the public MCP boundary."""

import json
import os
import subprocess
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_selection import configure, venv
from boundaries.client_server.python.test_without_r import environment
from boundaries.client_server.requirements.test_actions import inspect
from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import (
    code,
    normalize_python_traceback_paths,
    normalize_trailing_spaces,
)
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.r import r_test_environment
from support.resolvers import (
    checkpoint_uv_environment,
    ir_run_records,
    recording_ir_environment,
    recording_uv_environment,
    uv_tool_run_requirements,
)


def mixed_environment(root: Path) -> tuple[dict[str, str], Path, Path]:
    env, ir_record = recording_fixture_r_environment(root, ("mcpfirst", "mcpsecond"))
    # The existing fixture overlays synthetic optional packages on real infrastructure.
    base = subprocess.check_output(
        [
            env["MCP_CONSOLE_TEST_REAL_IR"],
            "run",
            "--isolated",
            "--vanilla",
            *[
                argument
                for package in (
                    "DBI",
                    "duckdb",
                    "jsonlite",
                    "nanoarrow",
                    "pillar",
                    "reticulate",
                    "tibble",
                    "utf8",
                )
                for argument in ("--with", package)
            ],
            "-e",
            "cat(normalizePath(.libPaths()[[1L]]))",
        ],
        env=env,
        text=True,
    )
    Path(env["MCP_CONSOLE_TEST_IR_BASE_LIBRARY"]).write_text(base)
    uv_env, uv_record = recording_uv_environment(root)
    env.update(
        {
            key: value
            for key, value in uv_env.items()
            if key.startswith("MCP_CONSOLE_TEST_UV")
            or key in ("RETICULATE_UV", "MCP_CONSOLE_TEST_REAL_UV")
        }
    )
    return env, ir_record, uv_record


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_requires_explicit_python_requirements_by_default(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        recorded, uv_record = recording_uv_environment(root)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            client.expect("Python initialized\n", python="print('Python initialized')")
            startup = inspect(client)
            baseline = uv_tool_run_requirements(uv_record)
            assert baseline, "startup preparation must reach the recording resolver"
            client.send(python="import yaml12")
            assert "ModuleNotFoundError" in last_tool_text(client), last_tool_text(
                client
            )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                normalize_python_traceback_paths(last_tool_text(client))
            )
            client.expect(
                "dynamic imports require declared dependencies\n",
                # fmt: python
                python=code("""
                    import importlib

                    for load in (importlib.import_module, __import__):
                        try:
                            load("yaml12")
                        except ModuleNotFoundError:
                            pass
                        else:
                            raise AssertionError("undeclared import resolved")
                    print("dynamic imports require declared dependencies")
                    """),
            )
            assert inspect(client) == startup
            assert uv_tool_run_requirements(uv_record) == baseline
            client.expect(
                "declared Python package available\n",
                requirements={"python": ["py-yaml12"]},
                python="import yaml12; print('declared Python package available')",
            )
            assert "py-yaml12" in inspect(client)["requirements"]["python"]
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_locked_python_startup_and_fallback(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, uv_record = recording_uv_environment(root)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = environment(tools, inherited=recorded)
        env.update(
            {
                key: value
                for key, value in recorded.items()
                if key.startswith("MCP_CONSOLE_TEST_")
                or key in ("RETICULATE_UV", "UV_TOOL_DIR")
            }
        )
        configure(
            root,
            {
                "first_available": [
                    {"existing": "missing"},
                    {
                        "managed": {
                            "version": "3.13",
                            "packages": ["packaging"],
                            "resolution": "fixed",
                        }
                    },
                ]
            },
            cache="host",
            r={"resolution": "disabled"},
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            early = client.send(
                control="restart",
                requirements={"action": "set"},
                python="raise AssertionError('must not run')",
                stdin="withheld\n",
            )
            assert early.get("isError") and "python.managed.resolution=fixed" in str(
                early
            ), early
            startup = inspect(client)
            assert startup["requirements"]["python_version"] == ["3.13"], startup
            client.expect(
                "startup available\n",
                python="import packaging, os; original_pid = os.getpid(); print('startup available')",
            )
            baseline = uv_tool_run_requirements(uv_record)
            assert baseline and all(
                packages == ["packaging"] for packages in baseline
            ), baseline
            for requirements in (
                {"python": ["six"]},
                {"python_version": [">=3.12"]},
                {"exclude_newer": "2026-01-01"},
                {"action": "set"},
            ):
                result = client.send(
                    control="restart",
                    requirements=requirements,
                    python="raise AssertionError('must not run')",
                    stdin="withheld\n",
                )
                assert result.get(
                    "isError"
                ) and "python.managed.resolution=fixed" in str(result), result
                assert inspect(client) == startup
                assert uv_tool_run_requirements(uv_record) == baseline
                client.expect(
                    "worker retained\n",
                    python="assert os.getpid() == original_pid; print('worker retained')",
                )
            client.send(requirements={"python": ["packaging"]})
            client.send(requirements={"action": "set", **startup["requirements"]})
            client.expect("[prepared]", requirements={"action": "reset"})
            configure(root, {"managed": {"resolution": "implicit"}})
            result = client.send(control="restart", requirements={"action": "reset"})
            assert not result.get("isError"), result
            client.expect(
                "startup available\n",
                python="import packaging, sys; assert sys.version_info[:2] == (3, 13); print('startup available')",
            )
            assert inspect(client) == startup
            assert uv_tool_run_requirements(uv_record) == baseline
            return client.finish()


@requires(POSIX, R, command("uv"), command("ir"))
@executions(DIRECT)
def test_locked_r_admission_and_deferred_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, ir_record, uv_record = mixed_environment(root)
        configure(
            root,
            {"managed": {"version": "3.13", "packages": [], "resolution": "explicit"}},
            cache="host",
            r={"packages": ["mcpfirst"], "resolution": "fixed"},
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            early = client.send(requirements={"action": "set"})
            assert early.get("isError") and "r.resolution=fixed" in str(early), early
            startup = inspect(client)
            client.expect(
                "startup available\n",
                r="stopifnot(mcpfirst::fixture()); original_pid <- Sys.getpid(); cat('startup available\\n')",
            )
            client.expect("Python available\n", python="print('Python available')")
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            for control in (None, "restart"):
                for requirements in (
                    {"r": ["mcpsecond"], "python": ["packaging"]},
                    {"action": "set", "python": ["packaging"]},
                ):
                    args = {} if control is None else {"control": control}
                    result = client.send(
                        **args,
                        requirements=requirements,
                        r="stop('must not run')",
                        stdin="withheld\n",
                    )
                    assert result.get("isError") and "r.resolution=fixed" in str(
                        result
                    ), result
                    assert inspect(client) == startup
                    assert (
                        ir_run_records(ir_record),
                        uv_tool_run_requirements(uv_record),
                    ) == baseline
                    client.expect(
                        "worker retained\n",
                        r="stopifnot(Sys.getpid() == original_pid); cat('worker retained\\n')",
                    )
            client.send(requirements={"python": ["packaging"]})
            client.expect(
                "deliberate Python available\n",
                python="import packaging; print('deliberate Python available')",
            )
            accepted = inspect(client)
            assert accepted["requirements"]["r"] == ["mcpfirst"], accepted
            assert accepted["requirements"]["python"] == ["packaging"], accepted
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            with closing(FifoCheckpoint.create(root / "active")) as active:
                # Publish readiness only after the interrupt handler is installed.
                client.send(
                    # fmt: r
                    r=code(f"""
                        tryCatch({{
                          local({{
                            checkpoint <- file({json.dumps(str(active.path))}, "wb", raw = TRUE)
                            on.exit(close(checkpoint))
                            writeChar("1", checkpoint, eos = NULL)
                          }})
                          Sys.sleep(60)
                        }}, interrupt = function(e) cat("active interrupted\\n"))
                        """),
                    timeout_ms=0,
                )
                active.wait("active R cell")
                result = client.send(
                    control="interrupt",
                    requirements={"r": ["mcpsecond"]},
                    r="followup_ran <- TRUE",
                    timeout_ms=10_000,
                )
                assert result.get("isError") and "r.resolution=fixed" in str(result), (
                    result
                )
                assert "active interrupted" in str(result), result
            client.expect(
                "worker retained\n",
                r="stopifnot(!exists('followup_ran'), Sys.getpid() == original_pid); cat('worker retained\\n')",
            )
            assert inspect(client) == accepted
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<fixture>")
            )


@requires(POSIX, R, command("uv"), command("ir"))
@executions(DIRECT)
def test_explicit_runtime_requests_and_deliberate_additions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, ir_record, uv_record = mixed_environment(root)
        configure(
            root,
            {
                "first_available": [
                    {"existing": "missing"},
                    {"managed": {"packages": []}},
                ]
            },
            cache="host",
            r={"packages": ["mcpfirst"], "resolution": "explicit"},
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            client.send(r="mcpsecond::fixture()")
            assert "there is no package called" in last_tool_text(client), (
                last_tool_text(client)
            )
            assert inspect(client) == startup
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            # A deferred reticulate declaration cannot authorize host preparation.
            client.send(r="reticulate::py_require('six'); reticulate::py_config()")
            assert "python.managed.resolution=explicit" in last_tool_text(client), (
                last_tool_text(client)
            )
            normalize_trailing_spaces(client)
            assert inspect(client) == startup
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            client.send(
                r="reticulate::py_require(packages = character(), action = 'set')"
            )
            client.expect("Python initialized\n", python="print('Python initialized')")
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            client.send(python="import six")
            assert "ModuleNotFoundError" in last_tool_text(client), last_tool_text(
                client
            )
            client.transcript[-1]["result"]["content"][0]["text"] = (
                normalize_python_traceback_paths(last_tool_text(client))
            )
            assert inspect(client) == startup
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            client.send(requirements={"r": ["mcpsecond"], "python": ["six"]})
            client.expect(
                "deliberate packages available\n",
                r="stopifnot(mcpsecond::fixture()); cat('deliberate packages available\\n')",
            )
            client.expect(
                "deliberate Python available\n",
                python="import six; print('deliberate Python available')",
            )
            assert inspect(client)["requirements"]["python"] == ["six"]
            return client.finish()


@requires(POSIX, R, command("uv"), command("ir"))
@executions(DIRECT)
def test_disabled_r_keeps_independent_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, ir_record = recording_ir_environment(root)
        uv_env, uv_record = recording_uv_environment(root)
        env.update(
            {
                key: value
                for key, value in uv_env.items()
                if key.startswith("MCP_CONSOLE_TEST_UV")
                or key in ("RETICULATE_UV", "MCP_CONSOLE_TEST_REAL_UV")
            }
        )
        empty = root / "empty-library"
        empty.mkdir()
        for key in ("R_LIBS", "R_LIBS_USER", "R_LIBS_SITE"):
            env[key] = str(empty)
        (root / ".Rprofile").write_text("native_startup <- 42L\n")
        env["R_PROFILE_USER"] = str(root / ".Rprofile")
        configure(
            root,
            {
                "managed": {
                    "version": "3.13",
                    "packages": ["packaging"],
                    "resolution": "explicit",
                }
            },
            cache="host",
            r={"resolution": "disabled"},
        )
        with McpClient(
            binary, execution.serve(), env, root, use_r_startup_files=True
        ) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            assert (
                startup["requirements"]["r"] == []
                and startup["runtime_requirements"]["r"] == []
            ), startup
            client.expect(
                "native R available\n",
                r="stopifnot(native_startup == 42L, nzchar(find.package('stats')), !length(.libPaths()[grepl('resolved-r', .libPaths())])); cat('native R available\\n')",
            )
            client.expect(
                "Python available\n",
                python="import packaging; print('Python available')",
            )
            baseline = uv_tool_run_requirements(uv_record)
            result = client.send(
                control="restart",
                requirements={"r": ["praise"], "python": ["six"]},
                python="raise AssertionError('must not run')",
            )
            assert result.get("isError") and "r.resolution=disabled" in str(result), (
                result
            )
            assert inspect(client) == startup
            assert uv_tool_run_requirements(uv_record) == baseline
            result = client.send(requirements={"duckdb": ["fts"]})
            assert result.get("isError") and "r.resolution=disabled" in str(result), (
                result
            )
            client.send(requirements={"python": ["six"]})
            client.expect(
                "Python addition available\n",
                python="import six; print('Python addition available')",
            )
            client.send(requirements={"action": "get"})
            assert ir_run_records(ir_record) == [], ir_run_records(ir_record)
            assert not (root / "resolved-r-libraries").exists()
            return client.finish()


@requires(POSIX, R)
@executions(DIRECT)
def test_promised_r_startup_requires_tooling(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        selected = venv(root)
        env, _ = r_test_environment()
        empty = root / "empty-library"
        empty.mkdir()
        env.update(
            PATH=os.defpath,
            R_LIBS=str(empty),
            R_LIBS_USER=str(empty),
            R_LIBS_SITE=str(empty),
        )
        env.pop("RETICULATE_UV", None)
        for mode, packages in (("explicit", None), ("fixed", [])):
            r = {"resolution": mode}
            if packages is not None:
                r["packages"] = packages
            configure(root, {"existing": str(selected)}, r=r)
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                result = client.send(requirements={"action": "get"})
                assert result.get("isError") and f"r.resolution={mode}" in str(
                    result
                ), result
                assert "structuredContent" not in result, result
                client.finish_with_standard_error(expected_exit_status=1)
                records.append({"mode": mode, "packages": packages, "result": result})
    return records


@requires(POSIX)
@executions(DIRECT)
def test_unreached_managed_policy_keeps_existing_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        selected = venv(root)
        tools = root / "tools"
        tools.mkdir()
        configure(
            root,
            {
                "first_available": [
                    {"existing": str(selected)},
                    {
                        "managed": {
                            "resolution": "fixed",
                            "packages": ["unavailable-fixture"],
                            "version": "9.99",
                        }
                    },
                ]
            },
            r={"resolution": "disabled"},
        )
        with McpClient(binary, execution.serve(), environment(tools), root) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            assert (
                startup["requirements"]["python"] == []
                and startup["requirements"]["python_version"] == []
            ), startup
            result = client.send(requirements={"python": ["six"]})
            assert result.get("isError") and "non-managed Python session" in str(
                result
            ), result
            client.expect(
                "existing Python retained\n", python="print('existing Python retained')"
            )
            client.send(control="restart")
            assert inspect(client) == startup
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_python_provider_extension_policy(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        recorded, uv_record = recording_uv_environment(root)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = {
            **environment(tools),
            **{
                key: value
                for key, value in recorded.items()
                if key.startswith("MCP_CONSOLE_TEST_")
                or key in ("RETICULATE_UV", "UV_TOOL_DIR")
            },
        }
        configure(
            root,
            {"managed": {"packages": ["duckdb"], "resolution": "fixed"}},
            cache="host",
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            client.expect(
                "installed extension usable\n",
                python="import duckdb; duckdb.sql('LOAD json'); print('installed extension usable')",
            )
            baseline = uv_tool_run_requirements(uv_record)
            result = client.send(requirements={"duckdb": ["fts"]})
            assert result.get("isError") and "python.managed.resolution=fixed" in str(
                result
            ), result
            assert inspect(client) == startup
            assert uv_tool_run_requirements(uv_record) == baseline
            client.send(sql="SELECT 42 AS answer")
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            return client.finish()


@requires(POSIX, R, command("uv"), command("ir"))
@executions(DIRECT)
def test_deferred_runtime_declaration_cannot_piggyback_on_mcp(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        env, ir_record, uv_record = mixed_environment(root)
        env["MCP_CONSOLE_LANGUAGES"] = "r"
        configure(
            root,
            {"managed": {"packages": [], "resolution": "explicit"}},
            cache="host",
            r={"packages": [], "resolution": "fixed"},
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            client.send(
                r="reticulate::py_require(python_version = '3.13', exclude_newer = '2026-01-01'); reticulate::py_write_requirements(NULL, NULL, freeze = FALSE)"
            )
            assert "python.managed.resolution=explicit" in last_tool_text(client), (
                last_tool_text(client)
            )
            assert inspect(client) == startup
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            client.send(
                r="reticulate::py_require(python_version = character(), exclude_newer = '', action = 'set')"
            )
            client.expect(
                r="reticulate::py_require('six'); stopifnot(!reticulate::py_available(initialize = FALSE))"
            )
            baseline = (ir_run_records(ir_record), uv_tool_run_requirements(uv_record))
            result = client.send(requirements={"python": ["packaging"]})
            assert result.get(
                "isError"
            ) and "python.managed.resolution=explicit" in str(result), result
            assert inspect(client) == startup
            assert (
                ir_run_records(ir_record),
                uv_tool_run_requirements(uv_record),
            ) == baseline
            client.send(
                r="reticulate::py_require(packages = character(), action = 'set')"
            )
            client.send(requirements={"python": ["packaging"]})
            client.expect(
                "authorized Python initialized\n",
                r="stopifnot(reticulate::py_eval('__import__(\"packaging\").__name__') == 'packaging'); cat('authorized Python initialized\\n')",
            )
            assert inspect(client)["requirements"]["python"] == ["packaging"]
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_early_locked_request_waits_for_configured_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        checkpoint_env, started, release = checkpoint_uv_environment(root, "packaging")
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        (tools / "uv").symlink_to(checkpoint_env["RETICULATE_UV"])
        env = {
            **environment(tools),
            **{
                key: value
                for key, value in checkpoint_env.items()
                if key.startswith("MCP_CONSOLE_TEST_") or key == "UV_TOOL_DIR"
            },
        }
        configure(
            root,
            {
                "managed": {
                    "version": "3.13",
                    "packages": ["packaging"],
                    "resolution": "fixed",
                }
            },
            cache="host",
        )
        with (
            closing(started),
            closing(release),
            McpClient(binary, execution.serve(), env, root) as client,
        ):
            client.initialize_and_list_tools()
            started.wait("configured initial Python preparation")
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="early_cell_ran = True",
                stdin="withheld\n",
            )
            release.release()
            client.receive(pending)
            assert pending["result"].get(
                "isError"
            ) and "python.managed.resolution=fixed" in str(pending["result"]), pending
            startup = inspect(client)
            assert startup["requirements"]["python"] == ["packaging"] and startup[
                "requirements"
            ]["python_version"] == ["3.13"], startup
            client.expect(
                "configured startup retained\n",
                python="import packaging; assert 'early_cell_ran' not in globals(); print('configured startup retained')",
            )
            client.send(requirements={"action": "set", **startup["requirements"]})
            assert inspect(client) == startup
            return client.finish()


@requires(POSIX, command("uv"))
@executions(DIRECT)
def test_failed_initial_preparation_retries_locked_baseline(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        recorded, uv_record = recording_uv_environment(
            root, fail_requirement="packaging"
        )
        tools = root / "tools"
        tools.mkdir()
        (tools / "python3").symlink_to(sys.executable)
        (tools / "uv").symlink_to(recorded["RETICULATE_UV"])
        env = {
            **environment(tools),
            **{
                key: value
                for key, value in recorded.items()
                if key.startswith("MCP_CONSOLE_TEST_") or key == "UV_TOOL_DIR"
            },
        }
        configure(
            root,
            {
                "managed": {
                    "version": "3.13",
                    "packages": ["packaging"],
                    "resolution": "fixed",
                }
            },
            cache="host",
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            failed = client.send(requirements={"action": "get"})
            assert failed.get("isError") and "synthetic uv failure" in str(failed), (
                failed
            )
            Path(recorded["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).unlink()
            configure(
                root, {"managed": {"packages": ["six"], "resolution": "implicit"}}
            )
            result = client.send(
                control="restart",
                requirements={"action": "set"},
                python="raise AssertionError('must not run')",
            )
            assert result.get("isError") and "python.managed.resolution=fixed" in str(
                result
            ), result
            startup = inspect(client)
            assert startup["requirements"]["python"] == ["packaging"] and startup[
                "requirements"
            ]["python_version"] == ["3.13"], startup
            client.expect(
                "retry retained startup\n",
                python="import packaging; print('retry retained startup')",
            )
            baseline = uv_tool_run_requirements(uv_record)
            assert baseline == [["packaging"], ["packaging"]], baseline
            client.send(control="restart")
            client.expect(
                "startup still usable\n",
                python="import packaging; print('startup still usable')",
            )
            assert uv_tool_run_requirements(uv_record) == baseline
            return client.finish()
