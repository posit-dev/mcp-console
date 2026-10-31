"""Public MCP coverage with no R executable visible to the local server."""

import json
import os
import re
import select
import time
import subprocess
import shutil
import sys
import tempfile
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    SQL,
    requires,
    windows_short_paths,
)
from support.assertions import (
    assert_exact_interleaving,
    assert_result_content,
    last_result_text,
    wait_for_evaluation_output,
)
from support.installation import installed_console
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.records import Transcript, TranscriptWithCompanions
from support.snapshots import execution_snapshots
from support.python import virtualenv_python, windows_short_path
from support.resolvers import expose_uv
from support.normalization import code, normalize_python_resolution_error
from support.native import LOADER_VARIABLE, build_interposer
from support.r import r_test_environment
from support.python import runtime_source_line, write_test_wheel


def environment(
    path: Path, *, inherited: dict[str, str] | None = None
) -> dict[str, str]:
    """Filter R selection after composing the fixture's inherited environment."""
    retain_system_bwrap(path)
    env = dict(os.environ if inherited is None else inherited, PATH=str(path))
    for name in (
        "R_HOME",
        "R_LIBS",
        "R_LIBS_USER",
        "RETICULATE_PYTHON",
        "RETICULATE_UV",
    ):
        env.pop(name, None)
    return env


def selected_environment(path: Path) -> dict[str, str]:
    selected = path / ("python.exe" if os.name == "nt" else "python3")
    if os.name == "nt" and not selected.exists():
        selected = Path(sys.executable)
    return dict(environment(path), RETICULATE_PYTHON=str(selected))


def preparation_directory():
    return tempfile.TemporaryDirectory(prefix="console-preparation-test-")


def grant_resolver_cache(workspace: Path, cache: Path) -> None:
    # uv config files select the cache; Console YAML supplies its permissions.
    cache.mkdir()
    config = workspace / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "cache": "host",
                "resolver": {
                    "environment": {
                        "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY": str(cache / "duckdb"),
                        "MPLCONFIGDIR": str(cache / "matplotlib"),
                    },
                    "sandbox": {
                        "filesystem": {"read_only": ["/"], "read_write": [str(cache)]}
                    },
                },
            }
        )
    )


@contextmanager
def unavailable_fixture_index():
    requests: list[str] = []

    class Index(BaseHTTPRequestHandler):
        def do_GET(self):
            if "/mcp-console-test-" in self.path:
                requests.append(self.path)
                self.send_error(503, "fixture packages must resolve locally")
            else:
                self.send_response(302)
                self.send_header("Location", "https://pypi.org" + self.path)
                self.end_headers()

        def log_message(self, *_):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Index) as index:
        thread = threading.Thread(target=index.serve_forever)
        thread.start()
        try:
            yield f"http://127.0.0.1:{index.server_port}/simple", requests
        finally:
            index.shutdown()
            thread.join(timeout=5)
            assert not thread.is_alive(), "fixture index did not stop"


def preparation_environment(root: Path, *, with_r: bool = False) -> dict[str, str]:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/sans_r_uv.sh"
    for name in ("uv", "invalid-python"):
        program = root / name
        program.write_text(fixture.read_text())
        program.chmod(0o755)
    shutil.copy2(shutil.which("uv"), root / "real-uv")
    os.mkfifo(root / "wait")
    # A resolver may succeed while its selected executable is not embeddable.
    description = {
        "executable": str(root / "invalid-python"),
        "libpython": str(root / "missing-libpython"),
        "metadata": {
            "base_executable": str(root / "invalid-python"),
            "pythonpath": "",
            "version": "3.12.7",
            "version_number": "3.12",
            "architecture": "64bit",
            "conda": False,
            "numpy": None,
        },
    }
    description.update(
        {
            name: str(root)
            for name in ("prefix", "exec_prefix", "base_prefix", "base_exec_prefix")
        }
    )
    (root / "invalid-inspection.json").write_text(json.dumps(description))
    env = dict(environment(root), UV_CACHE_DIR=str(root))
    if with_r:
        r_environment, _ = r_test_environment()
        env.update(
            {key: value for key, value in r_environment.items() if key.startswith("R_")}
        )
        env["PATH"] = os.pathsep.join((str(root), os.environ["PATH"]))
    return env


def preparation_records(
    records: Transcript, root: Path, *, include_initialization: bool = False
) -> Transcript:
    for record in records:
        for content in record.get("result", {}).get("content", []):
            if content.get("type") != "text":
                continue
            text = (
                content["text"]
                .replace(str(root.resolve()), "<preparation>")
                .replace(str(root), "<preparation>")
            )
            text = re.sub(
                r'("python": ")<preparation>/[^"\n]+(")',
                r"\1<running Python>\2",
                text,
            )
            text = re.sub(
                r"<preparation>/archive-v0/[^/\"\n]+/bin/activate_this.py",
                "<preparation>/archive-v0/<environment>/bin/activate_this.py",
                text,
            )
            text = re.sub(
                r"(?m)^old libpython: .+$",
                "old libpython: <running libpython>",
                text,
            )
            if (
                text.startswith(
                    (
                        "managed Python resolution failed:",
                        "[managed Python resolution failed:",
                    )
                )
                and '"python": "<running Python>"' not in text
            ):
                text = normalize_python_resolution_error(text)
            content["text"] = text
    return records if include_initialization else records[3:]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_trusts_host_resolver(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {"sandbox": {"filesystem": {"read_write": [str(workspace)]}}}
                if execution == SANDBOXED
                else {}
            )
        )
        uv = workspace / "uv"
        marker = root / "host-resolution"
        uv.write_text(f"""#!/bin/sh
test -z "$UV_OFFLINE" || exit 81
printf 'host resolver ran' > "{marker}"
exec "{shutil.which("uv")}" "$@"
""")
        uv.chmod(0o755)
        env = dict(
            environment(workspace),
            UV_TOOL_DIR=str(root),
            UV_OFFLINE="1",
            RETICULATE_UV="unused-selection",
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, workspace
        ) as client:
            client.initialize_and_list_tools()
            prepared = client.send(requirements={"action": "get"})
            assert not prepared.get("isError", False), prepared
            assert marker.read_text() == "host resolver ran"
            marker.unlink()
            client.expect(
                "42\n",
                requirements={"python": ["py-yaml12"]},
                python="import yaml12; 42",
            )
            assert marker.read_text() == "host resolver ran"
            return client.finish()[3:]


@requires(SQL)
@executions(RUNTIME)
def test_inspects_and_replaces_managed_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        expose_uv(root)
        env = dict(environment(root), MCP_CONSOLE_LANGUAGES="python")
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()

            def declaration(extensions: tuple[str, ...] = ()) -> list[str]:
                result = client.send(requirements={"action": "get"})
                assert not result.get("isError"), result
                snapshot = result["structuredContent"]
                assert snapshot["requirements"]["r"] == []
                assert snapshot["requirements"]["duckdb"] == list(extensions)
                assert snapshot["runtime_requirements"] == {"r": [], "python": []}
                return snapshot["requirements"]["python"]

            defaults = ["numpy", "pandas", "matplotlib", "plotnine", "duckdb"]
            assert declaration(("icu", "json", "sqlite")) == defaults
            client.send(requirements={"action": "set", "python": ["six"]})
            assert declaration() == ["six"]
            client.expect("42\n", python="import six; retained = 42; retained")
            client.send(requirements={"action": "reset"})
            assert client.transcript[-1]["result"]["isError"]
            client.expect("42\n", python="retained")
            client.send(
                control="restart",
                requirements={"action": "set"},
                # fmt: python
                python=code("""
                    import importlib.util

                    all(
                        importlib.util.find_spec(package) is None
                        for package in ("numpy", "pandas", "matplotlib", "plotnine")
                    )
                    """),
            )
            assert last_result_text(client).endswith("True\n[done]"), client.transcript[
                -1
            ]
            assert declaration() == []
            client.send(control="restart", requirements={"action": "reset"})
            assert declaration(("icu", "json", "sqlite")) == defaults
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import importlib.metadata

                    for package in ("numpy", "pandas", "matplotlib", "plotnine", "duckdb"):
                        assert importlib.metadata.version(package)
                    import numpy, pandas, matplotlib, plotnine

                    42
                    """),
            )
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_configured_python_expands_home(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        home = root / "home with spaces"
        workspace = root / "workspace"
        home.mkdir()
        workspace.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", home / ".venv"],
            check=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        bin_dir = root / "bin"
        bin_dir.mkdir()
        env = environment(bin_dir) | {"HOME": str(home)}
        records = []
        if os.name == "nt":
            env["USERPROFILE"] = str(home)
        selected = "~/" + virtualenv_python(home / ".venv").relative_to(home).as_posix()
        for source in ("project", "CLI"):
            config.write_text(
                f"python: {selected}\n" if source == "project" else "python: missing\n"
            )
            arguments = () if source == "project" else ("-c", f"python={selected}")
            with McpClient(
                binary, execution.serve(*arguments), env, workspace
            ) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "True\n",
                    # fmt: python
                    python=code("""
                        import os, sys
                        from pathlib import Path

                        print(Path(sys.prefix) == Path(os.environ["HOME"]) / ".venv")
                        """),
                )
                records.append({"configuration": source})
                records.extend(client.finish()[3:])
        return records


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_configured_python_bypasses_uv(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", workspace / ".venv"],
            check=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: .venv/bin/python\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        env = environment(bin_dir)
        # Explicit selection bypasses even unsupported resolver configuration.
        env["UV_ENV_FILE"] = str(workspace / "missing.env")
        env["RETICULATE_PYTHON"] = str(root / "missing-python")
        with McpClient(
            binary, execution.serve(), env, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import subprocess, sys
                    from pathlib import Path

                    assert Path(sys.prefix) == Path.cwd() / ".venv"
                    assert (
                        subprocess.check_output(
                            [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                        ).strip()
                        == sys.prefix
                    )
                    identity = object()
                    identity_id = id(identity)
                    42
                    """),
            )
            result = client.send(
                control="restart",
                requirements={"python": ["six"]},
                python="identity = None",
            )
            assert result["isError"]
            client.expect("42\n", python="assert id(identity) == identity_id; 42")
            config.write_text("python: missing-after-startup\n")
            client.send(
                control="restart",
                python="import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / '.venv'; 42",
            )
            assert last_result_text(client).endswith("42\n[done]")
            records = client.finish()
        # The CLI uses the same config layer and overrides the now-invalid file.
        with McpClient(
            binary, execution.serve("-c", "python=.venv/bin/python"), env, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "42\n",
                python="import sys; from pathlib import Path; assert Path(sys.prefix) == Path.cwd() / '.venv'; 42",
            )
            records.extend(client.finish())
        return records


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_captures_user_uv_configuration(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir=Path.home()) as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        cache = root / "cache"
        config = root / "uv.toml"
        config.write_text(
            f'cache-dir = "{cache}"\nindex-url = "https://invalid.example/simple"\n'
        )
        uv = root / "uv"
        uv.write_text(f"""#!/bin/sh
test "$UV_HTTP_TIMEOUT" = 37 || exit 81
exec "{shutil.which("uv")}" "$@"
""")
        uv.chmod(0o755)
        env = dict(
            environment(root),
            UV_CONFIG_FILE=str(config),
            UV_INDEX_URL="https://pypi.org/simple",
            UV_HTTP_TIMEOUT="37",
            MCP_CONSOLE_TEST_CACHE=str(cache),
        )
        # Exercise the file setting without an inherited environment override.
        env.pop("UV_CACHE_DIR", None)
        # Project discovery must not override user configuration.
        (workspace / "uv.toml").write_text(
            'index-url = "https://invalid.example/project"\n'
        )
        if execution is SANDBOXED:
            grant_resolver_cache(workspace, cache)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "user cache selected\n",
                # fmt: python
                python=code("""
                    import os, sys
                    from pathlib import Path

                    assert Path(sys.prefix).is_relative_to(Path(os.environ["MCP_CONSOLE_TEST_CACHE"]))
                    os.environ["UV_CONFIG_FILE"] = str(Path.cwd() / "uv.toml")
                    os.environ["UV_CACHE_DIR"] = str(Path.cwd() / "worker-cache")
                    os.environ["UV_HTTP_TIMEOUT"] = "1"
                    print("user cache selected")
                    """),
            )
            client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                python="import yaml12; 42",
            )
            assert last_result_text(client).endswith("42\n[done]"), client.transcript[
                -1
            ]
            assert not (workspace / "worker-cache").exists()
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_captures_relative_uv_paths(binary: Path, execution: Execution) -> Transcript:
    records = []
    installations = subprocess.check_output(["uv", "python", "dir"], text=True).strip()
    # Windows CI keeps the checkout and Python installations on different
    # drives. A relative installation path needs a workspace on its drive.
    temporary_parent = Path(installations).parent if os.name == "nt" else None
    if temporary_parent is not None:
        # `uv python dir` reports the path without creating its parents.
        temporary_parent.mkdir(parents=True, exist_ok=True)
    for cache_setting in ("environment", "configuration"):
        with tempfile.TemporaryDirectory(
            prefix="console-preparation-test-",
            dir=temporary_parent,
        ) as directory:
            root = Path(directory).resolve()
            workspace = root / "workspace"
            workspace.mkdir()
            expose_uv(root)
            config = root / "uv.toml"
            config.write_text('cache-dir = "../shared-uv"\n')
            env = dict(
                environment(root),
                UV_CONFIG_FILE="../uv.toml",
                UV_PYTHON_INSTALL_DIR=os.path.relpath(installations, workspace),
                MCP_CONSOLE_TEST_CACHE=str(root / "shared-uv"),
            )
            if cache_setting == "environment":
                config.write_text('cache-dir = "unused-config-cache"\n')
                env["UV_CACHE_DIR"] = "../shared-uv"
            else:
                env.pop("UV_CACHE_DIR", None)
                if execution is SANDBOXED:
                    grant_resolver_cache(workspace, root / "shared-uv")
            with McpClient(
                binary, execution.serve("-c", "cache=host"), env, workspace
            ) as client:
                client.initialize_and_list_tools()
                client.expect(
                    "relative startup paths retained\n",
                    # fmt: python
                    python=code("""
                        import os, sys
                        from pathlib import Path

                        assert Path(sys.prefix).is_relative_to(Path(os.environ["MCP_CONSOLE_TEST_CACHE"]))
                        print("relative startup paths retained")
                        """),
                )
                records.extend(client.finish()[3:])
    return records


