"""Existing Python paths and bounded fallback capture through public launch/MCP."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import environment
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.python import virtualenv_python
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.r import r_test_environment
from support.normalization import code
from support.resolvers import expose_uv


def configure(root: Path, python: object, **settings: object) -> None:
    config = root / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(json.dumps({"python": python, **settings}))


def venv(root: Path) -> Path:
    selected = root / "ordinary venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", selected],
        check=True,
        capture_output=True,
    )
    return selected


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_existing_paths_and_fallback_capture(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(
        prefix="console-python-selection-", dir=Path.home()
    ) as temporary:
        root = Path(temporary).resolve()
        selected = venv(root)
        programs = root / "tools"
        programs.mkdir()
        env = dict(
            environment(programs),
            RETICULATE_PYTHON="invalid-inherited-python",
            VIRTUAL_ENV=str(selected),
            CONDA_PREFIX="unrelated",
        )
        variants = {
            "venv shorthand": "ordinary venv",
            "tilde venv": "~/" + str(selected.relative_to(Path.home().resolve())),
            "existing executable": {"existing": str(virtualenv_python(selected))},
            "first available": {
                "first_available": [
                    {"existing": "created-later"},
                    "active_venv",
                    {"managed": {}},
                ]
            },
            "unused broken candidate": {
                "first_available": [
                    {"existing": "ordinary venv"},
                    {"existing": "broken"},
                    {"managed": {}},
                ]
            },
        }
        (root / "broken").mkdir()
        audit = []
        for label, choice in variants.items():
            configure(root, choice, cache="host")
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                # fmt: python
                program = code("""
                    import sys
                    from pathlib import Path

                    assert Path(sys.prefix) == Path.cwd() / "ordinary venv"
                    assert sys.prefix != sys.base_prefix
                    print("venv retained")
                    """)
                client.expect("venv retained\n", python=program)
                if label == "first available":
                    (root / "created-later").mkdir()
                    client.expect(
                        "changed\n",
                        python="import os; os.environ['VIRTUAL_ENV'] = 'broken'; print('changed')",
                    )
                client.send(control="restart")
                client.expect("venv retained\n", python=program)
                client.finish()
                audit.append({"choice": label, "selected_venv": True, "restart": True})
        return audit


@requires(POSIX)
def test_broken_executable_does_not_select_later_candidate(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        programs = root / "tools"
        programs.mkdir()
        broken = root / "broken-python"
        broken.write_text("#!/bin/sh\necho 'fixture interpreter failed' >&2\nexit 23\n")
        broken.chmod(0o755)
        configure(
            root,
            {
                "first_available": [
                    {"existing": str(broken)},
                    {"existing": sys.executable},
                ]
            },
            cache="host",
        )
        with McpClient(binary, DIRECT.serve(), environment(programs), root) as client:
            client.initialize_and_list_tools()
            result = client.send(python="raise AssertionError('must not run')")
            assert (
                result.get("isError")
                and "selected Python inspection failed" in result["content"][0]["text"]
            ), result
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            return [{"result": result, "stderr": stderr}]


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_existing_venv_with_r_retains_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = venv(root)
        env, _ = r_test_environment()
        env["RETICULATE_PYTHON"] = "invalid-inherited-python"
        configure(root, {"existing": str(selected)}, cache="host")
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("R ready\n", r='cat("R ready\\n")')
            program = "import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / 'ordinary venv'; print('selected Python')"
            client.expect("selected Python\n", python=program)
            client.send(control="restart")
            client.expect("selected Python\n", python=program)
            client.expect("R ready\n", r='cat("R ready\\n")')
            client.finish()
            return [{"r_and_selected_venv": True, "restart": True}]


@requires(R)
@executions(DIRECT)
def test_managed_selection_requires_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        programs = root / "tools"
        programs.mkdir()
        library = root / "r-library"
        library.mkdir()
        env, _ = r_test_environment()
        # Expose Python on PATH without either preparation tool.
        env.update(
            PATH=os.pathsep.join(
                (str(programs), str(Path(sys.executable).parent), os.defpath)
            ),
            R_LIBS=str(library),
            R_LIBS_SITE=str(library),
            R_LIBS_USER=str(library),
        )
        env.pop("RETICULATE_UV", None)
        audit = []
        for choice in ({}, {"managed": {}}, {"first_available": [{"managed": {}}]}):
            configure(root, choice, cache="host")
            with McpClient(binary, execution.serve(), env, root) as client:
                client.initialize_and_list_tools()
                result = client.send(python="raise AssertionError('must not run')")
                assert result.get("isError"), result
                assert "dynamic environment resolution is unavailable" in str(result), (
                    result
                )
                client.finish_with_standard_error(expected_exit_status=1)
                audit.append({"choice": choice, "managed_preparation_required": True})
        return audit


def test_rejects_invalid_selection_chains(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        invalid = {
            "empty chain": {"first_available": []},
            "duplicate active venv": {
                "first_available": ["active_venv", "active_venv"]
            },
            "managed first": {
                "first_available": [{"managed": {}}, {"existing": "unused"}]
            },
            "nested chain": {
                "first_available": [{"first_available": [{"managed": {}}]}]
            },
            "multiple choices": {"existing": "unused", "managed": {}},
            "null managed options": {"managed": None},
            "empty path": "",
        }
        audit = []
        for label, choice in invalid.items():
            configure(root, choice)
            result = subprocess.run(
                [binary, "serve"], cwd=root, input="", capture_output=True, text=True
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "python" in result.stderr.lower(), result
            audit.append({"choice": label, "stderr": result.stderr})
        return audit


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_managed_fallback_overrides_legacy_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        programs = root / "tools"
        programs.mkdir()
        expose_uv(programs)
        env = dict(environment(programs), RETICULATE_PYTHON="invalid-inherited-python")
        env.pop("VIRTUAL_ENV", None)
        configure(
            root,
            {
                "first_available": [
                    {"existing": "absent"},
                    "active_venv",
                    {"managed": {}},
                ]
            },
            cache="host",
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "managed fallback\n",
                python="import sys; assert sys.prefix != sys.base_prefix; print('managed fallback')",
            )
            inspection = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]
            assert inspection["requirements"]["python"]
            client.finish()
            return [{"managed_fallback": True, "legacy_selection_overridden": True}]


@requires(POSIX, command("uv"))
@executions(SANDBOXED)
def test_reached_managed_candidate_keeps_console_user_site(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        programs = root / "tools"
        programs.mkdir()
        expose_uv(programs)
        env = dict(
            environment(programs),
            PYTHONUSERBASE=str(root / "host-user"),
            RETICULATE_PYTHON=sys.executable,
        )
        env.pop("VIRTUAL_ENV", None)
        configure(
            root,
            {
                "first_available": [
                    {"existing": "absent"},
                    "active_venv",
                    {"managed": {}},
                ]
            },
        )
        expected = (
            Path(
                env.get(
                    "XDG_CACHE_HOME",
                    str(
                        Path.home()
                        / ("Library/Caches" if sys.platform == "darwin" else ".cache")
                    ),
                )
            )
            / "mcp-console/dependencies/python/user"
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            # fmt: python
            program = code("""
                import os, site, sys
                from pathlib import Path

                expected = Path(EXPECTED_USER_BASE)
                assert Path(os.environ["PYTHONUSERBASE"]) == expected
                assert Path(site.getuserbase()) == expected
                assert not any(Path(path).is_relative_to(Path.cwd() / "host-user") for path in sys.path)
                print("console user site")
                """).replace("EXPECTED_USER_BASE", repr(str(expected)))
            client.expect("console user site\n", python=program)
            client.send(control="restart")
            client.expect("console user site\n", python=program)
            client.finish()
            return [{"reached_managed_console_user_site": True}]


@requires(POSIX)
def test_present_broken_candidates_do_not_fall_back(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = venv(root)
        programs = root / "tools"
        programs.mkdir()
        env = environment(programs)
        (root / "dangling").symlink_to(root / "absent")
        (root / "broken").mkdir()
        (root / "conda/conda-meta").mkdir(parents=True)
        conda_python = root / "conda/bin/python"
        conda_python.parent.mkdir()
        conda_python.write_text("unsupported interpreter")
        (root / "conda-alias").symlink_to(conda_python)
        selected_python = virtualenv_python(selected)
        selected_python.unlink()
        selected_python.symlink_to(conda_python)
        failures = {
            "dangling": "cannot use existing Python",
            "broken": "standard venv",
            "conda": "Conda environments are unsupported",
            "conda-alias": "Conda environments are unsupported",
            "ordinary venv": "Conda environments are unsupported",
        }
        audit = []
        for name, expected in failures.items():
            configure(
                root,
                {
                    "first_available": [
                        {"existing": name},
                        {"existing": str(selected)},
                        {"managed": {}},
                    ]
                },
            )
            result = subprocess.run(
                [binary, "serve"],
                cwd=root,
                env=env,
                input="",
                capture_output=True,
                text=True,
            )
            assert result.returncode != 0 and expected in result.stderr, result
            audit.append(
                {
                    "candidate": name,
                    "stderr": result.stderr.replace(str(root), "<fixture>"),
                }
            )
        return audit


@requires(POSIX)
@executions(SANDBOXED)
def test_existing_inspection_uses_worker_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        outer = Path(temporary).resolve()
        root = outer / "workspace"
        root.mkdir()
        selected = venv(root)
        programs = root / "tools"
        programs.mkdir()
        env = environment(programs)
        target = outer / "resolver-writable"
        target.mkdir()
        site = next(selected.glob("lib/python*/site-packages"))
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import errno
                from pathlib import Path

                try:
                    Path({str(target / "escaped")!r}).write_text("escaped")
                except OSError as error:
                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    Path({str(root / "denied")!r}).write_text("denied")
                """)
        )
        configure(
            root,
            str(virtualenv_python(selected)),
            cache="host",
            sandbox={"filesystem": {"read_write": ["."]}},
            resolver={
                "sandbox": {
                    "filesystem": {"read_only": ["/"], "read_write": [str(target)]}
                }
            },
        )
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="21 * 2")
            assert (root / "denied").read_text() == "denied"
            assert not (target / "escaped").exists()
            client.finish()
            return [{"inspection_hook_denied_resolver_write": True}]
