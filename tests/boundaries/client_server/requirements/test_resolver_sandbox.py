"""Resolver permissions through the public MCP preparation API."""

import json
import os
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, SANDBOX, SQL, command, requires
from support.assertions import (
    last_result_text,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.progress import without_elapsed
from support.records import ToolResult, Transcript
from support.r import r_test_environment
from support.resolvers import ir_cache_directory
from boundaries.client_server.python.test_without_r import environment


@requires(SQL)
@requires(SANDBOX)
def test_default_caches_use_console_namespace(binary: Path) -> Transcript:
    return console_cache_defaults(binary, configured_empty=False)


@requires(SANDBOX)
def test_empty_cache_overrides_use_console_defaults(binary: Path) -> Transcript:
    return console_cache_defaults(binary, configured_empty=True)


def console_cache_defaults(binary: Path, *, configured_empty: bool) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        env = environment(tools)
        home = root / "home"
        env.update(HOME=str(home), XDG_CACHE_HOME="", UV_NO_CONFIG="1")
        for name in (
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "UV_TOOL_DIR",
            "UV_CONFIG_FILE",
            "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
            "MPLCONFIGDIR",
        ):
            env.pop(name, None)
        payload = home / (
            "Library/Caches/mcp-console/dependencies"
            if sys.platform == "darwin"
            else ".cache/mcp-console/dependencies"
        )
        if configured_empty:
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "resolver": {
                            "environment": {
                                name: ""
                                for name in (
                                    "UV_CACHE_DIR",
                                    "UV_PYTHON_INSTALL_DIR",
                                    "UV_TOOL_DIR",
                                    "IR_CACHE_DIR",
                                    "R_USER_CACHE_DIR",
                                    "RENV_PATHS_ROOT",
                                    "PKG_CACHE_DIR",
                                    "MPLCONFIGDIR",
                                    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
                                )
                            }
                        }
                    }
                )
            )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                lambda text: Path(text.strip()).is_relative_to(payload),
                requirements={"python": ["six"]},
                python="import six, sys; print(sys.executable)",
            )
            client.expect(
                lambda text: str(payload / "duckdb/extensions") in text,
                sql="SELECT current_setting('extension_directory') AS directory",
            )
            client.send(control="restart")
            client.expect("retained\n", python="import six; print('retained')")
            client.finish()
        assert (payload / "uv/cache").is_dir()
        assert not (home / ".cache/uv").exists()
        assert not (home / "Library/Caches/uv").exists()
        assert not (home / "Library/Application Support/uv").exists()
    result = {"default_caches": "console-owned", "empty_xdg": "HOME fallback"}
    if configured_empty:
        result["empty_cache_overrides"] = "Console defaults"
    return [result]


@requires(SANDBOX)
def test_python_duckdb_uses_resolver_cache(binary: Path) -> Transcript:
    return duckdb_cache(binary, r=False)


@requires(SQL)
@requires(SANDBOX, R, command("ir"))
def test_r_duckdb_uses_resolver_cache(binary: Path) -> Transcript:
    return duckdb_cache(binary, r=True)