@requires(POSIX)
@executions(DIRECT)
def test_ignores_unrelated_non_utf8_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return non_utf8_environment_preparation(binary, execution)


@executions(SANDBOXED)
def test_prepares_after_native_non_utf8_environment_rejection(
    binary: Path, execution: Execution
) -> Transcript:
    return non_utf8_environment_preparation(binary, execution)


def non_utf8_environment_preparation(binary: Path, execution: Execution) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        expose_uv(root)
        env = environment(root)
        env["UNRELATED_STARTUP_VALUE"] = os.fsdecode(b"non-utf8-\xff")
        env[os.fsdecode(b"UNRELATED_STARTUP_NAME_\xff")] = "unused"
        if execution is SANDBOXED:
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "resolver": {
                            "environment": {
                                "HOME": env["HOME"],
                                "PATH": env["PATH"],
                                "UV_CACHE_DIR": str(root / "uv-cache"),
                                "UV_NO_CONFIG": "1",
                            },
                            "inherit_environment": False,
                        }
                    }
                )
            )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, root
        ) as client:
            client.initialize_and_list_tools()
            # Host preparation accepts unrelated non-UTF-8 values. The explicit
            # resolver environment keeps native preparation usable while the
            # worker retains its UTF-8 environment requirement.
            startup = client.send(requirements={"action": "get"})
            assert startup["isError"] == (execution == SANDBOXED), startup
            if execution == SANDBOXED:
                assert last_result_text(client) == (
                    "mcp-console-sandbox: environment key must be UTF-8\n"
                    "[worker relay exited before readiness]"
                ), startup
            result = client.send(requirements={"python": ["py-yaml12"]})
            assert not result["isError"], result
            assert last_result_text(client) == "[prepared]"
            transcript, stderr = client.finish_with_standard_error()
            assert stderr == "", stderr
            return transcript[3:]


