"""Temporary-storage ownership through the public MCP interface."""

import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text, last_tool_text
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
from boundaries.client_server.sql.test_without_r import (
    environment as python_only_environment,
)


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
        (temporary / "keep").chmod(0o400)
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
                    stopifnot(file.symlink(
                      file.path(parent, "keep"),
                      file.path(tempdir(), "external")
                    ))
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
            client.expect(
                # fmt: r
                r=code(r"""
                    cleanup_probe <- new.env()
                    cleanup_finalizer <- function(x) {
                      writeLines("R finalizer ran", file.path(parent, "r-finalizer.txt"))
                      warning("R finalizer warning")
                    }
                    invisible(reg.finalizer(
                      cleanup_probe,
                      function(x) cleanup_finalizer(x),
                      onexit = TRUE
                    ))
                    """),
            )
            client.expect(
                "Warning message:\nIn cleanup_finalizer(x) : R finalizer warning\n[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                control="restart",
            )
            assert (
                temporary / "r-finalizer.txt"
            ).read_text().strip() == "R finalizer ran"
            assert not r_temporary.exists(), "R session tempdir survived restart"
            assert (temporary / "keep").stat().st_mode & 0o777 == 0o400
            client.expect(
                "R shutdown tempdir recorded\n",
                # fmt: r
                r=code(r"""
                    writeLines(tempdir(), file.path(Sys.getenv("TMPDIR"), "r-session.txt"))
                    cat("R shutdown tempdir recorded\n")
                    """),
            )
            r_temporary = Path((temporary / "r-session.txt").read_text().strip())
            transcript = client.finish()
        assert not r_temporary.exists(), (
            "R session tempdir survived graceful worker shutdown"
        )
        assert (temporary / "keep").read_text() == "caller-owned"
        assert (temporary / "keep").stat().st_mode & 0o777 == 0o400
        assert sorted(path.name for path in temporary.iterdir()) == [
            "keep",
            "r-finalizer.txt",
            "r-session.txt",
        ]
        return transcript


@requires(POSIX, SANDBOX, R)
def test_workspace_profile_retains_managed_temp_cleanup(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as directory, TemporaryDirectory() as temporary:
        workspace = Path(directory).resolve()
        environment["TMPDIR"] = temporary
        environment["TEST_HOST_TEMP"] = temporary
        caller_owned = Path(temporary) / "keep"
        caller_owned.write_text("caller-owned")
        caller_owned.chmod(0o400)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps({"sandbox": {"profile": "workspace"}, "python": sys.executable})
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
                    locked = temporary / "locked"
                    locked.mkdir()
                    _ = (locked / "probe").write_text("owned")
                    locked.chmod(0o500)
                    (temporary / "external").symlink_to(Path(os.environ["TEST_HOST_TEMP"]) / "keep")
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
        assert caller_owned.read_text() == "caller-owned"
        assert caller_owned.stat().st_mode & 0o777 == 0o400
        # Preparation may use the caller-owned parent independently. Verify
        # the worker's attempted host write and its actual private root.
        assert not (Path(temporary) / "blocked").exists()
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
        (temporary / "keep").write_text("caller-owned")
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
                    secrets = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    )
                    secrets.mkdir(parents=True, exist_ok=True)
                    assert secrets.parent.stat().st_mode & 0o777 == 0o700
                    _ = (secrets / "credential").write_text("test credential")
                    _ = Path(os.environ["TMPDIR"], "sql-storage.txt").write_text(str(secrets.parent))
                    _console.sql_connection(None)
                    print("native SQL uses host temporary storage")
                    """),
            )
            result = client.send(sql="SELECT 42 AS answer")
            assert not result.get("isError"), result
            assert "42" in last_tool_text(client)
            first_storage = Path((temporary / "sql-storage.txt").read_text())
            assert (first_storage / "stored-secrets/credential").is_file()
            client.send(control="restart")
            assert not first_storage.exists(), "retired SQL credential storage remains"
            client.expect(
                "SQL storage recorded\n",
                # fmt: python
                python=code(r"""
                    import os
                    from pathlib import Path

                    connection = _console.sql_connection()
                    secrets = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    )
                    secrets.mkdir(parents=True, exist_ok=True)
                    _ = (secrets / "credential").write_text("test credential")
                    _ = Path(os.environ["TMPDIR"], "sql-storage.txt").write_text(str(secrets.parent))
                    print("SQL storage recorded")
                    """),
            )
            crashed_storage = Path((temporary / "sql-storage.txt").read_text())
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            assert not crashed_storage.exists(), (
                "crashed SQL credential storage remains"
            )
            client.expect(
                "SQL shutdown storage recorded\n",
                # fmt: python
                python=code(r"""
                    import os
                    from pathlib import Path

                    connection = _console.sql_connection()
                    storage = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    ).parent
                    (storage / "stored-secrets").mkdir(parents=True, exist_ok=True)
                    _ = (storage / "stored-secrets/credential").write_text("test credential")
                    _ = Path(os.environ["TMPDIR"], "sql-storage.txt").write_text(str(storage))
                    print("SQL shutdown storage recorded")
                    """),
            )
            shutdown_storage = Path((temporary / "sql-storage.txt").read_text())
            transcript = client.finish()
        assert not shutdown_storage.exists(), "shutdown SQL storage remains"
        assert (temporary / "keep").read_text() == "caller-owned"
        return transcript


@requires(POSIX, command("uv"))
@executions(SANDBOXED)
def test_read_write_sql_uses_platform_temp_without_inheritance(
    binary: Path, execution: Execution
) -> Transcript:
    # Keep the server's inherited temp location outside the automatic /tmp grant.
    with TemporaryDirectory(dir="/var/tmp") as directory:
        workspace = Path(directory).resolve()
        tools = workspace / "tools"
        tools.mkdir()
        expose_uv(tools)
        retain_system_bwrap(tools)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "inherit_environment": False,
                    "resolver": {"inherit_environment": True},
                    "sandbox": {"tempdir": "read_write"},
                    "python": {"managed": {"packages": ["duckdb"]}},
                    "languages": ["python", "sql"],
                }
            )
        )
        with McpClient(
            binary, execution.serve(), python_only_environment(tools), workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "SQL uses platform temp without inherited environment\n",
                # fmt: python
                python=code(r"""
                    import os
                    from pathlib import Path

                    assert not any(name in os.environ for name in ("TMPDIR", "TEMP", "TMP"))
                    connection = _console.sql_connection()
                    storage = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    ).parent
                    assert storage.parent.samefile("/tmp"), storage
                    _ = (storage / "probe").write_text("owned")
                    print("SQL uses platform temp without inherited environment")
                    """),
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
