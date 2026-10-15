"""Temporary-storage ownership through the public MCP interface."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.resolvers import expose_uv
from support.linux_sandbox import retain_system_bwrap
from support.records import Transcript
from support.requirements import POSIX, R, SANDBOX, command, requires
from support.snapshots import execution_snapshots
from support.suites import run_this_suite


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_host_temp_preserves_environment_and_r_cleans_its_session(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        temporary = workspace / "host-temp"
        temporary.mkdir()
        (temporary / "keep").write_text("caller-owned")
        cache = workspace / "cache"
        cache.mkdir()
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        settings = {
            "cache": "host",
            "python": sys.executable,
            "languages": ["r", "python"],
            "environment": {
                "TMPDIR": str(temporary),
                "MPLCONFIGDIR": str(cache),
                "XDG_CACHE_HOME": str(cache),
            },
        }
        if execution is SANDBOXED:
            settings["sandbox"] = {"tempdir": "read_write"}
        config.write_text(json.dumps(settings))
        with McpClient(binary, execution.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                "R session tempdir created beneath host TMPDIR\n",
                # fmt: r
                r=code(r"""
                    parent <- Sys.getenv("TMPDIR")
                    stopifnot(identical(dirname(tempdir()), parent))
                    writeLines(tempdir(), file.path(parent, "r-session.txt"))
                    writeLines("R-owned", file.path(tempdir(), "probe"))
                    cat("R session tempdir created beneath host TMPDIR\n")
                    """),
            )
            r_temporary = Path((temporary / "r-session.txt").read_text().strip())
            assert (r_temporary / "probe").read_text().strip() == "R-owned"
            client.expect(
                "host temporary environment and caches retained\n",
                # fmt: python
                python=code(r"""
                    import os
                    import tempfile
                    from pathlib import Path

                    assert Path(tempfile.gettempdir()).name == "host-temp"
                    assert os.environ["MPLCONFIGDIR"] == os.environ["XDG_CACHE_HOME"]
                    assert Path(os.environ["MPLCONFIGDIR"]).name == "cache"
                    print("host temporary environment and caches retained")
                    """),
            )
            transcript = client.finish()
        assert not r_temporary.exists(), (
            "R session tempdir survived graceful worker shutdown"
        )
        assert (temporary / "keep").read_text() == "caller-owned"
        assert sorted(path.name for path in temporary.iterdir()) == [
            "keep",
            "r-session.txt",
        ]
        return transcript


@requires(SANDBOX, R)
def test_workspace_profile_retains_managed_temp_cleanup(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as directory, TemporaryDirectory() as temporary:
        workspace = Path(directory).resolve()
        environment["TMPDIR"] = temporary
        environment["TEST_HOST_TEMP"] = temporary
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {"sandbox": {"profile": "workspace_write"}, "python": sys.executable}
            )
        )
        with McpClient(binary, ("serve",), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                "workspace writes with private temp storage\n",
                # fmt: python
                python=code(r"""
                    import errno
                    import os
                    from pathlib import Path

                    temporary = Path(os.environ["TMPDIR"])
                    assert str(temporary) != os.environ["TEST_HOST_TEMP"]
                    _ = (temporary / "probe").write_text("owned")
                    _ = Path("worker-temp.txt").write_text(str(temporary))
                    try:
                        _ = (Path(os.environ["TEST_HOST_TEMP"]) / "blocked").write_text("blocked")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        raise AssertionError("host temp directory was writable")
                    print("workspace writes with private temp storage")
                    """),
            )
            owned = Path((workspace / "worker-temp.txt").read_text())
            transcript = client.finish()
        assert not owned.exists()
        assert list(Path(temporary).iterdir()) == []
        return transcript


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_python_only_sql_uses_host_temporary_storage(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as directory:
        workspace = Path(directory).resolve()
        tools = workspace / "tools"
        tools.mkdir()
        expose_uv(tools)
        retain_system_bwrap(tools)
        temporary = workspace / "host-temp"
        temporary.mkdir()
        environment = dict(os.environ, PATH=str(tools))
        for name in (
            "R_HOME",
            "R_LIBS",
            "R_LIBS_USER",
            "RETICULATE_PYTHON",
            "RETICULATE_UV",
        ):
            environment.pop(name, None)
        settings = {
            "python": {"managed": {"packages": ["duckdb"]}},
            "languages": ["python", "sql"],
            "environment": {"TMPDIR": str(temporary)},
        }
        if execution is SANDBOXED:
            settings["sandbox"] = {"tempdir": "read_write"}
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(json.dumps(settings))
        with McpClient(binary, execution.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect(
                "native SQL uses host temporary storage\n",
                # fmt: python
                python=code(r"""
                    import os
                    from pathlib import Path

                    connection = _console.sql_connection()
                    storage = Path(
                        connection.execute("SELECT current_setting('temp_directory')").fetchone()[0]
                    )
                    assert storage.is_relative_to(Path(os.environ["TMPDIR"])), storage
                    assert connection.execute("SELECT 42").fetchone() == (42,)
                    _console.sql_connection(None)
                    print("native SQL uses host temporary storage")
                    """),
            )
            result = client.send(sql="SELECT 42 AS answer")
            assert not result.get("isError"), result
            assert "42" in last_tool_text(client)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