@executions(DIRECT, SANDBOXED)
def test_prepares_managed_python_at_startup_and_restart(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with preparation_directory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        env = environment(root)
        uv = expose_uv(root)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            "sandbox: {filesystem: {read_write: [.]}}\n"
            if execution == SANDBOXED
            else "{}\n"
        )
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    import yaml12

                    print("startup package available")
                    """),
            )
            assert not client.transcript[-1]["result"]["isError"], client.transcript[-1]
            schema = client.transcript[2]["result"]["tools"][0]["inputSchema"]
            assert "r" in schema["properties"]
            assert ("sql" in schema["properties"]) == SQL.available
            requirement_schema = schema["properties"]["requirements"]
            expected_fields = {
                "r",
                "python",
                "duckdb",
                "action",
                "python_version",
                "exclude_newer",
            }
            assert set(requirement_schema["properties"]) == expected_fields
            client.expect(
                "startup packages available\n",
                # fmt: python
                python=code("""
                    import os, subprocess, sys, numpy, pandas, yaml12

                    assert "RETICULATE_PYTHON" not in os.environ
                    subprocess.run([sys.executable, "-c", "import numpy, pandas, yaml12"], check=True)
                    identity = object()
                    identity_id = id(identity)
                    print("startup packages available")
                    """),
            )
            # A no-op must not invoke uv or replace the running interpreter.
            uv.unlink()
            client.expect("[prepared]", requirements={"python": ["py-yaml12", "numpy"]})
            client.expect(
                "same worker\n",
                # fmt: python
                python=code("""
                    assert id(identity) == identity_id
                    print("same worker")
                    """),
            )
            client.expect(
                "no-op cell\n",
                requirements={"python": ["py-yaml12"]},
                # fmt: python
                python=code("""
                    assert id(identity) == identity_id
                    print("no-op cell")
                    """),
            )
            client.send(
                # fmt: python
                python=code("""
                    input("old worker> ")
                    open("old-worker-consumed-input", "w").close()
                    """)
            )
            assert "[waiting for stdin]" in last_result_text(client)
            shutil.copy2(shutil.which("uv"), uv)
            # Queued stdin does not acknowledge consumption. Collect the
            # replacement across running/input-request cuts without replaying it.
            wait_for_evaluation_output(
                client,
                lambda output: "replacement input\n" in output,
                "replacement Python input",
                controlled_completion=True,
                completion_timeout_seconds=client.response_timeout,
                control="restart",
                requirements={"python": ["more-itertools"]},
                stdin="replacement input\n",
                timeout_ms=0,
                # fmt: python
                python=code("""
                    assert "identity" not in globals()
                    print(input())
                    """),
            )
            assert not (workspace / "old-worker-consumed-input").exists()
            # Plain restart and crash replacement retain the accepted result.
            uv.unlink()
            for control in ({}, {"control": "restart"}, {"crash": True}):
                if control.pop("crash", False):
                    client.send(
                        # fmt: python
                        python=code("""
                            import os

                            os._exit(47)
                            """)
                    )
                    assert "status 47" in last_result_text(client), client.transcript[
                        -1
                    ]
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os, subprocess, sys, numpy, pandas, yaml12, more_itertools

                        assert "RETICULATE_PYTHON" not in os.environ
                        probe = "import numpy, pandas, yaml12, more_itertools"
                        subprocess.run([sys.executable, "-c", probe], check=True)
                        subprocess.run(["python", "-c", probe], check=True)
                        print("cumulative packages retained")
                        """),
                )
                assert "cumulative packages retained\n" in last_result_text(client), (
                    client.transcript[-1]
                )
            client.expect(
                "[prepared]",
                requirements={"python": ["more-itertools", "py-yaml12"]},
            )
            client.send(
                control="restart",
                requirements={"python": ["more-itertools", "py-yaml12"]},
                # fmt: python
                python=code("""
                    import yaml12, more_itertools

                    print("retained restart")
                    """),
            )
            assert "retained restart\n" in last_result_text(client), client.transcript[
                -1
            ]
            # Restart without code prepares immediately and retains cumulative requirements.
            shutil.copy2(shutil.which("uv"), uv)
            client.send(control="restart", requirements={"python": ["six"]})
            assert not client.transcript[-1]["result"]["isError"]
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import six, yaml12, more_itertools

                    42
                    """),
            )
            records = client.finish()[3:]
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        capture = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        calls = {
            event["call_id"]: event["request"]["arguments"]
            for event in capture
            if event["event"] == "tool_call"
        }
        sources = {}
        for event in capture:
            if (
                event["event"] == "source_associated"
                and "python" in calls[event["call_id"]]
            ):
                sources.setdefault(event["session_id"], []).append(
                    calls[event["call_id"]]["python"]
                )
        documents = []
        for path in sorted((session / "sessions").glob("session-*.qmd")):
            quarto = path.read_text()
            session_id = int(path.stem.removeprefix("session-"))
            expected = sources.pop(session_id)
            for source in set(expected):
                assert quarto.count(source) == expected.count(source), (
                    path,
                    source,
                    quarto,
                )
            assert "execute:\n  eval: false" not in quarto
            assert "original-working-directory:" in quarto and "\nir:" in quarto
            assert (
                "ir:\n  isolated: true\n  packages: []\n  python-packages:\n" in quarto
            )
            started = next(
                event
                for event in capture
                if event["event"] == "worker_started"
                and event["session_id"] == session_id
            )
            for package in started["requirements"]["python"]:
                assert f"    - {package}\n" in quarto, (path, package, quarto)
            documents.append(
                {
                    "session": session_id,
                    "qmd": quarto.replace(
                        str(workspace.resolve()), "<workspace>"
                    ).replace(str(workspace), "<workspace>"),
                    "path": path.relative_to(session).as_posix(),
                }
            )
        assert not sources, sources
        assert len(documents) > 1, documents
    return TranscriptWithCompanions(records, {"documents.yaml": documents})


@requires(SQL)
@executions(RUNTIME)
def test_adds_python_packages_to_idle_managed_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        preparation_directory() as directory,
        tempfile.TemporaryDirectory() as workspace,
    ):
        root = Path(directory)
        expose_uv(root)
        with McpClient(
            installed_console(binary),
            execution.serve(),
            environment(root),
            Path(workspace),
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "running state created\n",
                # fmt: python
                python=code("""
                    import os, sqlite3, sys

                    identity = object()
                    identity_id = id(identity)
                    worker_pid = os.getpid()
                    managed = _console.sql_connection()
                    managed_id = id(managed)
                    managed.execute("create table retained as select 42 as value")
                    selected = sqlite3.connect(":memory:")
                    selected.execute("create table chosen (value integer)")
                    _console.sql_connection(selected)
                    print("running state created")
                    """),
            )
            client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
            # Queued stdin does not acknowledge consumption. Collect the cell
            # across running/input-request cuts without resubmitting its input.
            wait_for_evaluation_output(
                client,
                '[input requested: ""]\nlive packages and state retained\n',
                "live Python preparation and queued input",
                completion_timeout_seconds=client.response_timeout,
                requirements={"python": ["six"]},
                stdin="live input\n",
                timeout_ms=0,
                # fmt: python
                python=code("""
                    import os, six, sys, yaml12
                    import subprocess

                    assert os.getpid() == worker_pid
                    assert id(identity) == identity_id
                    assert id(managed) == managed_id
                    assert _console.sql_connection() is selected
                    assert managed.execute("select value from retained").fetchone() == (42,)
                    assert (
                        subprocess.check_output(
                            [sys.executable, "-c", "import six, yaml12; print('child ready')"],
                            text=True,
                        ).strip()
                        == "child ready"
                    )
                    assert input() == "live input"
                    print("live packages and state retained")
                    """),
            )
            client.send(sql="select value from chosen")
            assert "value" in last_result_text(client), client.transcript[-1]
            client.send(python="_console.sql_connection(None)")
            client.send(sql="select value from retained")
            assert "42" in last_result_text(client), client.transcript[-1]
            client.send(
                requirements={"python": ["more-itertools"]},
                python="raise ValueError('later cell failed')",
            )
            assert "ValueError: later cell failed" in last_result_text(client), (
                client.transcript[-1]
            )
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert {"py-yaml12", "six", "more-itertools"}.issubset(
                declaration["python"]
            )
            client.expect(
                "accepted after cell failure\n",
                # fmt: python
                python=code("""
                    import more_itertools

                    assert id(identity) == identity_id
                    print("accepted after cell failure")
                    """),
            )
            client.send(control="restart")
            client.expect(
                "plain restart retained additions\n",
                # fmt: python
                python=code("""
                    import os, subprocess, sys
                    import more_itertools, six, yaml12

                    assert "identity" not in globals()
                    assert (
                        subprocess.check_output(
                            ["python", "-c", "import more_itertools, six, yaml12; print('child ready')"],
                            text=True,
                        ).strip()
                        == "child ready"
                    )
                    print("plain restart retained additions")
                    """),
            )
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client), client.transcript[-1]
            client.expect(
                "crash replacement retained additions\n",
                python="import more_itertools, six, yaml12; print('crash replacement retained additions')",
            )
            records = client.finish()[3:]
        (session,) = (Path(workspace) / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        accepted = [
            event["packages"]
            for event in events
            if event["event"] == "python_environment_accepted"
        ]
        assert len(accepted) >= 3, accepted
        assert {"py-yaml12", "six", "more-itertools"}.issubset(accepted[-1])
        quarto = sorted((session / "sessions").glob("session-*.qmd"))[-1].read_text()
        assert "execute:\n  eval: false" not in quarto
        assert "original-working-directory:" in quarto and "\nir:" in quarto
        assert "ir:\n  isolated: true\n  packages: []\n  python-packages:\n" in quarto
        assert "    - more-itertools\n" in quarto
        return records


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_resolves_reached_import_in_managed_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        uv = expose_uv(root)
        with McpClient(
            installed_console(binary),
            execution.serve("-c", "python.managed.resolution=implicit"),
            environment(root),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "state created\n",
                python=code("""
                    import os, sqlite3

                    worker_pid = os.getpid()
                    identity = object()
                    identity_id = id(identity)
                    managed = _console.sql_connection()
                    managed.execute("create table retained as select 42 as value")
                    selected = sqlite3.connect(":memory:")
                    selected.execute("create table chosen as select 7 as value")
                    _console.sql_connection(selected)
                    steps = []
                    print("state created")
                    """),
            )
            client.send(
                python=code("""
                    steps.append("before import")
                    def load_package():
                        import yaml12
                        return yaml12
                    loaded = load_package()
                    assert os.getpid() == worker_pid
                    assert id(identity) == identity_id
                    assert _console.sql_connection() is selected
                    assert managed.execute("select value from retained").fetchone() == (42,)
                    assert steps == ["before import"]
                    print("reached import activated")
                    """)
            )
            assert "reached import activated\n" in last_result_text(client), (
                client.transcript[-1]
            )
            assert (
                "resolved PyPI distribution 'py-yaml12' for Python import 'yaml12'"
                in last_result_text(client)
            )
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert "py-yaml12" in declaration["python"]
            client.send(sql="select value from chosen")
            assert "7" in last_result_text(client), client.transcript[-1]
            client.send(
                python=code("""
                    steps.append("before second import")
                    import pydash
                    assert steps == ["before import", "before second import"]
                    raise ValueError("later statement failed")
                    """)
            )
            assert "ValueError: later statement failed" in last_result_text(client)
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert {"py-yaml12", "pydash"}.issubset(declaration["python"])
            uv.unlink()
            client.expect(
                "[prepared]", requirements={"python": ["py-yaml12", "pydash"]}
            )
            client.expect(
                "automatic additions survived later failure\n",
                python=code("""
                    import subprocess, sys

                    assert os.getpid() == worker_pid and id(identity) == identity_id
                    assert _console.sql_connection() is selected
                    assert managed.execute("select value from retained").fetchone() == (42,)
                    assert subprocess.check_output(
                        [sys.executable, "-c", "import yaml12, pydash; print('child ready')"],
                        text=True,
                    ).strip() == "child ready"
                    print("automatic additions survived later failure")
                    """),
            )
            client.send(control="restart")
            client.expect(
                "restart retained imports\n",
                python="import yaml12, pydash; assert 'steps' not in globals(); print('restart retained imports')",
            )
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.expect(
                "replacement retained imports\n",
                python="import yaml12, pydash; print('replacement retained imports')",
            )
            records = client.finish()[3:]
        (session,) = (root / ".agents/console/sessions").iterdir()
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        assert any(
            event["event"] == "python_environment_accepted"
            and {"py-yaml12", "pydash"}.issubset(event["packages"])
            for event in events
        )
        quarto = sorted((session / "sessions").glob("session-*.qmd"))[-1].read_text()
        assert "execute:\n  eval: false" not in quarto
        assert "original-working-directory:" in quarto and "\nir:" in quarto
        assert "ir:\n  isolated: true\n  packages: []\n  python-packages:\n" in quarto
        assert "    - py-yaml12\n" in quarto and "    - pydash\n" in quarto
        return records


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_combines_live_python_and_duckdb_additions(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        expose_uv(root)
        with McpClient(
            installed_console(binary), execution.serve(), environment(root)
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "state created\n",
                python=code("""
                    import os, sqlite3

                    worker_pid = os.getpid()
                    identity = object()
                    identity_id = id(identity)
                    managed = _console.sql_connection()
                    managed.execute("create table retained as select 42 as value")
                    selected = sqlite3.connect(":memory:")
                    selected.execute("create table chosen as select 7 as value")
                    _console.sql_connection(selected)
                    print("state created")
                    """),
            )
            client.send(
                requirements={"python": ["py-yaml12"], "duckdb": ["parquet"]},
                sql="select value from chosen",
            )
            assert "7" in last_result_text(client), client.transcript[-1]
            client.expect(
                "combined additions retained state\n",
                python=code("""
                    import os, subprocess, sys, yaml12

                    assert os.getpid() == worker_pid
                    assert id(identity) == identity_id
                    assert _console.sql_connection() is selected
                    assert managed.execute("select value from retained").fetchone() == (42,)
                    assert subprocess.check_output(
                        [sys.executable, "-c", "import yaml12; print('child ready')"],
                        text=True,
                    ).strip() == "child ready"
                    print("combined additions retained state")
                    """),
            )
            client.send(python="_console.sql_connection(None)")
            client.send(sql="select value from retained")
            assert "42" in last_result_text(client), client.transcript[-1]
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert "py-yaml12" in declaration["python"]
            assert declaration["duckdb"] == ["icu", "json", "parquet", "sqlite"]
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_retains_automatic_additions_after_import_errors(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        preparation_directory() as directory,
        unavailable_fixture_index() as (index, requests),
    ):
        root = Path(directory)
        expose_uv(root)
        wheel_index = write_test_wheel(root, "mcp_console_test_empty_pkg", None)
        write_test_wheel(
            root,
            "mcp_console_test_raises_pkg",
            'raise RuntimeError("synthetic module initialization failure")\n',
        )
        env = dict(
            environment(root),
            # find-links still queries the default registry. A first-priority
            # index makes each generated distribution local to this test.
            UV_INDEX=wheel_index.as_uri(),
            UV_INDEX_STRATEGY="first-index",
            UV_DEFAULT_INDEX=index,
            UV_HTTP_RETRIES="0",
        )
        for name in ("UV_FIND_LINKS", "UV_INDEX_URL", "UV_EXTRA_INDEX_URL"):
            env.pop(name, None)
        # The index is a host loopback fixture, so preparation must share its
        # network namespace. Worker network policy remains independently selected.
        with McpClient(
            installed_console(binary),
            execution.serve(
                "-c",
                "python.managed.resolution=implicit",
                *(
                    ("-c", "resolver.sandbox.network=enabled")
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            env,
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import os; worker_pid = os.getpid(); steps = []; identity = object()"
            )
            client.send(
                python="steps.append('empty'); import mcp_console_test_empty_pkg"
            )
            output = last_result_text(client)
            assert "did not provide the import" in output, output
            client.send(
                python="steps.append('raises'); import mcp_console_test_raises_pkg"
            )
            output = last_result_text(client)
            assert "RuntimeError: synthetic module initialization failure" in output, (
                output
            )
            normalized, count = re.subn(
                r'File "[^"\n]*[/\\]archive-v0[/\\][^/\\]+[/\\](?:lib[/\\]python\d+\.\d+|Lib)[/\\]site-packages[/\\](mcp_console_test_raises_pkg)[/\\](__init__\.py)"',
                r'File "<managed Python>/\1/\2"',
                output,
            )
            assert count == 1, output
            client.transcript[-1]["result"]["content"][0]["text"] = normalized
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert {
                "mcp_console_test_empty_pkg",
                "mcp_console_test_raises_pkg",
            }.issubset(declaration["python"])
            # Inspect the complete public response before recording the Python fields
            # owned by this case; provider inventories have separate coverage.
            assert (
                json.loads(last_result_text(client))
                == client.transcript[-1]["result"]["structuredContent"]
            )
            client.transcript[-1]["result"] = {
                "accepted_python_requirements": declaration["python"]
            }
            client.expect(
                "failed imports did not replay cells\n",
                python="assert steps == ['empty', 'raises']; assert os.getpid() == worker_pid; print('failed imports did not replay cells')",
            )
            assert not requests, requests
            return client.finish()[3:]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_automatic_resolution_failure_and_cancel_keep_accepted_state(
    binary: Path, execution: Execution
) -> Transcript:
    return automatic_resolution_failure_and_cancel_keep_accepted_state(
        binary, execution, with_r=False
    )


def automatic_resolution_failure_and_cancel_keep_accepted_state(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root, with_r=with_r)
        started = FifoCheckpoint.create(root / "started")
        os.mkfifo(root / "alive")
        alive = os.open(root / "alive", os.O_RDONLY | os.O_NONBLOCK)
        try:
            with McpClient(
                installed_console(binary),
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                env,
            ) as client:
                client.initialize_and_list_tools()
                client.expect(
                    python="import os; worker_pid = os.getpid(); steps = []; identity = object()"
                )
                initial = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                (root / "mode").write_text("failure")
                failure = wait_for_evaluation_output(
                    client,
                    None,
                    "automatic Python resolution failure",
                    completion_timeout_seconds=client.response_timeout,
                    python="steps.append('failure'); import yaml12",
                )
                assert "fixture Python resolution failed" in failure, failure
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                (root / "mode").write_text("interrupt")
                pending = client.start_send(
                    python="steps.append('cancel'); import yaml12",
                    timeout_ms=int(client.response_timeout * 1_000),
                )
                started.wait(
                    "automatic Python resolver entered", timeout=client.response_timeout
                )
                assert os.read(alive, 1) == b"1"
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                interrupt = client.start_send(control="interrupt")
                client.receive_many([pending, interrupt])
                assert os.read(alive, 1) == b""
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                client.expect(
                    "accepted state retained\n",
                    python="assert steps == ['failure', 'cancel']; assert os.getpid() == worker_pid; print('accepted state retained')",
                )
                (root / "mode").write_text("success")
                client.expect(
                    "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
                    "retry resolved import\n",
                    python="import yaml12; print('retry resolved import')",
                )
                return preparation_records(
                    client.finish(), root, include_initialization=with_r
                )
        finally:
            started.close()
            os.close(alive)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_automatic_activation_failure_requires_restart(
    binary: Path, execution: Execution
) -> Transcript:
    return automatic_activation_failure_requires_restart(
        binary, execution, with_r=False
    )


def automatic_activation_failure_requires_restart(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root, with_r=with_r)
        (root / "mode").write_text("activation-failure")
        with McpClient(
            installed_console(binary),
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            env,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="identity = object(); steps = []")
            initial = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            output = wait_for_evaluation_output(
                client,
                None,
                "automatic activation failure",
                completion_timeout_seconds=client.response_timeout,
                python="steps.append('before'); import yaml12",
            )
            assert output.count("RuntimeError: synthetic activation failure") == 1, (
                output
            )
            assert "restart required" in output, output
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == initial
            )
            client.expect("[restart required]", requirements={"python": ["six"]})
            client.expect(
                "worker still running\n",
                python="assert steps == ['before']; print('worker still running')",
            )
            client.send(control="restart")
            client.expect(
                "restart retained accepted environment\n",
                python="assert 'identity' not in globals(); print('restart retained accepted environment')",
            )
            return preparation_records(
                client.finish(), root, include_initialization=with_r
            )


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_automatic_imports_stay_on_main_worker_thread_and_process(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root)
        (root / "mode").write_text("success")
        with McpClient(
            installed_console(binary),
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            env,
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="import os; worker_pid = os.getpid()")
            before = (root / "resolutions.log").read_text()
            client.expect(
                "background import rejected\n",
                python=code("""
                    import threading

                    thread_result = []
                    def import_from_thread():
                        try:
                            import mcp_console_thread_missing
                        except ModuleNotFoundError as error:
                            thread_result.append((error.name, str(error)))
                    thread = threading.Thread(target=import_from_thread)
                    thread.start()
                    thread.join()
                    assert thread_result[0][0] == "mcp_console_thread_missing"
                    assert "configuring thread" in thread_result[0][1]
                    print("background import rejected")
                    """),
            )
            client.expect(
                "child import rejected\n",
                python=code("""
                    import select, signal, warnings

                    read_descriptor, write_descriptor = os.pipe()
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore", DeprecationWarning)
                        child = os.fork()
                    if child == 0:
                        os.close(read_descriptor)
                        try:
                            import mcp_console_child_missing
                        except ModuleNotFoundError as error:
                            payload = f"{error.name}: {error}"
                        else:
                            payload = "missing import unexpectedly succeeded"
                        os.write(write_descriptor, payload.encode())
                        os._exit(0)
                    os.close(write_descriptor)
                    readable, _, _ = select.select([read_descriptor], [], [], 10)
                    if not readable:
                        os.kill(child, signal.SIGKILL)
                        os.waitpid(child, 0)
                        raise AssertionError("fork child did not finish its import")
                    payload = os.read(read_descriptor, 65536).decode()
                    os.close(read_descriptor)
                    _, status = os.waitpid(child, 0)
                    assert os.waitstatus_to_exitcode(status) == 0
                    assert "mcp_console_child_missing" in payload
                    assert "main worker process" in payload
                    assert os.getpid() == worker_pid
                    print("child import rejected")
                    """),
            )
            client.send(
                python=code("""
                    import sqlite3

                    sql_missing = []
                    def missing_from_sql():
                        try:
                            import mcp_console_sql_missing
                        except ModuleNotFoundError as error:
                            sql_missing.append(str(error))
                            return 42
                    selected = sqlite3.connect(":memory:")
                    selected.create_function("missing_from_sql", 0, missing_from_sql)
                    _console.sql_connection(selected)
                    """)
            )
            client.send(sql="select missing_from_sql() as value")
            assert "42" in last_result_text(client), client.transcript[-1]
            client.expect(
                "SQL did not resolve imports\n",
                python="assert sql_missing == [\"No module named 'mcp_console_sql_missing'\"]; print('SQL did not resolve imports')",
            )
            assert (root / "resolutions.log").read_text() == before
            return preparation_records(client.finish(), root)


@requires(POSIX, SQL)
@executions(RUNTIME)
def test_limits_live_python_additions_to_new_idle_distributions(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root)
        (root / "mode").write_text("success")
        with McpClient(
            installed_console(binary), execution.serve("-c", "cache=host"), env
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                python="import os; worker_pid = os.getpid(); identity = object()",
            )
            before = (root / "resolutions.log").read_text()
            client.send(python="input('busy> ')")
            assert "waiting for stdin" in last_result_text(client)
            busy = client.send(requirements={"python": ["py-yaml12"]})
            assert busy["isError"], busy
            assert "already evaluating" in last_result_text(client), busy
            client.expect("[prepared]", requirements={"python": ["numpy"]})
            # Enqueuing stdin can return before the cell resumes or completes.
            client.expect("'ready'\n", stdin="ready\n")
            client.send(python="import pdb; pdb.set_trace(); print('debugger resumed')")
            assert "(Pdb)" in last_result_text(client)
            busy = client.send(requirements={"python": ["py-yaml12"]})
            assert busy["isError"], busy
            assert "already evaluating" in last_result_text(client), busy
            client.expect("debugger resumed\n", stdin="continue\n")
            for requirements in (
                {"python": ["NumPy==0"]},
                {"python_version": ["<3"]},
                {"exclude_newer": "2026-01-01"},
                {"action": "set", "python": ["six"]},
            ):
                rejected = client.send(requirements=requirements)
                assert rejected["isError"], rejected
            assert (root / "resolutions.log").read_text() == before
            client.expect("[prepared]", requirements={"python": ["six"]})
            for requirements in (
                {"action": "reset"},
                {"action": "set", "python": ["six"]},
            ):
                rejected = client.send(requirements=requirements)
                assert rejected["isError"], rejected
            client.expect(
                "[prepared]",
                requirements={"python": ["six"], "duckdb": ["json"]},
            )
            client.send(
                requirements={"python": ["py-yaml12"], "duckdb": ["json"]},
                sql="select 42 as value",
            )
            assert "42" in last_result_text(client), client.transcript[-1]
            before = (root / "resolutions.log").read_text()
            rejected = client.send(
                requirements={"python": ["NumPy==0"], "duckdb": ["fts"]}
            )
            assert rejected["isError"], rejected
            assert (root / "resolutions.log").read_text() == before
            client.expect(
                "state retained\n",
                python="import os, six, yaml12; assert os.getpid() == worker_pid; print('state retained')",
            )
            declaration = client.send(requirements={"action": "get"})[
                "structuredContent"
            ]["requirements"]
            assert declaration["duckdb"] == ["icu", "json", "sqlite"]
            assert "NumPy==0" not in declaration["python"]
            return preparation_records(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_live_python_failure_and_interrupt_preserve_accepted_state(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root)
        started = FifoCheckpoint.create(root / "started")
        os.mkfifo(root / "alive")
        alive = os.open(root / "alive", os.O_RDONLY | os.O_NONBLOCK)
        try:
            with McpClient(
                installed_console(binary), execution.serve("-c", "cache=host"), env
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    python="import os; worker_pid = os.getpid(); identity = object()"
                )
                initial = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]["requirements"]
                (root / "mode").write_text("failure")
                failed = client.send(
                    requirements={"python": ["py-yaml12"]},
                    python="raise AssertionError('failed preparation ran code')",
                    stdin="must not be queued\n",
                )
                assert failed["isError"], failed
                assert "fixture Python resolution failed" in last_result_text(client)
                assert "failed preparation ran code" not in last_result_text(client)
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                (root / "mode").write_text("interrupt")
                pending = client.start_send(
                    requirements={"python": ["py-yaml12"]},
                    python="raise AssertionError('cancelled preparation ran code')",
                )
                started.wait("live candidate resolver entered")
                assert os.read(alive, 1) == b"1"
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                interrupt = client.start_send(control="interrupt")
                client.receive_many([pending, interrupt])
                assert pending["result"]["isError"], pending
                assert os.read(alive, 1) == b""
                assert (
                    client.send(requirements={"action": "get"})["structuredContent"][
                        "requirements"
                    ]
                    == initial
                )
                client.expect(
                    "still running\n",
                    python="import os; assert os.getpid() == worker_pid; print('still running')",
                )
                (root / "mode").write_text("success")
                client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
                return preparation_records(client.finish(), root)
        finally:
            started.close()
            os.close(alive)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_live_python_rejects_incompatible_library_before_activation(
    binary: Path, execution: Execution
) -> Transcript:
    return live_python_rejects_incompatible_library_before_activation(
        binary, execution, with_r=False
    )


def live_python_rejects_incompatible_library_before_activation(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root, with_r=with_r)
        with McpClient(
            installed_console(binary), execution.serve("-c", "cache=host"), env
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os, sysconfig

                    worker_pid = os.getpid()
                    identity = object()
                    identity_id = id(identity)
                    print(
                        os.path.join(
                            sysconfig.get_config_var("LIBDIR"), sysconfig.get_config_var("LDLIBRARY")
                        )
                    )
                    """)
            )
            library = Path(last_result_text(client).strip())
            assert library.is_file(), library
            alias = root / "alias-libpython"
            alias.symlink_to(library)
            description_file = root / "invalid-inspection.json"
            description = json.loads(description_file.read_text())
            description["libpython"] = str(alias)
            description_file.write_text(json.dumps(description))
            initial = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            (root / "mode").write_text("inspection")
            rejected = client.send(requirements={"python": ["py-yaml12"]})
            assert rejected["isError"], rejected
            assert "does not use the same Python binary" in last_result_text(client)
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == initial
            )
            client.expect(
                "compatible worker retained\n",
                python="import os; assert os.getpid() == worker_pid and id(identity) == identity_id; print('compatible worker retained')",
            )
            # The first cell exposes a host library path only to the test.
            return preparation_records(client.finish(), root)[1:]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_live_python_activation_failure_requires_restart(
    binary: Path, execution: Execution
) -> Transcript:
    return live_python_activation_failure_requires_restart(
        binary, execution, with_r=False
    )


