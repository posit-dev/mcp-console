#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, SQL, requires
from support.assertions import last_result_text
from support.previews import (
    assert_preview,
    compact_previews,
)
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import (
    code,
    normalize_python_resolution_error,
    normalize_python_traceback_paths,
)
from support.records import Transcript
from support.resolvers import (
    initialize_python_and_record_baseline,
    recording_uv_environment,
    resolve_managed_python,
    send_and_collect_runtime_python_resolution,
    uv_tool_run_requirements,
)
from support.suites import run_this_suite

PENDING_TEXT_BUDGET = 8 * 1024 * 1024


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_resolves_missing_python_import_without_replaying_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()

    client.expect(r="automatic_python_r_state <- 42L")
    client.send(sql="CREATE TABLE automatic_python_state AS SELECT 42 AS answer")

    # fmt: python
    setup = code("""
        import os

        automatic_python_object = {"answer": 42}
        automatic_python_pid = os.getpid()
        """)
    client.expect(python=setup)

    # fmt: python
    python = code("""
        automatic_python_attempts = globals().get("automatic_python_attempts", 0) + 1
        print("prefix", end="")
        import yaml12

        assert automatic_python_attempts == 1
        automatic_python_input = input()
        (
            yaml12.__name__,
            automatic_python_object["answer"],
            __import__("os").getpid() == automatic_python_pid,
            automatic_python_input,
        )
        """)
    client.send(python=python, stdin="42\n")
    output = last_result_text(client)
    assert output == (
        "prefix\n"
        "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
        '[input requested: ""]\n'
        "('yaml12', 42, True, '42')\n"
    ), repr(output)
    assert "[prepared]" not in output

    client.send(r="automatic_python_r_state")
    assert last_result_text(client) == "[1] 42\n"
    client.send(sql="SELECT answer FROM automatic_python_state")
    assert last_result_text(client).splitlines()[-1].split() == ["1", "42"]
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_mapped_resolution_notice_atomic_at_output_limit(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()

    retained = PENDING_TEXT_BUDGET - 8
    # fmt: python
    python = code(f"""
        print("x" * {retained}, end="")
        import yaml12

        yaml12.__name__
        """)
    client.send(python=python, timeout_ms=120_000)
    output = last_result_text(client)
    prefix = "x" * retained
    notice = "\n[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
    assert notice in output
    assert_preview(output, prefix + notice + "'yaml12'\n")
    assert client.temporary_directory is not None
    workspace = Path(client.temporary_directory.name)
    session = next((workspace / ".agents/console" / "sessions").iterdir())
    relative_output = Path("outputs/call-000001.log")
    public_output = (
        f".agents/console/sessions{os.sep}{session.name}{os.sep}outputs/call-000001.log"
    )
    assert f"raw log: {public_output}" in output, output[len(prefix) :]
    assert (session / relative_output).read_text(encoding="utf-8") == (
        prefix + "'yaml12'\n"
    )
    client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
        public_output, public_output.replace(os.sep, "/")
    ).replace(session.name, "<run ID>")
    compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_distinguishes_omitted_resolution_notice_from_retained_raw_text(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            print("notice head\\n" + "x" * 10000, end="")
            import yaml12

            print("y" * 10000 + "\\nnotice tail")
            """)
        client.send(python=python, timeout_ms=120_000)
        output = last_result_text(client)
        head = "notice head\n" + "x" * 10000
        tail = "y" * 10000 + "\nnotice tail\n"
        notice = (
            "\n[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
        )
        assert_preview(output, head + notice + tail)
        assert "generated notice bytes unavailable" in output
        assert "resolved PyPI distribution" not in output
        assert client.temporary_directory is not None
        session = next(
            (
                Path(client.temporary_directory.name) / ".agents/console/sessions"
            ).iterdir()
        )
        assert (session / "outputs/call-000001.log").read_text() == head + tail
        public_output = f".agents/console/sessions{os.sep}{session.name}{os.sep}outputs/call-000001.log"
        assert f"raw log: {public_output}" in output
        client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
            public_output, public_output.replace(os.sep, "/")
        ).replace(session.name, "<run ID>")
        compact_previews(client, "x", "y", "z", "s", "p", "ab", "�")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retries_new_meta_path_finders_after_automatic_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    module = "mcp_console_activated_finder"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            substitute_requirement=(module, "pydash"),
        )
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code(rf"""
            import importlib.util


            class AutomaticMetaLoader:
                def create_module(self, specification):
                    return None

                def exec_module(self, module):
                    module.answer = 42


            class AutomaticMetaFinder:
                def find_spec(self, fullname, path=None, target=None):
                    if fullname == "{module}":
                        return importlib.util.spec_from_loader(
                            fullname,
                            AutomaticMetaLoader(),
                        )
                    return None


            automatic_meta_finder = AutomaticMetaFinder()
            """)
        client.expect(python=python)

        # Activation runs ordinary Python code. Install a finder while the
        # original import is waiting, after the new environment is active.
        # fmt: python
        python = code(r"""
            import runpy
            import sys

            original_run_path = runpy.run_path

            def activate_with_finder(*args, **kwargs):
                result = original_run_path(*args, **kwargs)
                sys.meta_path.insert(0, automatic_meta_finder)
                runpy.run_path = original_run_path
                return result

            runpy.run_path = activate_with_finder
            """)
        client.expect(python=python)

        output = send_and_collect_runtime_python_resolution(
            client,
            python=f"import {module}; {module}.answer",
        )
        assert output == "42\n", repr(output)
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and module in runs[0], runs
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
def test_infers_python_distributions_for_normal_import_forms(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import yaml
            from yaml import safe_load
            from yaml12 import Yaml
            import importlib

            yaml_from_importlib = importlib.import_module("yaml")
            pydash = importlib.import_module("pydash")
            (
                yaml.safe_load("answer: 42"),
                safe_load("answer: 43"),
                yaml_from_importlib is yaml,
                Yaml.__name__,
                pydash.__name__,
            )
            """)
        output = send_and_collect_runtime_python_resolution(client, python=python)
        assert output == (
            "[resolved PyPI distribution 'pyyaml' for Python import 'yaml']\n"
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "({'answer': 42}, {'answer': 43}, True, 'Yaml', 'pydash')\n"
        ), repr(output)
        assert "[prepared]" not in output

        runs = uv_tool_run_requirements(record)[baseline:]
        inferred = ("pyyaml", "py-yaml12", "pydash")
        assert len(runs) == len(inferred), runs
        for index, (run, requirement) in enumerate(zip(runs, inferred, strict=True)):
            assert run.count(requirement) == 1, run
            for retained in inferred[: index + 1]:
                assert run.count(retained) == 1, run
            assert all(later not in run for later in inferred[index + 1 :]), run
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
def test_does_not_resolve_unreached_or_available_python_imports(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "mcp_console_local_module.py").write_text(
            "answer = 42\n",
            encoding="utf-8",
        )
        (directory / "mcp_console_local_requires_pydash.py").write_text(
            "import pydash\nanswer = pydash.get({'answer': 42}, 'answer')\n",
            encoding="utf-8",
        )
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
            current_directory=directory,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            if False:
                import mcp_console_not_a_real_package


            def never_called():
                import mcp_console_also_not_a_real_package


            import importlib.util
            import json
            import numpy
            import pandas
            import mcp_console_local_module

            (
                json.loads("42"),
                numpy.__name__,
                pandas.__name__,
                mcp_console_local_module.answer,
                importlib.util.find_spec("mcp_console_probe_missing") is None,
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == ("(42, 'numpy', 'pandas', 42, True)\n"), repr(output)
        runs = uv_tool_run_requirements(record)
        assert len(runs) == baseline, (baseline, runs)

        output = send_and_collect_runtime_python_resolution(
            client,
            python=(
                "import mcp_console_local_requires_pydash; "
                "mcp_console_local_requires_pydash.answer"
            ),
        )
        assert output == "42\n"
        assert len(uv_tool_run_requirements(record)) == baseline + 1

        output = send_and_collect_runtime_python_resolution(
            client,
            python="import yaml12; yaml12.__name__",
        )
        assert output == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "'yaml12'\n"
        )
        resolved = len(uv_tool_run_requirements(record))
        assert resolved == baseline + 2

        client.send(python="import yaml12; yaml12.__name__")
        assert last_result_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client.finish()


