#!/usr/bin/env -S uv run --script

import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, requires
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.linux_sandbox import retain_system_bwrap
from boundaries.client_server.server.test_no_r import no_r_environment
from support.resolvers import bare_runtime_environment
from support.suites import run_this_suite
from support.snapshots import platform_snapshots


def rejected_selection(binary: Path, root: Path, environment: dict[str, str]) -> list:
    with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
        tool_error = client.startup_error()
        assert "R_HOME" in tool_error and str(root / "missing-r") in tool_error, (
            tool_error
        )
        client.stdin.close()
        assert client.stdout.read(timeout=20) == ""
        error = client.stderr.read(timeout=20)
        assert "R_HOME" in error and str(root / "missing-r") in error, error
        assert "setup or retirement deadline exceeded" not in error, error
        assert client.process.wait(timeout=5) != 0
        return [{"stderr": error.replace(str(root), "<workspace>")}]


@platform_snapshots(
    "win32", reason="Invalid R_HOME reports the native Rscript installation layout"
)
def test_rejects_invalid_local_r_home(binary: Path) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = {**os.environ, "R_HOME": str(root / "missing-r")}
        return rejected_selection(binary, root, environment)


@requires(POSIX)
@requires(R)
@executions(DIRECT, SANDBOXED)
def test_retains_discovered_r_home_across_generations(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        selected = environment.pop("R_HOME")
        environment = bare_runtime_environment(environment, root / "r-library")
        commands = root / "bin"
        commands.mkdir()
        retain_system_bwrap(commands)
        # R's normal session-tempdir cleanup invokes rm through the shell.
        for name in ("sh", "rm"):
            (commands / name).symlink_to(shutil.which(name))
        r = commands / "R"
        r.write_text(
            code(f"""
                #!/bin/sh
                printf '%s\\n' {shlex.quote(selected)}
                """)
        )
        r.chmod(0o755)
        environment["PATH"] = str(commands)
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            # MCP readiness precedes runtime discovery; wait for the captured selection.
            client.send(requirements={"action": "get"})
            r.write_text(
                code("""
                    #!/bin/sh
                    exit 91
                    """)
            )
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.send(r="answer <- 41L; answer + 1L")
                assert last_result_text(client) == "[1] 42\n", last_result_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retains_r_absence_across_generations(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        commands = root / "bin"
        commands.mkdir()
        retain_system_bwrap(commands)
        marker = root / "discovered-r"
        environment = bare_runtime_environment(os.environ.copy(), root / "r-library")
        environment.pop("R_HOME", None)
        environment.update(PATH=str(commands), RETICULATE_PYTHON=sys.executable)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            # MCP readiness precedes runtime discovery; wait for the captured selection.
            client.send(requirements={"action": "get"})
            r = commands / "R"
            r.write_text(
                code(f"""
                    #!/bin/sh
                    printf called > {shlex.quote(str(marker))}
                    exit 91
                    """)
            )
            r.chmod(0o755)
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.send(python="answer = 41; answer + 1")
                assert last_result_text(client) == "42\n", last_result_text(client)
                client.send(r="1 + 1")
                assert (
                    last_result_text(client)
                    == "R cells are unavailable in Python sessions without R"
                ), client.transcript[-1]
                assert not marker.exists(), "worker rediscovered R after server startup"
            return client.finish()


@executions(SANDBOXED)
def test_removes_managed_sql_storage_on_restart_and_shutdown(
    binary: Path, execution: Execution
) -> list:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with McpClient(
            binary, execution.serve(), no_r_environment(root), root
        ) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                    assert not storage.exists(), "SQL storage survived restart"
                # fmt: python
                python = code("""
                    import json
                    from pathlib import Path

                    connection = _console.sql_connection()
                    storage = Path(
                        connection.execute("SELECT current_setting('temp_directory')").fetchone()[0]
                    ).parent
                    storage.mkdir(parents=True, exist_ok=True)
                    (storage / "cleanup-marker").write_text("owned storage")
                    print(json.dumps(str(storage)))
                    """)
                client.send(python=python)
                assert client.transcript[-1]["result"]["isError"] is False, (
                    client.transcript[-1]
                )
                storage = Path(json.loads(last_result_text(client)))
                assert storage.is_dir()
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    '"<SQL storage>"\n'
                )
            transcript = client.finish()
        assert not storage.exists(), "SQL storage survived shutdown"
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