def live_python_activation_failure_requires_restart(
    binary: Path, execution: Execution, *, with_r: bool
) -> Transcript:
    with preparation_directory() as directory:
        root = Path(directory)
        env = preparation_environment(root, with_r=with_r)
        (root / "mode").write_text("activation-failure")
        with McpClient(
            installed_console(binary), execution.serve("-c", "cache=host"), env
        ) as client:
            client.initialize_and_list_tools()
            # Separate fd 2 from the sideband deterministically. Activation
            # diagnostics must arrive before their preparation result even
            # when the relay cannot observe the raw stderr stream.
            client.expect(
                # fmt: python
                python=code("""
                    import os, tempfile

                    identity = object()
                    original_stderr = os.dup(2)
                    raw_stderr = tempfile.TemporaryFile()
                    _ = os.dup2(raw_stderr.fileno(), 2)
                    """),
                timeout_ms=0,
            )
            initial = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            failed = client.send(
                requirements={"python": ["py-yaml12"]},
                python="raise AssertionError('activation failure ran code')",
            )
            assert failed["isError"], failed
            diagnostic = last_result_text(client)
            assert (
                diagnostic.count("RuntimeError: synthetic activation failure") == 1
            ), diagnostic
            assert "activation failure ran code" not in diagnostic
            assert diagnostic.endswith(
                "further requirement changes are unavailable until session restart"
            ), diagnostic
            client.expect(  # fmt: python
                python=code("""
                    os.dup2(original_stderr, 2)
                    os.close(original_stderr)
                    raw_stderr.seek(0)
                    assert raw_stderr.read() == b""
                    raw_stderr.close()
                    """),
            )
            assert (
                client.send(requirements={"action": "get"})["structuredContent"][
                    "requirements"
                ]
                == initial
            )
            client.expect("[restart required]", requirements={"python": ["six"]})
            client.send(control="restart")
            client.expect(
                "accepted environment retained\n",
                python="assert 'identity' not in globals(); print('accepted environment retained')",
            )
            return preparation_records(
                client.finish(), root, include_initialization=with_r
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_failed_managed_preparation_preserves_worker_and_input(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as directory,
        preparation_directory() as fixture_directory,
    ):
        root = Path(fixture_directory)
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        env = preparation_environment(root)
        started = FifoCheckpoint.create(root / "started")
        os.mkfifo(root / "alive")
        alive = os.open(root / "alive", os.O_RDONLY | os.O_NONBLOCK)
        try:
            with McpClient(
                binary, execution.serve("-c", "cache=host"), env, workspace
            ) as client:
                client.initialize_and_list_tools()
                client.send(
                    # fmt: python
                    python=code("""
                        identity = object()
                        identity_id = id(identity)
                        42
                        """),
                    stdin="retained input\n",
                )
                assert last_result_text(client) == "42\n"
                for mode, expected in (
                    ("failure", "fixture Python resolution failed"),
                    ("inspection", "selected Python embedding library is missing"),
                    ("interrupt", "Python resolution interrupted"),
                    (
                        "inspection-interrupt",
                        "selected Python inspection failed (exit status: 1): fixture Python inspection interrupted",
                    ),
                ):
                    (root / "mode").write_text(mode)
                    arguments = dict(
                        control="restart",
                        requirements={"python": ["py-yaml12"]},
                        stdin="must not reach old worker\n",
                        # fmt: python
                        python=code("""
                            raise AssertionError("failed restart ran code")
                            """),
                    )
                    if mode in ("interrupt", "inspection-interrupt"):
                        pending = client.start_send(**arguments)
                        started.wait("candidate resolver entered")
                        assert os.read(alive, 1) == b"1"
                        interrupt = client.start_send(control="interrupt")
                        client.receive_many([pending, interrupt])
                        response = pending["result"]
                        assert os.read(alive, 1) == b"", (
                            "resolver survived interruption"
                        )
                    else:
                        response = client.send(**arguments)
                    assert response["isError"], response
                    text = "".join(item.get("text", "") for item in response["content"])
                    assert expected in text, response
                    client.send(
                        # fmt: python
                        python=code("""
                            assert id(identity) == identity_id
                            print("objects intact")
                            """)
                    )
                    assert last_result_text(client) == "objects intact\n"
                before = (root / "resolutions.log").read_text()
                for request in (
                    {"requirements": {"action": "set", "python": ["py-yaml12"]}},
                    {
                        "requirements": {"action": "set", "python": ["py-yaml12"]},
                        # fmt: python
                        "python": code("""
                            identity = None
                            """),
                        "stdin": "rejected live input\n",
                    },
                    {
                        "control": "restart",
                        "requirements": {"python": ["./local-package"]},
                        "stdin": "invalid input\n",
                    },
                    {"requirements": {"r": ["cli"]}},
                    {
                        "requirements": {
                            "duckdb": ["json"],
                            "python": ["numpy==0.1"],
                        }
                    },
                ):
                    response = client.send(**request)
                    assert response.get("isError", True), response
                assert (root / "resolutions.log").read_text() == before
                client.send(
                    # fmt: python
                    python=code("""
                        assert id(identity) == identity_id
                        print(input())
                        """)
                )
                assert (
                    last_result_text(client)
                    == '[input requested: ""]\nretained input\n'
                ), client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        print(input("remaining> "))
                        """)
                )
                assert "[waiting for stdin]" in last_result_text(client)
                client.send(
                    control="interrupt",
                    requirements={"python": ["numpy"]},
                    # fmt: python
                    python=code("""
                        identity = None
                        """),
                    stdin="rejected interrupt input\n",
                )
                assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
                client.send(session="default")
                assert "[waiting for stdin]" in last_result_text(client), (
                    client.transcript[-1]
                )
                wait_for_evaluation_output(
                    client,
                    "fresh input\n",
                    "queue remained empty",
                    stdin="fresh input\n",
                )
                # Failed candidates were never retained: the addition still needs resolution.
                (root / "mode").write_text("success")
                client.send(
                    control="restart",
                    requirements={"python": ["py-yaml12"]},
                    # fmt: python
                    python=code("""
                        import yaml12

                        print("accepted")
                        """),
                )
                assert "accepted\n" in last_result_text(client), client.transcript[-1]
                assert (root / "resolutions.log").read_text() != before
                records = preparation_records(client.finish(), root)
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            events = [
                json.loads(line)
                for line in (session / "capture/events.jsonl").read_text().splitlines()
            ]
            accepted = [
                event
                for event in events
                if event["event"] == "python_environment_accepted"
            ]
            assert len(accepted) == 1, accepted
            assert set(accepted[0]["packages"]) == {
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
                "py-yaml12",
            }
            return records
        finally:
            started.close()
            os.close(alive)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preparation_pins_result_files(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as workspace,
        preparation_directory() as directory,
    ):
        root = Path(directory)
        env = preparation_environment(root)
        unrelated = root / "unrelated"
        unrelated.write_text("unrelated host contents")
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, Path(workspace)
        ) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 42")
            for mode in ("replace-output", "replace-inspection"):
                (root / "mode").write_text(mode)
                client.send(control="restart", requirements={"python": ["py-yaml12"]})
                diagnostic = last_result_text(client)
                assert "selected Python embedding library is missing" in diagnostic, (
                    diagnostic
                )
                assert "unrelated host contents" not in diagnostic
                assert unrelated.read_text() == "unrelated host contents"
                client.expect("42\n", python="retained")
            return preparation_records(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retries_failed_prestart_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as directory,
        preparation_directory() as fixture_directory,
    ):
        root = Path(fixture_directory)
        workspace = Path(directory) / "workspace"
        workspace.mkdir()
        env = preparation_environment(root)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), env, workspace
        ) as client:
            client.initialize_and_list_tools()
            for mode in ("failure", "inspection"):
                (root / "mode").write_text(mode)
                client.send(
                    requirements={"python": ["py-yaml12"]},
                    # fmt: python
                    python=code("""
                        raise AssertionError("failed preparation ran code")
                        """),
                    stdin="rejected input\n",
                )
                assert client.transcript[-1]["result"]["isError"], client.transcript[-1]
            (root / "mode").write_text("success")
            client.expect("[prepared]", requirements={"python": ["py-yaml12"]})
            client.send(
                # fmt: python
                python=code("""
                    import yaml12

                    print(input("prepared> "))
                    """)
            )
            assert "[waiting for stdin]" in last_result_text(client), client.transcript[
                -1
            ]
            wait_for_evaluation_output(
                client,
                "fresh input\n",
                "failed prestart did not queue input",
                stdin="fresh input\n",
            )
            return preparation_records(client.finish(), root)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_shutdown_cancels_sans_r_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for restart, mode in (
        (False, "interrupt"),
        (True, "interrupt"),
        (False, "inspection-interrupt"),
        (True, "inspection-interrupt"),
    ):
        with (
            tempfile.TemporaryDirectory() as directory,
            preparation_directory() as fixture_directory,
        ):
            root = Path(fixture_directory)
            workspace = Path(directory) / "workspace"
            workspace.mkdir()
            env = preparation_environment(root)
            (root / "mode").write_text(mode)
            started = FifoCheckpoint.create(root / "started")
            os.mkfifo(root / "alive")
            alive = os.open(root / "alive", os.O_RDONLY | os.O_NONBLOCK)
            try:
                with McpClient(
                    binary, execution.serve("-c", "cache=host"), env, workspace
                ) as client:
                    client.initialize_and_list_tools()
                    # MCP readiness precedes discovery. Wait for its retained
                    # result before measuring the operation's resolver checkpoint.
                    client.send(requirements={"action": "get"})
                    assert not client.transcript[-1]["result"].get("isError", False), (
                        client.transcript[-1]
                    )
                    if restart:
                        client.expect(
                            "42\n",
                            # fmt: python
                            python=code("""
                                retained = 42
                                retained
                                """),
                        )
                    client.start_send(
                        requirements={"python": ["py-yaml12"]},
                        # fmt: python
                        python=code("""
                            from pathlib import Path

                            Path("cancelled-preparation-cell-ran").touch()
                            raise AssertionError("cancelled preparation ran code")
                            """),
                        **({"control": "restart"} if restart else {}),
                    )
                    started.wait("resolver entered before input closure")
                    assert os.read(alive, 1) == b"1"
                    deadline = time.monotonic() + 10
                    client.stdin.close()
                    assert client.process.wait(timeout=deadline - time.monotonic()) == 0
                    # Host retirement reaps the leader after signaling its group.
                    # Observe descendant descriptor closure, not scheduler order.
                    readable, _, _ = select.select(
                        [alive], [], [], max(0, deadline - time.monotonic())
                    )
                    assert readable and os.read(alive, 1) == b"", (
                        "resolver survived shutdown"
                    )
                    assert not (workspace / "cancelled-preparation-cell-ran").exists()
                    # Closing MCP input forfeits response delivery. Drain EOF;
                    # the cancellation/retirement race has no fixed diagnostic.
                    client.stdout.read()
                    records.append(
                        {
                            "restart": restart,
                            "phase": mode,
                            "cell_was_not_run": True,
                            "stderr": client.stderr.read(),
                        }
                    )
                (session,) = (workspace / ".agents/console/sessions").iterdir()
                events = [
                    json.loads(line)
                    for line in (session / "capture/events.jsonl")
                    .read_text()
                    .splitlines()
                ]
                assert all(
                    event["event"] != "python_environment_accepted" for event in events
                )
            finally:
                started.close()
                os.close(alive)
    return records


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_resolves_default_python_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        path = Path(directory)
        uv = shutil.which("uv")
        assert uv is not None
        shutil.copy2(uv, path / "uv")
        env = environment(path)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            schema = client.transcript[-1]["result"]["tools"][0]
            assert "r" in schema["inputSchema"]["properties"]
            assert "sql" in schema["inputSchema"]["properties"]
            assert (
                "duckdb"
                in schema["inputSchema"]["properties"]["requirements"]["properties"]
            )
            python_description = schema["inputSchema"]["properties"]["python"][
                "description"
            ]
            assert "on Python-owned DuckDB" in python_description
            assert (
                "_console.sql_connection().register(name, frame)" in python_description
            )
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import json
                    import os
                    import subprocess
                    import sys
                    import numpy
                    import pandas

                    assert "RETICULATE_PYTHON" not in os.environ
                    assert sys.prefix != sys.base_prefix
                    base = os.path.join(sys.base_prefix, "bin", "python3")
                    probe = "import importlib.util; assert importlib.util.find_spec('pandas') is None"
                    subprocess.run([base, "-I", "-c", probe], check=True)
                    probe = "import json, sys, pandas; print(json.dumps([sys.executable, sys.prefix, sys.exec_prefix]))"
                    child = json.loads(subprocess.check_output([sys.executable, "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    child = json.loads(subprocess.check_output(["python", "-c", probe], text=True))
                    assert child == [sys.executable, sys.prefix, sys.exec_prefix]
                    assert os.environ["VIRTUAL_ENV"] == sys.prefix
                    import multiprocessing

                    with multiprocessing.get_context("spawn").Pool(1) as pool:
                        child = pool.apply(eval, ("__import__('sys').executable",))
                    assert child == sys.executable
                    # Release the pool's semaphores before restarting a worker
                    # whose interpreter is intentionally never finalized.
                    del pool
                    import gc

                    gc.collect()
                    selected = sys.executable
                    retained = 41
                    identity = object()
                    identity_id = id(identity)
                    retained + 1
                    """),
            )
            client.expect(
                "43\n", python="assert id(identity) == identity_id; retained + 2"
            )
            for request in (
                {"r": "1"},
                {"requirements": {"action": "set", "python": ["six"]}},
                {"requirements": {"r": ["cli"]}},
                {"requirements": {"duckdb": ["json"], "python": ["numpy==0.1"]}},
            ):
                result = client.send(**request)
                assert result.get("isError", True), result
                client.expect(
                    "43\n", python="assert id(identity) == identity_id; retained + 2"
                )
            client.send(python="import os.mcp_console_missing")
            assert 'File "<string>"' not in last_result_text(client)
            assert "os.mcp_console_missing" in last_result_text(client)
            assert "user-selected" not in last_result_text(client)
            client.expect(
                "43\n", python="assert id(identity) == identity_id; retained + 2"
            )
            client.send(python="print(selected)")
            selected = last_result_text(client).strip()
            # Resolution cannot run again: remove uv after successful selection.
            (path / "uv").unlink()
            discovered_r = path / "R"
            discovered_r.write_text(
                code("""
                #!/bin/sh
                exit 48
                """)
            )
            discovered_r.chmod(0o755)
            client.send(
                control="restart", python="import sys, pandas; print(sys.executable)"
            )
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n"
                + selected
                + "\n[done]"
            ), client.transcript[-1]
            records = client.finish()
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = content["text"].replace(
                                selected, "<selected Python>"
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_standard_library_environment_supports_python_and_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(venv)
        env = selected_environment(selected.parent)
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: environment\n")
        with McpClient(binary, execution.serve(), env, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "4\n",
                # fmt: python
                python=code("""
                    import importlib.util
                    import statistics

                    assert all(
                        importlib.util.find_spec(package) is None
                        for package in ("numpy", "pandas", "matplotlib", "duckdb")
                    )
                    values = [2, 4, 6]
                    statistics.mean(values)
                    """),
            )
            client.expect("5\n", python="values.append(8); statistics.mean(values)")
            client.expect(
                "standard-library help available\n",
                # fmt: python
                python=code("""
                    import contextlib
                    import io

                    with contextlib.redirect_stdout(io.StringIO()) as documentation:
                        help(statistics.mean)
                    assert "arithmetic mean" in documentation.getvalue()
                    print("standard-library help available")
                    """),
            )
            client.send(python="answer = input('value> '); answer")
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "'hello'\n", "standard-library input", stdin="hello\n"
            )
            client.send(
                # fmt: python
                python=code("""
                    import sqlite3

                    connection = sqlite3.connect(":memory:")
                    _console.sql_connection(connection)
                    """),
            )
            client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
            client.send(sql="SELECT answer FROM answers")
            assert "42" in last_result_text(client), last_result_text(client)
            return client.finish()