@requires(POSIX, SQL)
@executions(RUNTIME)
def test_does_not_resolve_missing_python_imports_from_sql(
    binary: Path,
    execution: Execution,
) -> Transcript:
    prefix = "mcp_console_sql_missing_"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            fail_requirement=prefix,
        )
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)
        client.expect(sql="CREATE TABLE managed_restore_value AS SELECT 42 AS answer")

        # Exercise every driver-controlled call made by the DB-API adapter.
        # fmt: python
        python = code(f"""
            import importlib

            sql_import_stages = []


            def miss(stage):
                sql_import_stages.append(stage)
                try:
                    importlib.import_module("{prefix}" + stage)
                except ModuleNotFoundError:
                    pass


            class ColumnName:
                def __str__(self):
                    miss("name")
                    return "answer"


            class Value:
                def __repr__(self):
                    miss("repr")
                    return "42"


            class Cursor:
                @property
                def description(self):
                    miss("description")
                    return ((ColumnName(),),)

                def execute(self, source):
                    miss("execute")
                    return self

                def fetchmany(self, size):
                    miss("fetch")
                    return [(Value(),)][:size]

                def close(self):
                    miss("close")


            class Connection:
                def cursor(self):
                    miss("cursor")
                    return Cursor()


            _console.sql_connection(Connection())
            """)
        client.expect(python=python)

        client.send(sql="ANSWER")
        preview = last_result_text(client)
        assert "answer" in preview and "42" in preview, preview

        # Replacing the retained cursor exercises cleanup inside SQL's
        # automatic-resolution boundary, alongside the other driver callbacks.
        client.send(sql="ANSWER AGAIN")
        client.send(python="sorted(set(sql_import_stages))")
        assert last_result_text(client) == (
            "['close', 'cursor', 'description', 'execute', 'fetch', 'name', 'repr']\n"
        )
        runs = uv_tool_run_requirements(record)
        assert len(runs) == baseline, (baseline, runs)

        # Keep the lazy transition back to managed DuckDB inside the SQL
        # exception and automatic-resolution boundary.
        # fmt: python
        python = code("""
            import _mcp_console_sql
            import sys

            use_r_code = _mcp_console_sql.use_r.__code__


            def restore_hook(frame, event, argument):
                if event == "call" and frame.f_code is use_r_code:
                    miss("restore")
                    raise SystemExit("managed SQL restoration exit")
                return restore_hook


            _console.sql_connection(None)
            sys.settrace(restore_hook)
            """)
        client.expect(python=python)

        output = send_and_collect_runtime_python_resolution(
            client,
            sql="SELECT answer FROM managed_restore_value",
        )
        assert "SystemExit: managed SQL restoration exit" in output, output

        client.send(python="sql_import_stages[-1]")
        assert last_result_text(client) == "'restore'\n"

        client.send(sql="SELECT answer FROM managed_restore_value")
        preview = last_result_text(client)
        assert "answer" in preview and "42" in preview, preview
        runs = uv_tool_run_requirements(record)
        assert len(runs) == baseline, (baseline, runs)

        module = f"{prefix}python_cell"
        # fmt: python
        python = code(f"""
            try:
                importlib.import_module("{module}")
            except ModuleNotFoundError:
                pass
            """)
        output = send_and_collect_runtime_python_resolution(client, python=python)
        assert "Traceback" not in output, output
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and module in runs[0], runs
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
def test_does_not_reenter_automatic_python_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    nested = "mcp_console_nested_resolution_missing"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # A missing import inside the activation script must not reenter
        # resolution. Use ordinary Python's script hook in every composition.
        # fmt: python
        python = code(rf"""
            import runpy
            original_run_path = runpy.run_path
            automatic_nested_calls = 0
            automatic_nested_error = None

            def activate_with_nested_import(*args, **kwargs):
                global automatic_nested_calls, automatic_nested_error
                automatic_nested_calls += 1
                try:
                    import {nested}
                except ModuleNotFoundError as error:
                    automatic_nested_error = str(error)
                return original_run_path(*args, **kwargs)

            runpy.run_path = activate_with_nested_import
            """)
        client.expect(python=python)

        output = send_and_collect_runtime_python_resolution(
            client,
            python="import yaml12; yaml12.__name__",
        )
        assert output == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "'yaml12'\n"
        )
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and "py-yaml12" in runs[0], runs

        client.send(
            python=f"assert automatic_nested_calls == 1; assert '{nested}' in automatic_nested_error; print('nested resolution suppressed')"
        )
        assert last_result_text(client) == "nested resolution suppressed\n"
        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retains_automatic_python_requirement_after_error_and_restart(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import yaml12

            raise RuntimeError("after automatic Python activation")
            """)
        output = send_and_collect_runtime_python_resolution(client, python=python)
        assert "RuntimeError: after automatic Python activation" in output
        resolved = len(uv_tool_run_requirements(record))
        assert resolved == baseline + 1

        client.send(python="import yaml12; yaml12.__name__")
        assert last_result_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved

        client.send(control="restart")
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(python="import yaml12; yaml12.__name__")
        assert last_result_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_automatic_python_resolution_failure(
    binary: Path, execution: Execution
) -> Transcript:
    requirement = "scikit-learn"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            fail_requirement=requirement,
        )
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import json
            import os
            import sys

            prior_module = json
            prior_state = {"answer": 42}
            prior_pid = os.getpid()
            print(sys.executable)
            """)
        client.send(python=python)
        executable = last_result_text(client).strip()
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"

        # fmt: python
        python = code("""
            automatic_failure_attempts = globals().get("automatic_failure_attempts", 0) + 1
            prior_state["answer"] += 1
            import sklearn

            automatic_failure_suffix = True
            """)
        output = send_and_collect_runtime_python_resolution(
            client,
            python=python,
        )
        for expected in (
            "ModuleNotFoundError",
            "sklearn",
            requirement,
            "synthetic uv failure",
            "requirements.python",
            'requirements: {"python": ["scikit-learn"]}',
            "Import names and PyPI distribution names can differ",
        ):
            assert expected in output, (expected, output)
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1, runs
        assert requirement in runs[0] and "sklearn" not in runs[0], runs

        normalized = normalize_python_resolution_error(output, executable=executable)
        client.transcript[-1]["result"]["content"][0]["text"] = normalized

        client.send(r=f'"{requirement}" %in% reticulate::py_require()$packages')
        assert last_result_text(client) == "[1] FALSE\n"
        # fmt: python
        python = code("""
            import json

            (
                json is prior_module,
                json.loads('{"answer": 42}')["answer"],
                prior_state["answer"],
                automatic_failure_attempts,
                "automatic_failure_suffix" not in globals(),
                os.getpid() == prior_pid,
            )
            """)
        client.send(python=python)
        assert last_result_text(client) == "(True, 42, 43, 1, True, True)\n"
        assert len(uv_tool_run_requirements(record)) == baseline + 1
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retains_inferred_distribution_that_does_not_provide_import(
    binary: Path,
    execution: Execution,
) -> Transcript:
    inferred = "mcp_console_distribution_without_module"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory,
            substitute_requirement=(inferred, "py-yaml12"),
        )
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(python=f"import {inferred}")
        output = last_result_text(client)
        for expected in (
            "ModuleNotFoundError",
            inferred,
            f"prepared the inferred PyPI distribution `{inferred}`",
            "did not provide the import",
            "requirements.python",
            'requirements: {"python": ["correct-distribution-name"]}',
        ):
            assert expected in output, (expected, output)
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_traceback_paths(output)
        )
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1 and inferred in runs[0], runs
        resolved = len(uv_tool_run_requirements(record))

        client.send(r=f'"{inferred}" %in% reticulate::py_require()$packages')
        assert last_result_text(client) == "[1] TRUE\n"
        client.send(python="import yaml12; yaml12.__name__")
        assert last_result_text(client) == "'yaml12'\n"
        assert len(uv_tool_run_requirements(record)) == resolved

        client.send(control="restart")
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.send(r=f'"{inferred}" %in% reticulate::py_require()$packages')
        assert last_result_text(client) == "[1] TRUE\n"
        assert len(uv_tool_run_requirements(record)) == resolved
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_explicit_python_requirements_preempt_automatic_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(
            python="import yaml12; yaml12.__name__",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_result_text(client) == "'yaml12'\n"
        runs = uv_tool_run_requirements(record)[baseline:]
        assert len(runs) == 1, runs
        assert runs[0].count("py-yaml12") == 1, runs
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
def test_requires_explicit_python_requirements_for_ambiguous_or_installed_roots(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = directory / "mcp_console_available_root"
        package.mkdir()
        (package / "__init__.py").write_text("answer = 42\n", encoding="utf-8")
        fromlist_parent = directory / "mcp_console_fromlist_parent"
        fromlist_parent.mkdir()
        (fromlist_parent / "__init__.py").write_text("", encoding="utf-8")
        (fromlist_parent / "missing.py").write_text(
            # fmt: python
            code("""
                try:
                    import mcp_console_available_root.nested_missing
                except ModuleNotFoundError as error:
                    missing_name = error.name
                    answer = 42
                """),
            encoding="utf-8",
        )
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
            current_directory=directory,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            try:
                import azure.mcp_console_missing
            except ModuleNotFoundError as error:
                print(f"ambiguous name: {error.name}")
                print(error)

            import mcp_console_available_root

            assert mcp_console_available_root.answer == 42
            try:
                import mcp_console_available_root.missing
            except ModuleNotFoundError as error:
                print(f"installed-root name: {error.name}")
                print(error)

            try:
                from mcp_console_available_root import missing
            except ImportError as error:
                print(f"from-import name: {error.name}")
                print(error)

            from mcp_console_fromlist_parent import missing as nested_direct

            assert nested_direct.answer == 42
            print(f"nested-direct name: {nested_direct.missing_name}")
            """)
        client.send(python=python)
        output = last_result_text(client)
        for expected in (
            "ambiguous name: azure",
            "could not safely infer a PyPI distribution",
            "installed-root name: mcp_console_available_root.missing",
            "from-import name: mcp_console_available_root.missing",
            "nested-direct name: mcp_console_available_root.nested_missing",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        assert output.count("requirements.python") >= 3, output
        assert len(uv_tool_run_requirements(record)) == baseline
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
def test_reports_unavailable_standard_library_module_without_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import sys

            assert "winreg" in sys.stdlib_module_names
            try:
                import winreg
            except ModuleNotFoundError as error:
                print(f"missing standard-library name: {error.name}")
                print(error)
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert "missing standard-library name: winreg" in output, output
        assert "selected Python build" in output, output
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_disables_automatic_resolution_for_user_selected_python(
    binary: Path,
    execution: Execution,
) -> Transcript:
    missing = "mcp_console_user_selected_missing"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        managed_python = resolve_managed_python(binary, execution, directory)
        environment, record = recording_uv_environment(directory)
        environment["RETICULATE_PYTHON"] = str(managed_python)
        environment["PYTHONNODEBUGRANGES"] = "1"
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        client.send(python=f"import {missing}")
        output = last_result_text(client)
        assert f"ModuleNotFoundError: No module named '{missing}'" in output, output
        assert "MCP Console" not in output, output
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_traceback_paths(output)
        )
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_runtime_toolchain_probe_does_not_install_a_distribution(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_uv_environment(Path(temporary))
        with McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"python": ["rply"]})
            assert last_result_text(client) == "[prepared]"
            baseline = len(uv_tool_run_requirements(record))
            client.send(
                python="import rply; rply.LexerGenerator().build(); print('CPython lexer ready')"
            )
            assert last_result_text(client) == "CPython lexer ready\n", (
                client.transcript[-1]
            )
            assert len(uv_tool_run_requirements(record)) == baseline
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]
            assert "rpython" not in declaration["requirements"]["python"], declaration
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