@requires(SQL, SANDBOX, POSIX)
def test_duckdb_cache_waits_for_terminal_sql_results(binary: Path) -> Transcript:
    class GatedSqlClient(McpClient):
        """Return a real running SQL response before releasing its DuckDB UDF."""

        checkpoints: tuple[FifoCheckpoint, ...] = ()
        query_gated = False
        sql_submissions = 0

        def initialize_and_list_tools(self) -> None:
            super().initialize_and_list_tools()
            self.expect(
                lambda text: Path(text.strip()).is_dir(),
                python="import os; print(os.environ['TMPDIR'])",
            )
            temporary = Path(last_tool_text(self).strip())
            self.checkpoints = tuple(
                FifoCheckpoint.create(temporary / name)
                for name in ("cache-query-started", "cache-query-release")
            )
            self.expect(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    gate_calls = 0


                    def resolver_cache_gate() -> bool:
                        global gate_calls
                        gate_calls += 1
                        assert gate_calls == 1, "SQL query was replayed"
                        temporary = Path(os.environ["TMPDIR"])
                        with (temporary / "cache-query-started").open("wb", buffering=0) as started:
                            assert started.write(b"1") == 1
                        with (temporary / "cache-query-release").open("rb", buffering=0) as release:
                            assert release.read(1) == b"1"
                        return True


                    _ = _console.sql_connection().create_function(
                        "resolver_cache_gate", resolver_cache_gate, [], "BOOLEAN", side_effects=True
                    )
                    """),
            )

        def send(self, **arguments: Any) -> ToolResult:
            if "sql" in arguments:
                self.sql_submissions += 1
            if "sql" not in arguments or self.query_gated:
                return super().send(**arguments)
            self.query_gated = True
            result = super().send(
                **(
                    arguments
                    | {
                        "sql": arguments["sql"] + " WHERE resolver_cache_gate()",
                        "timeout_ms": 0,
                    }
                )
            )
            started, release = self.checkpoints
            started.wait("managed DuckDB query entered its release gate")
            assert without_elapsed(last_tool_text(self)) == (
                "\n[running; poll with an empty send]"
            ), last_tool_text(self)
            release.release()
            return result

        def finish(self) -> Transcript:
            assert self.sql_submissions == 4, "submit each SQL cell once"
            return super().finish()

        def close(self) -> None:
            try:
                super().close()
            finally:
                for checkpoint in self.checkpoints:
                    checkpoint.close()

    return [
        *duckdb_cache(binary, r=False, client_type=GatedSqlClient),
        {
            "initial_sql_response": "running",
            "query_executions_per_gate": 1,
            "sql_submissions_per_cache": 4,
        },
    ]


@requires(POSIX, R, SQL, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_custom_worker_captures_duckdb_cache_before_live_r(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    for explicit in (False, True):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            env, _ = r_test_environment()
            env.pop("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY", None)
            env["MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS"] = str(root / "cells.jsonl")
            home = root / "resolver-home"
            home.mkdir()
            cache = root / "extensions" if explicit else home / ".duckdb/extensions"
            resolver_env = {
                "HOME": str(home),
                "IR_CACHE_DIR": env.get(
                    "IR_CACHE_DIR",
                    str(
                        Path(env["HOME"])
                        / (
                            "Library/Caches/org.R-project.R/R/ir"
                            if sys.platform == "darwin"
                            else ".cache/R/ir"
                        )
                    ),
                ),
            }
            if explicit:
                resolver_env["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(cache)
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps({"cache": "host", "resolver": {"environment": resolver_env}})
            )
            with McpClient(
                binary,
                execution.serve(
                    *(("--writable-root", str(root)) if execution is SANDBOXED else ()),
                    "--worker",
                    str(zod),
                ),
                env,
                root,
            ) as client:
                client.initialize_and_list_tools()
                # Launch precedes the first managed-R layer. All later checks
                # use the same cache selection captured at Console startup.
                for phase in ("launch", "live R", "restart"):
                    if phase == "live R":
                        prepared = client.send(
                            requirements={"r": ["praise"], "duckdb": ["sqlite"]}
                        )
                        assert not prepared.get("isError"), prepared
                        assert list(cache.glob("v*/**/sqlite_scanner.duckdb_extension"))
                    elif phase == "restart":
                        client.send(control="restart")
                    client.expect(
                        str(cache) + "\n",
                        # fmt: python
                        python=code("""
                            import os

                            print(os.environ.get("MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"))
                            """),
                    )
                client.finish()
    return [{"custom_cache_captured_at_launch": True, "live_r_cache_matches": True}]


def duckdb_cache(
    binary: Path, *, r: bool, client_type: type[McpClient] = McpClient
) -> Transcript:
    for explicit in (False, True):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tools = root / "bin"
            tools.mkdir()
            (tools / "uv").symlink_to(shutil.which("uv"))
            env = r_test_environment()[0] if r else environment(tools)
            home = root / "resolver-home"
            home.mkdir()
            cache = root / "extensions" if explicit else home / ".duckdb/extensions"
            resolver_env = {"HOME": str(home)}
            if r:
                resolver_env["IR_CACHE_DIR"] = env.get(
                    "IR_CACHE_DIR",
                    str(
                        Path(env["HOME"])
                        / (
                            "Library/Caches/org.R-project.R/R/ir"
                            if sys.platform == "darwin"
                            else ".cache/R/ir"
                        )
                    ),
                )
            if explicit:
                resolver_env["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(cache)
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            settings = {"cache": "host", "resolver": {"environment": resolver_env}}
            if r:
                settings["r"] = {"packages": []}
            config.write_text(json.dumps(settings))
            with client_type(binary, ("serve",), env, root) as client:
                client.initialize_and_list_tools()
                wait_for_evaluation_output(
                    client,
                    None,
                    "DuckDB resolver-cache directory query",
                    completion_timeout_seconds=client.response_timeout,
                    sql="SELECT current_setting('extension_directory') AS directory",
                )
                result = client.transcript[-1]["result"]
                assert not result.get("isError"), result
                assert str(cache) in last_tool_text(client), last_tool_text(client)
                assert list(cache.glob("v*/**/sqlite_scanner.duckdb_extension"))
                wait_for_evaluation_output(
                    client,
                    None,
                    "DuckDB resolver-cache extension load",
                    completion_timeout_seconds=client.response_timeout,
                    sql="LOAD sqlite",
                )
                wait_for_evaluation_output(
                    client,
                    None,
                    "DuckDB resolver-cache loaded-state query",
                    completion_timeout_seconds=client.response_timeout,
                    sql="SELECT CASE WHEN loaded THEN 'loaded' ELSE 'missing' END AS state FROM duckdb_extensions() WHERE extension_name = 'sqlite_scanner'",
                )
                assert "loaded" in last_tool_text(
                    client
                ) and "missing" not in last_tool_text(client), last_tool_text(client)
                config.write_text("resolver: {environment: {HOME: /missing}}\n")
                client.send(control="restart")
                wait_for_evaluation_output(
                    client,
                    None,
                    "DuckDB resolver-cache directory query after restart",
                    completion_timeout_seconds=client.response_timeout,
                    sql="SELECT current_setting('extension_directory') AS directory",
                )
                assert str(cache) in last_tool_text(client), last_tool_text(client)
                client.finish()
    return [
        {
            "resolver_home_cache": True,
            "explicit_cache": True,
            "retained_across_restart": True,
        }
    ]


@requires(SANDBOX)
def test_default_resolver_permissions(binary: Path) -> Transcript:
    return permissions(binary, tailored=False)


@requires(SANDBOX)
def test_retains_tailored_resolver_policy(binary: Path) -> Transcript:
    return permissions(binary, tailored=True)


@requires(SANDBOX)
def test_preserves_python_selection_in_resolver_environment(binary: Path) -> Transcript:
    for source in ("managed", "environment", "config"):
        for inherit in (False, True):
            with TemporaryDirectory() as directory:
                root = Path(directory).resolve()
                tools = root / "bin"
                tools.mkdir()
                (tools / "uv").symlink_to(shutil.which("uv"))
                env = environment(tools)
                if source == "environment":
                    env["RETICULATE_PYTHON"] = sys.executable
                elif source == "config":
                    env["RETICULATE_PYTHON"] = str(root / "missing-server-python")
                settings = {
                    "resolver": {
                        "inherit_environment": inherit,
                        "environment": {
                            "HOME": env["HOME"],
                            "PATH": env["PATH"],
                            "RETICULATE_PYTHON": sys.executable,
                            "UV_NO_CONFIG": "1",
                        },
                    }
                }
                if source == "config":
                    settings["python"] = sys.executable
                config = root / ".agents/console/config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text(json.dumps(settings))
                with McpClient(binary, ("serve",), env, root) as client:
                    client.initialize_and_list_tools()
                    for restart in (False, True):
                        if restart:
                            client.send(control="restart")
                        if source == "managed":
                            client.expect(
                                "managed selection retained\n",
                                # fmt: python
                                python=code("""
                                    import os

                                    assert "RETICULATE_PYTHON" not in os.environ
                                    assert "MCP_CONSOLE_MANAGED_PYTHON" in os.environ
                                    print("managed selection retained")
                                    """),
                            )
                        else:
                            client.expect(
                                "explicit selection retained\n",
                                # fmt: python
                                python=code("""
                                    import os
                                    import sys

                                    assert os.environ["RETICULATE_PYTHON"] == sys.executable
                                    assert "MCP_CONSOLE_MANAGED_PYTHON" not in os.environ
                                    print("explicit selection retained")
                                    """),
                            )
                        inspected = client.send(requirements={"action": "get"})
                        requirements = inspected["structuredContent"]["requirements"]
                        assert bool(requirements["python"]) == (source == "managed")
                    client.finish()
    return [{"python_selection_preserved": ["managed", "environment", "config"]}]


@requires(SANDBOX, R, command("ir"))
def test_discovers_r_from_resolver_environment(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        env = dict(environment(tools), MCP_CONSOLE_LANGUAGES="r")
        r_env, _ = r_test_environment()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "resolver": {
                        "environment": {
                            "R_HOME": r_env["R_HOME"],
                            "PATH": r_env["PATH"],
                        }
                    },
                    "environment": {"PATH": r_env["PATH"]},
                }
            )
        )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("resolver R selected\n", r='cat("resolver R selected\\n")')
            client.finish()
        return [{"configured_resolver_r_available": True}]


@requires(SANDBOX, R)
def test_omits_r_removed_by_resolver_environment(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        without_r = environment(tools)
        env, _ = r_test_environment()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "resolver": {
                        "environment": {
                            "HOME": env["HOME"],
                            "PATH": without_r["PATH"],
                            "UV_CACHE_DIR": str(root / "uv-cache"),
                        },
                        "inherit_environment": False,
                    }
                }
            )
        )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("prepared\n", python="print('prepared')")
            failure = client.send(r="1 + 1")
            assert failure.get("isError"), failure
            assert (
                last_result_text(client)
                == "R cells are unavailable in Python sessions without R"
            )
            client.finish()
        return [{"ambient_r_removed": True, "python_prepared": True}]


@requires(SANDBOX)
def test_rejects_non_utf8_cache_paths(binary: Path) -> Transcript:
    for name in ("UV_CACHE_DIR", "HOME"):
        with TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            tools = root / "bin"
            tools.mkdir()
            (tools / "uv").symlink_to(shutil.which("uv"))
            invalid = root / os.fsdecode(b"cache-\xff")
            env = dict(environment(tools), **{name: str(invalid)})
            with McpClient(binary, ("serve", "-c", "cache=host"), env, root) as client:
                client.initialize_and_list_tools()
                failure = client.send(requirements={"action": "get"})
                assert failure.get("isError"), failure
                diagnostic = "resolver policy paths must be UTF-8"
                assert last_result_text(client) == diagnostic, failure
                assert not invalid.exists()
                assert client.send(requirements={"action": "get"}) == failure
                client.request("ping")
                _, errors = client.finish_with_standard_error(expected_exit_status=1)
                assert errors == diagnostic + "\n", errors
    return [
        {"invalid_cache_paths": ["UV_CACHE_DIR", "HOME"], "cache_not_created": True}
    ]


@requires(SANDBOX)
def test_ignores_relative_uv_xdg_directories(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        (tools / "uv").symlink_to(shutil.which("uv"))
        env = environment(tools)
        for name in (
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "UV_TOOL_DIR",
            "UV_CONFIG_FILE",
        ):
            env.pop(name, None)
        env["UV_NO_CONFIG"] = "1"
        home = root / "resolver-home"
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "cache": "host",
                    "resolver": {
                        "environment": {
                            "HOME": str(home),
                            "XDG_CACHE_HOME": "relative-cache",
                            "XDG_DATA_HOME": "relative-data",
                        }
                    },
                }
            )
        )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            client.expect("prepared\n", python="print('prepared')")
            client.finish()
        assert not (root / "relative-cache/uv").exists()
        assert not (root / "relative-data/uv").exists()
        assert (home / ".cache/uv").is_dir()
        assert (home / ".local/share/uv/python").is_dir()
        assert list(home.rglob("pyvenv.cfg"))
        return [{"relative_uv_xdg_ignored": True, "managed_python_prepared": True}]


@requires(SANDBOX, R, command("ir"))
def test_bootstraps_reticulate_uv_with_host_cache_policy(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env, _ = r_test_environment()
        env["IR_CACHE_DIR"] = ir_cache_directory(env)
        env.update(XDG_CACHE_HOME=str(root / "cache"), RETICULATE_UV="managed")
        for name in (
            "RETICULATE_PYTHON",
            "R_USER_CACHE_DIR",
            "UV_CACHE_DIR",
            "UV_TOOL_DIR",
            "UV_PYTHON_INSTALL_DIR",
        ):
            env.pop(name, None)
        # Cold shell installation on macOS needs the host policy's Darwin temp
        # grant. The redirected XDG cache keeps this fixture's artifacts private.
        with McpClient(binary, ("serve", "-c", "cache=host"), env, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                "managed uv prepared\n",
                requirements={"python": ["six"]},
                python="import six; print('managed uv prepared')",
            )
            client.finish()
        cache = root / "cache"
        assert (cache / "R/reticulate/uv/bin/uv").is_file()
        assert not (cache / "reticulate").exists()
    return [{"reticulate_managed_uv": "R/reticulate", "cache_write_granted": True}]


@requires(SANDBOX, R, command("ir"))
def test_cold_r_cache_resolution(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        env, _ = r_test_environment()
        env.update(
            XDG_CACHE_HOME=str(root / "cache-base"),
            IR_CACHE_DIR=str(root / "host-ir"),
            R_USER_CACHE_DIR=str(root / "host-r-cache"),
            RENV_PATHS_CACHE=str(root / "host-renv"),
            MCP_CONSOLE_LANGUAGES="r",
        )
        with McpClient(binary, ("serve",), env, root) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                lambda output: output.endswith("TRUE\n"),
                "cold R preparation and evaluation",
                completion_timeout_seconds=client.response_timeout,
                requirements={"action": "set", "r": ["utf8"]},
                # fmt: r
                r=code(r"""
                    root <- paste0(normalizePath(Sys.getenv("R_USER_CACHE_DIR")), "/")
                    stopifnot(startsWith(normalizePath(find.package("utf8")), root))
                    cat(utf8::utf8_valid("resolved"), "\n", sep = "")
                    """),
            )
            client.finish()
        cache = root / "cache-base/mcp-console/dependencies"
        descriptions = list((cache / "ir").rglob("utf8/DESCRIPTION"))
        assert descriptions
        assert all(path.resolve().is_relative_to(cache) for path in descriptions)
        assert not (root / "host-ir").exists()
        assert not (root / "host-r-cache").exists()
        assert not (root / "host-renv").exists()
        return [{"cold_ir_cache": True, "resolved_r_package": "utf8"}]


def permissions(binary: Path, *, tailored: bool) -> Transcript:
    from contextlib import ExitStack

    with ExitStack() as stack:
        # The protected workspace must be outside Darwin's granted user temp.
        directory = stack.enter_context(
            TemporaryDirectory(
                prefix="mcp-console-resolver-permissions-", dir=Path.home()
            )
        )
        root = Path(directory).resolve()
        tools = root / "bin"
        tools.mkdir()
        cache = root / "uv-cache"
        cache.mkdir()
        protected = root / "protected"
        protected.mkdir()
        real_uv = shutil.which("uv")
        assert real_uv is not None
        # This is the selected resolver executable, before it delegates to uv.
        # Assert actual enforcement rather than relying on the sandbox marker.
        # fmt: python
        probe = code(r"""
            import errno
            import http.client
            import json
            import os
            from pathlib import Path
            import sys
            import urllib.request
            from urllib.parse import urlsplit

            cache = Path(os.environ["UV_CACHE_DIR"])
            protected = Path(os.environ["RESOLVER_TEST_PROTECTED"])
            assert protected.joinpath("readable").read_text() == "host read"
            try:
                protected.joinpath("denied").write_text("host write")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
            else:
                raise AssertionError("resolver wrote outside its cache grants")
            assert os.environ["CODEX_NETWORK_PROXY_ACTIVE"] == "1"
            try:
                urllib.request.urlopen("https://example.com", timeout=10)
            except urllib.error.URLError as error:
                assert "403" in str(error), error
            else:
                raise AssertionError("resolver reached a disallowed download host")
            approved = os.environ.get("RESOLVER_TEST_APPROVED")
            if approved:
                Path(approved).joinpath("created").write_text("custom grant")
                try:
                    Path(os.environ["IR_CACHE_DIR"]).joinpath("denied").write_text("excluded cache")
                except OSError as error:
                    assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                else:
                    raise AssertionError("explicit entries retained a default cache grant")
                proxy = urlsplit(os.environ["HTTP_PROXY"])
                connection = http.client.HTTPConnection(proxy.hostname, proxy.port, timeout=10)
                connection.request("GET", os.environ["RESOLVER_TEST_ALLOWED_URL"])
                response = connection.getresponse()
                assert (response.status, response.read()) == (200, b"approved resolver host")
                connection.close()
            cache.joinpath("probe.json").write_text(json.dumps({
                "host_read": True, "cache_write": True,
                "host_write_denied": True, "disallowed_host_denied": True,
            }))
            os.execv(os.environ["RESOLVER_TEST_UV"], ["uv", *sys.argv[1:]])
            """)
        (tools / "uv").write_text(f"#!{sys.executable}\n" + probe, encoding="utf-8")
        (tools / "uv").chmod(0o755)
        (protected / "readable").write_text("host read")
        env = dict(
            environment(tools),
            UV_CACHE_DIR=str(cache),
            MCP_CONSOLE_LANGUAGES="python",
            MCP_CONSOLE_HOME=str(root / "console-home"),
            RESOLVER_TEST_PROTECTED=str(protected),
            RESOLVER_TEST_UV=real_uv,
        )
        config = root / ".agents/console/config.yaml"
        if tailored:

            class Origin(BaseHTTPRequestHandler):
                def do_GET(self):
                    body = b"approved resolver host"
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def log_message(self, *_):
                    pass

            origin = stack.enter_context(ThreadingHTTPServer(("127.0.0.1", 0), Origin))
            thread = threading.Thread(target=origin.serve_forever)
            thread.start()
            stack.callback(thread.join)
            stack.callback(origin.shutdown)
            env["RESOLVER_TEST_ALLOWED_URL"] = f"http://127.0.0.1:{origin.server_port}/"
            approved = root / "approved"
            approved.mkdir()
            excluded = root / "excluded-cache"
            excluded.mkdir()
            env["IR_CACHE_DIR"] = str(excluded)
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "resolver": {
                            "environment": {"RESOLVER_TEST_APPROVED": str(approved)},
                            "sandbox": {
                                "filesystem": {
                                    "read_only": ["/"],
                                    "read_write": ["uv-cache", "approved"],
                                },
                                "network": {
                                    "proxy": {
                                        "domains": {
                                            "allow": [
                                                "pypi.org",
                                                "files.pythonhosted.org",
                                                "127.0.0.1",
                                            ]
                                        }
                                    }
                                },
                            },
                        }
                    }
                )
            )
        with McpClient(binary, ("serve", "-c", "cache=host"), env, root) as client:
            client.initialize_and_list_tools()
            if tailored:
                config.write_text(
                    "resolver: {proxy: {domains: {example.com: allow}}}\n"
                )
            client.send(requirements={"action": "set", "python": ["six"]})
            assert not client.transcript[-1]["result"].get("isError"), (
                client.transcript[-1]
            )
            client.send(python="import six; print(six.__name__)")
            assert last_tool_text(client).endswith("six\n"), last_tool_text(client)
            if tailored:
                client.send(control="restart")
                client.send(python="import six; print(six.__name__)")
                assert last_tool_text(client).endswith("six\n"), last_tool_text(client)
            client.finish()
        assert not (protected / "denied").exists()
        result = json.loads((cache / "probe.json").read_text())
        if tailored:
            assert (approved / "created").read_text() == "custom grant"
            assert not (excluded / "denied").exists()
            result.update(
                custom_write=True,
                replaced_cache_grants=True,
                custom_download_host=True,
                captured_policy=True,
            )
        return [result]