@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_uses_selected_virtualenv(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(venv)
        subprocess.run(
            ["uv", "pip", "install", "--python", selected, "matplotlib"],
            check=True,
            capture_output=True,
        )
        env = selected_environment(selected.parent)
        env["MPLCONFIGDIR"] = str(root / "matplotlib")
        host_temp = root / "host-temp"
        host_temp.mkdir()
        env["TMPDIR"] = str(host_temp)
        # Prepare this fixture's fonts before interpreting its path-only output.
        subprocess.run(
            [selected, "-I", "-c", "import matplotlib.font_manager"],
            env=dict(os.environ, MPLCONFIGDIR=env["MPLCONFIGDIR"]),
            check=True,
            capture_output=True,
        )
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import tempfile
                    from pathlib import Path
                    import matplotlib.pyplot as plt

                    assert os.environ["RETICULATE_PYTHON"] == sys.executable
                    assert sys.prefix != sys.base_prefix
                    value = 40
                    temporary = Path(tempfile.gettempdir())
                    (temporary / "owned.txt").write_text("owned")
                    print(temporary)
                    """)
            )
            temporary = Path(last_result_text(client).strip())
            assert temporary.is_dir(), client.transcript[-1]
            client.send(
                python="value += 1; print('before error'); raise ValueError('recoverable')"
            )
            assert "ValueError: recoverable" in last_result_text(client)
            client.expect("42\n", python="value + 1")
            client.send(python="answer = input('value> '); answer")
            assert "[waiting for stdin]" in last_result_text(client)
            wait_for_evaluation_output(
                client, "'hello'\n", "sans-R input", stdin="hello\n"
            )
            client.send(
                # fmt: python
                python=code("""
                    _ = plt.plot([1, 2], [3, 4])
                    plt.gcf().savefig(temporary / "expected.png")
                    """)
            )
            assert_result_content(
                client,
                [(temporary / "expected.png").read_bytes()],
                image_reference="selected environment savefig {page}",
            )
            client.send(python="input('interrupt> ')")
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client)
            client.expect("42\n", python="value + 1")
            client.send(control="restart", python="'value' in globals()")
            assert (
                last_result_text(client)
                == """[worker stopped: in-memory state lost]
[starting new worker]
False
[done]"""
            ), client.transcript[-1]
            assert temporary.exists() is (execution is DIRECT)
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            replacement = Path(last_result_text(client).strip())
            assert replacement.is_dir()
            assert (replacement == temporary) is (execution is DIRECT)
            records = client.finish()
            assert replacement.exists() is (execution is DIRECT)
            if execution is DIRECT:
                assert (temporary / "owned.txt").read_text() == "owned"
            assert selected.exists(), "retirement deleted the selected environment"
            for record in records:
                if "result" in record:
                    for content in record["result"].get("content", []):
                        if content.get("type") == "text":
                            content["text"] = (
                                content["text"]
                                .replace(str(temporary), "<worker temporary>")
                                .replace(str(replacement), "<replacement temporary>")
                            )
            return records


@executions(DIRECT, SANDBOXED)
def test_reports_missing_interpreters(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        if os.name != "nt":
            (Path(directory) / "python3").symlink_to(sys.executable)
        with McpClient(
            binary, execution.serve(), environment(Path(directory))
        ) as client:
            error = client.startup_error()
            assert "require `uv` on PATH" in error, error
            client.finish_with_standard_error(expected_exit_status=1)
            return [{"error": error}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_resolver_failure_does_not_fall_back(
    binary: Path, execution: Execution
) -> Transcript:
    with preparation_directory() as directory:
        path = Path(directory)
        uv = path / "uv"
        uv.write_text("#!/bin/sh\necho 'fixture uv resolution failed' >&2\nexit 47\n")
        uv.chmod(0o755)
        if os.name != "nt":
            (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), environment(path)) as client:
            error = client.startup_error()
            assert "fixture uv resolution failed" in error, error
            client.finish_with_standard_error(expected_exit_status=1)
            return [{"error": error}]


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_broken_r_instead_of_selecting_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        if os.name != "nt":
            (path / "python3").symlink_to(sys.executable)
        env = environment(path)
        env["R_HOME"] = str(path / "missing-r")
        with McpClient(binary, execution.serve(), env) as client:
            error = client.startup_error()
            assert "Rscript" in error, error
            explicit = error.replace(str(path), "<fixture>")
            client.finish_with_standard_error(expected_exit_status=1)
        env.pop("R_HOME")
        broken = path / "R"
        broken.write_text(
            code("""
            #!/bin/sh
            echo 'broken discovered R' >&2
            exit 41
            """)
        )
        broken.chmod(0o755)
        with McpClient(binary, execution.serve(), env) as client:
            error = client.startup_error()
            assert "broken discovered R" in error, error
            client.finish_with_standard_error(expected_exit_status=1)
            return [{"invalid_R_HOME": explicit}, {"broken_R": error}]


@execution_snapshots
@executions(DIRECT, SANDBOXED)
def test_startup_failure_respects_temporary_storage_ownership(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(venv)
        site = Path(
            subprocess.check_output(
                [
                    selected,
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        # This is a real selected interpreter's startup hook, not an evaluator.
        # Inspection succeeds; only the subsequently embedded worker exits.
        # fmt: python
        hook = code("""
            import os
            from pathlib import Path

            # Only the first worker fails; eager replacement must not overwrite
            # the failed worker's recorded directory or fail a second time.
            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ and not Path("startup-temporary").exists():
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                Path(os.environ["TMPDIR"], "owned-before-failure").touch()
                os._exit(47)
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(root))
            if execution == SANDBOXED
            else execution.serve()
        )
        env = selected_environment(virtualenv_python(venv).parent)
        env["RETICULATE_PYTHON"] = str(selected)
        host_temp = root / "host-temp"
        host_temp.mkdir()
        env["TMPDIR"] = str(host_temp)
        with McpClient(binary, arguments, env, current_directory=root) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('startup failure ran the cell')"
            )
            assert result["isError"] and "status 47" in last_result_text(client), result
            temporary = Path((root / "startup-temporary").read_text())
            if execution is SANDBOXED:
                assert temporary.is_relative_to(host_temp)
                assert not temporary.exists(), (
                    f"failed worker storage remains: {temporary}"
                )
            else:
                assert temporary == host_temp
                assert (temporary / "owned-before-failure").exists()
            assert selected.exists(), "startup failure deleted the environment"
            client.expect(
                "replacement initializes successfully\n",
                python="print('replacement initializes successfully')",
            )
            return client.finish()


@executions(DIRECT)
def test_describes_direct_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


@executions(SANDBOXED)
def test_describes_sandboxed_python_session(
    binary: Path, execution: Execution
) -> Transcript:
    return describe_session(binary, execution)


def describe_session(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        if os.name != "nt":
            (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), selected_environment(path)) as client:
            client.initialize_and_list_tools()
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_python_execution(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        env = selected_environment(workspace)
        env["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary,
            execution.serve(),
            env,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="recorded_value = 41; recorded_value + 1")
            client.send(python="raise ValueError('recorded failure')")
            assert "ValueError: recorded failure" in last_result_text(client)
            client.send(control="restart", python="'recorded_value' in globals()")
            assert (
                last_result_text(client)
                == """[worker stopped: in-memory state lost]
[starting new worker]
False
[done]"""
            )
            records = client.finish()
            (session,) = (workspace / ".agents/console/sessions").iterdir()
            markdown = (session / "transcript.md").read_text()
            quarto = (session / "sessions/session-001.qmd").read_text()
            assert "recorded_value = 41" in markdown and "recorded_value = 41" in quarto
            assert "execute:\n  eval: false" not in quarto
            assert "original-working-directory:" in quarto and "\nir:" in quarto
            assert (
                "ir:\n  isolated: true\n  packages: []\n  python-packages: []\n"
                in quarto
            )
            assert "ValueError: recorded failure" in markdown
            assert (session / "capture/sessions/001/cell-001.txt").read_text() == "42\n"
            events = [
                json.loads(line)
                for line in (session / "capture/events.jsonl").read_text().splitlines()
            ]
            assert events[0]["event"] == "recording_started"
            assert sum(event["event"] == "tool_call" for event in events) == 3
            assert [event["sequence"] for event in events] == list(
                range(1, len(events) + 1)
            )
            cells = [event for event in events if event["event"] == "cell_started"]
            assert [
                (
                    event["call_id"],
                    event["owner"]["session_id"],
                    event["owner"]["cell_id"],
                )
                for event in cells
            ] == [(1, 1, 1), (2, 1, 2), (3, 2, 1)]
            for cell in cells:
                ready = next(
                    event
                    for event in events
                    if event["event"] == "worker_ready"
                    and event["session_id"] == cell["owner"]["session_id"]
                )
                associated = next(
                    event
                    for event in events
                    if event["event"] == "source_associated"
                    and event["call_id"] == cell["call_id"]
                )
                assert ready["sequence"] < cell["sequence"]
                assert associated["sequence"] < cell["sequence"]
                assert associated["session_id"] == cell["owner"]["session_id"]
            replacement = (session / "sessions/session-002.qmd").read_text()
            assert "'recorded_value' in globals()" in replacement
            assert "recorded_value = 41" not in replacement
            records.append(
                {
                    "recording": {
                        "events": [
                            event["event"]
                            for event in events
                            if event["event"]
                            in {
                                "recording_started",
                                "tool_call",
                                "tool_result",
                                "cell_output",
                            }
                        ],
                        "markdown and quarto": "Python source retained",
                        "output log": "42\n",
                    }
                }
            )
            return records


@executions(DIRECT, SANDBOXED)
def test_preserves_explicit_python_selection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        env = environment(Path(directory))
        env["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.expect(
                "42\n",
                python="import os, sys; assert sys.executable == os.environ['RETICULATE_PYTHON']; identity = object(); identity_id = id(identity); 42",
            )
            for control in ({}, {"control": "restart"}):
                result = client.send(**control, requirements={"python": ["py-yaml12"]})
                assert result["isError"], result
                assert "non-managed Python session" in last_result_text(client)
                client.expect("42\n", python="assert id(identity) == identity_id; 42")
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_interrupts_python_and_replaces_a_failed_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        if os.name != "nt":
            (path / "python3").symlink_to(sys.executable)
        env = selected_environment(path)
        host_temp = path / "host-temp"
        host_temp.mkdir()
        env["TMPDIR"] = str(host_temp)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 41")
            wait_for_evaluation_output(
                client,
                "loop entered\n\n[running; poll with an empty send]",
                "Python loop entry",
                # fmt: python
                python=code("""
                    print("loop entered")
                    # Keep interrupt locations stable across Python bytecode versions.
                    while True: pass  # fmt: skip
                    """),
                timeout_ms=100,
            )
            client.send(control="interrupt")
            assert "KeyboardInterrupt" in last_result_text(client), client.transcript[
                -1
            ]
            client.expect("42\n", python="retained + 1")
            client.send(python="import tempfile; print(tempfile.gettempdir())")
            temporary = Path(last_result_text(client).strip())
            client.send(python="import os; os._exit(23)")
            assert "status 23" in last_result_text(client), client.transcript[-1]
            client.expect("42\n", python="assert 'retained' not in globals(); 42")
            assert temporary.exists() is (execution is DIRECT)
            records = client.finish()
            for record in records:
                for content in record.get("result", {}).get("content", []):
                    if content["type"] == "text":
                        content["text"] = content["text"].replace(
                            str(temporary), "<worker temporary>"
                        )
            return records


@executions(SANDBOXED)
def test_preserves_explicit_selection_in_sandbox_environment(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for inherit in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            workspace = Path(directory)
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            env = environment(workspace)
            config.write_text(
                json.dumps(
                    {
                        "environment": {
                            "RETICULATE_PYTHON": "/invalid/project/python",
                            "HOME": env["HOME"],
                            "PATH": env["PATH"],
                        },
                        "inherit_environment": inherit,
                    }
                )
            )
            env["RETICULATE_PYTHON"] = sys.executable
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code("""
                            import os
                            import sys

                            assert os.environ["RETICULATE_PYTHON"] == sys.executable
                            print("explicit selection retained")
                            """),
                    )
                    assert "explicit selection retained\n" in last_result_text(client)
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_inspection_excludes_workspace_and_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        poisoned_path = workspace / "pythonpath"
        poisoned_path.mkdir()
        # fmt: python
        payload = code("""
            import sys
            from pathlib import Path

            # Eager worker startup may import the configured site hook. Only
            # host inspection must exclude workspace and PYTHONPATH imports.
            if "_mcp_console_services" not in sys.modules:
                Path("host-import-executed").touch()
                raise RuntimeError("inspection imported workspace code")
            """)
        (workspace / "ctypes.py").write_text(payload)
        (poisoned_path / "sitecustomize.py").write_text(payload)
        env = selected_environment(workspace)
        env["RETICULATE_PYTHON"] = sys.executable
        env["PYTHONPATH"] = str(poisoned_path)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            prepared = client.send(requirements={"action": "get"})
            assert not prepared.get("isError", False), prepared
            client.expect("42\n", python="41 + 1")
            assert not (workspace / "host-import-executed").exists()
            (workspace / "ctypes.py").unlink()
            (poisoned_path / "sitecustomize.py").unlink()
            shutil.rmtree(poisoned_path / "__pycache__", ignore_errors=True)
            return client.finish()


def failed_native_startup(binary: Path, execution: Execution) -> Transcript:
    error_line = runtime_source_line("raise RuntimeError(")
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        probe = build_interposer(workspace, "python_exit_state")
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(venv)
        site = Path(
            subprocess.check_output(
                [
                    selected,
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        # A real installed startup hook changes only the embedded worker.
        # fmt: python
        hook = code(f"""
            import os
            import sys

            if "MCP_CONSOLE_LOCAL_RUNTIME" in os.environ:
                import ctypes
                from pathlib import Path

                probe = ctypes.CDLL({str(probe)!r})
                Path("startup-temporary").write_text(os.environ["TMPDIR"])
                sys.prefix = "changed-by-startup-hook"
            """)
        (site / "sitecustomize.py").write_text(hook)
        arguments = (
            execution.serve("--writable-root", str(workspace))
            if execution == SANDBOXED
            else execution.serve()
        )
        env = selected_environment(virtualenv_python(venv).parent)
        env["RETICULATE_PYTHON"] = str(selected)
        host_temp = workspace / "host-temp"
        host_temp.mkdir()
        env["TMPDIR"] = str(host_temp)
        with McpClient(binary, arguments, env, workspace) as client:
            client.initialize_and_list_tools()
            result = client.send(
                python="raise AssertionError('failed startup ran cell')"
            )
            assert result["isError"], result
            records = client.finish()
            temporary = Path((workspace / "startup-temporary").read_text())
            assert temporary.exists() is (execution is DIRECT)
            assert selected.exists(), "failed startup removed selected environment"
            content = records[-1]["result"]["content"][0]
            diagnostic = (
                "Traceback (most recent call last):\n"
                f'  File "<string>", line {error_line}, in _mcp_console_configure_environment\n'
                "RuntimeError: embedded Python prefix differs from the selected environment: "
                f"'changed-by-startup-hook' != {str(venv)!r}\n"
            )
            stderr = (
                "Python environment setup failed; restart required\n"
                "Python exit thread attached\n"
            )
            lifecycle = (
                "[worker sideband read failed: worker sideband closed]\n"
                "[worker exited with status 1]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n[idle]"
            )
            assert content["text"].endswith(lifecycle), content
            # Sideband diagnostics and terminal stderr are independent streams.
            # Preserve every byte and each producer's order before recording.
            assert_exact_interleaving(
                content["text"][: -len(lifecycle)], diagnostic, stderr
            )
            content["text"] = (diagnostic + stderr + lifecycle).replace(
                str(venv), "<selected environment>"
            )
            return records


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_startup_failure_restores_python_thread(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert "Python exit thread attached\n" in diagnostic, diagnostic
    assert "Python exit thread detached" not in diagnostic, diagnostic
    return records


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_startup_failure_preserves_python_exception(
    binary: Path, execution: Execution
) -> Transcript:
    records = failed_native_startup(binary, execution)
    diagnostic = records[-1]["result"]["content"][0]["text"]
    assert (
        "RuntimeError: embedded Python prefix differs from the selected environment"
        in diagnostic
    ), diagnostic
    assert "'changed-by-startup-hook' != '<selected environment>'" in diagnostic, (
        diagnostic
    )
    return records


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_uses_environment_through_directory_alias(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        original = workspace / "original"
        original.mkdir()
        venv = original / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        alias = workspace / "alias"
        alias.symlink_to(original, target_is_directory=True)
        env = selected_environment(alias / "environment/bin")
        env["MCP_CONSOLE_TEST_ENVIRONMENT"] = str(venv)
        with McpClient(binary, execution.serve(), env) as client:
            client.initialize_and_list_tools()
            client.expect(
                "selected environment retained through directory alias\n",
                # fmt: python
                python=code("""
                    import os
                    import sys
                    import subprocess

                    assert os.environ["RETICULATE_PYTHON"] == sys.executable
                    assert os.path.samefile(sys.prefix, os.environ["MCP_CONSOLE_TEST_ENVIRONMENT"])
                    assert os.path.samefile(sys.exec_prefix, sys.prefix)
                    child = subprocess.check_output(
                        [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                    ).strip()
                    assert os.path.samefile(child, sys.prefix)
                    print("selected environment retained through directory alias")
                    """),
            )
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_consumes_idle_interrupt_before_next_python_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory)
        if os.name != "nt":
            (path / "python3").symlink_to(sys.executable)
        with McpClient(binary, execution.serve(), selected_environment(path)) as client:
            client.initialize_and_list_tools()
            client.send(python="retained = 40")
            client.send(control="interrupt")
            client.send(python="retained += 1; retained")
            assert last_result_text(client) == "41\n", client.transcript[-1]
            client.send(control="interrupt", python="retained += 1; retained")
            assert last_result_text(client) == "42\n[done]", client.transcript[-1]
            client.send(python="retained")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_imports_workspace_modules_without_pythonpath(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        if os.name != "nt":
            (root / "python3").symlink_to(sys.executable)
        (workspace / "workspace_module.py").write_text("value = 20\n")
        package = workspace / "workspace_package"
        package.mkdir()
        (package / "__init__.py").write_text("value = 22\n")
        subdirectory = workspace / "subdirectory"
        subdirectory.mkdir()
        (subdirectory / "after_chdir.py").write_text("value = 43\n")
        env = selected_environment(root)
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import sys
                        import workspace_module
                        import workspace_package

                        assert "PYTHONPATH" not in os.environ
                        assert sys.path[0] == ""
                        assert workspace_module.value + workspace_package.value == 42
                        os.chdir("subdirectory")
                        import after_chdir

                        after_chdir.value
                        """),
                )
                assert "43\n" in last_result_text(client), client.transcript[-1]
                assert not client.transcript[-1]["result"]["isError"]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonhome_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONHOME")


@executions(DIRECT, SANDBOXED)
def test_ignores_pythonplatlibdir_for_selected_environment(
    binary: Path, execution: Execution
) -> Transcript:
    return ignores_python_layout_override(binary, execution, "PYTHONPLATLIBDIR")


def ignores_python_layout_override(
    binary: Path, execution: Execution, variable: str
) -> Transcript:
    records = []
    for inherit in (True, False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            venv = root / "environment"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", venv],
                check=True,
                capture_output=True,
            )
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            env = selected_environment(virtualenv_python(venv).parent)
            config.write_text(
                json.dumps(
                    {
                        "environment": {
                            variable: "unavailable-configured-layout",
                            "HOME": env.get("HOME") or str(Path.home()),
                            "PATH": env["PATH"],
                            "TMPDIR": str(root),
                            "TEMP": str(root),
                            "TMP": str(root),
                        },
                        "inherit_environment": inherit,
                    }
                )
            )
            env[variable] = "unavailable-inherited-layout"
            with McpClient(binary, execution.serve(), env, workspace) as client:
                client.initialize_and_list_tools()
                for control in ({}, {"control": "restart"}):
                    client.send(
                        **control,
                        # fmt: python
                        python=code(f"""
                            import os
                            import sys
                            import subprocess

                            assert "{variable}" not in os.environ
                            assert os.environ["RETICULATE_PYTHON"] == sys.executable
                            assert os.path.samefile(sys.prefix, "../environment")
                            assert sys.prefix != sys.base_prefix
                            child = subprocess.check_output(
                                [sys.executable, "-c", "import sys; print(sys.prefix)"], text=True
                            ).strip()
                            assert os.path.samefile(child, sys.prefix)
                            print("selected environment retained")
                            """),
                    )
                    assert "selected environment retained\n" in last_result_text(
                        client
                    ), client.transcript[-1]
                records.extend(client.finish())
    return records


@executions(DIRECT, SANDBOXED)
def test_accepts_parent_components_in_selected_executable(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        venv = workspace / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        (workspace / "alias").mkdir()
        selected = (
            workspace / "alias/.." / virtualenv_python(venv).relative_to(workspace)
        )
        env = environment(workspace)
        env["RETICULATE_PYTHON"] = selected.as_posix()
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                client.send(
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import subprocess
                        import sys

                        selected = os.environ["RETICULATE_PYTHON"]
                        assert "/../" in selected.replace(os.sep, "/")
                        assert os.path.samefile(sys.executable, selected)
                        assert os.path.samefile(sys.prefix, "environment")
                        assert sys.prefix != sys.base_prefix
                        child = subprocess.check_output(
                            [sys.executable, "-c", "import sys; print(sys.executable); print(sys.prefix)"],
                            text=True,
                        ).splitlines()
                        assert os.path.samefile(child[0], selected)
                        assert os.path.samefile(child[1], sys.prefix)
                        print("selected executable and environment retained")
                        """),
                )
                assert (
                    "selected executable and environment retained\n"
                    in last_result_text(client)
                ), client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_excludes_executable_directory_from_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--copies", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(venv)
        site = Path(
            subprocess.check_output(
                [
                    selected,
                    "-I",
                    "-c",
                    "import sysconfig; print(sysconfig.get_path('purelib'))",
                ],
                text=True,
            ).strip()
        )
        (site / "selected_package.py").write_text("value = 42\n")
        (selected.parent / "json.py").write_text(
            "raise RuntimeError('imported executable directory')\n"
        )
        (selected.parent / "selected_package.py").write_text("value = -1\n")
        env = selected_environment(virtualenv_python(venv).parent)
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                expected = "'{\"selected package\": 42}'\n"
                if control:
                    expected = (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n" + expected + "[done]"
                    )
                # Exercise an early response and collect the completed evaluation.
                client.expect(
                    expected,
                    **control,
                    timeout_ms=0,
                    # fmt: python
                    python=code("""
                        import json
                        import os
                        import sys
                        import selected_package

                        assert sys.path[0] == ""
                        assert os.path.dirname(sys.executable) not in sys.path
                        assert selected_package.value == 42, (
                            selected_package.value,
                            selected_package.__file__,
                            sys.path,
                            sys.prefix,
                        )
                        json.dumps({"selected package": selected_package.value})
                        """),
                )
            return client.finish()


@requires(windows_short_paths())
@executions(DIRECT)
def test_short_executable_path_preserves_virtualenv_imports(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(prefix="console-short-path-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        venv = root / "environment"
        subprocess.run(
            [sys.executable, "-m", "venv", "--copies", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(windows_short_path(venv))
        assert selected != virtualenv_python(venv)
        (venv / "Lib/site-packages/selected_package.py").write_text("value = 42\n")
        (selected.parent / "selected_package.py").write_text("value = -1\n")
        env = selected_environment(selected.parent)
        env.pop("PYTHONPATH", None)
        with McpClient(binary, execution.serve(), env, workspace) as client:
            client.initialize_and_list_tools()
            for control in ({}, {"control": "restart"}):
                expected = "short executable path retained the selected environment\n"
                if control:
                    expected = (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n" + expected + "[done]"
                    )
                client.expect(
                    expected,
                    **control,
                    # fmt: python
                    python=code("""
                        import os
                        import sys
                        import selected_package

                        assert sys.executable == os.environ["RETICULATE_PYTHON"]
                        assert os.path.samefile(os.path.dirname(sys._base_executable), sys.base_prefix)
                        assert all(
                            not os.path.samefile(path, os.path.dirname(sys.executable))
                            for path in sys.path
                            if os.path.isdir(path)
                        )
                        assert selected_package.value == 42, selected_package.__file__
                        print("short executable path retained the selected environment")
                        """),
                )
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_records_managed_python_defaults(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        expose_uv(workspace)
        with McpClient(
            binary, execution.serve(), environment(workspace), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import numpy, pandas

                    retained = 42
                    retained
                    """),
            )
            client.send(requirements={"action": "set", "python": ["six"]})
            assert client.transcript[-1]["result"]["isError"]
            client.expect("42\n", python="retained")
            records = client.finish()
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        quarto = (session / "sessions/session-001.qmd").read_text()
        defaults = ["numpy", "pandas", "matplotlib", "plotnine"]
        if SQL.available:
            defaults.append("duckdb")
        assert (
            "  python-packages:\n"
            + "".join(f"    - {package}\n" for package in defaults)
            in quarto
        ), quarto
        assert "original-working-directory:" in quarto and "\nir:" in quarto, quarto
        assert "eval: false" not in quarto, quarto
        assert "six" not in quarto, quarto
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        assert (
            next(
                event for event in events if event["event"] == "environment_discovered"
            )["dynamic_resolution"]
            is False
        )
        assert (
            next(
                event for event in events if event["event"] == "environment_discovered"
            )["python_preparation"]
            is True
        )
        return TranscriptWithCompanions(
            records,
            {
                "qmd": quarto.replace(str(workspace.resolve()), "<workspace>").replace(
                    str(workspace), "<workspace>"
                )
            },
        )
