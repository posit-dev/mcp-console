"""Warmup exercises the configured server through its public MCP interface."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.checkpoints import FifoCheckpoint
from support.processes import (
    capture_process_identity,
    host_process_id,
    live_processes,
)
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires
from support.suites import run_this_suite
from boundaries.client_server.server.test_no_r import no_r_environment


def warmup(
    binary: Path,
    workspace: Path,
    execution: Execution,
    *arguments: str,
    root_arguments: tuple[str, ...] = (),
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *root_arguments, "warmup", *execution.serve()[1:], *arguments],
        cwd=workspace,
        env=(os.environ if environment is None else environment)
        | {"MCP_CONSOLE_HOME": str(workspace / "console-home"), "NO_COLOR": "1"}
        | dict.fromkeys(
            ("R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"),
            os.devnull,
        ),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=300,
    )


@requires(R, SQL)
@executions(RUNTIME)
def test_checks_all_languages(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        result = warmup(binary, Path(temporary), execution)
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "Checking R...\nR: OK\n"
            "Checking Python...\nPython: OK\n"
            "Checking SQL...\nSQL: OK\n"
            "Shutdown: OK\n"
            "All checks passed. Runtime caches are warm.\n"
        ), result
        return [{"stdout": result.stdout}]


@executions(DIRECT, SANDBOXED)
def test_respects_configuration_and_disabled_languages(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / "session.json"
        config.write_text(
            json.dumps(
                {
                    "languages": ["r", "python"],
                    "tool": {"parameters": {"r": {"enabled": False}}},
                    "startup": {
                        "language": "python",
                        # fmt: python
                        "code": code("""
                            import os
                            import sqlite3

                            assert os.environ["DOCTOR_LAYER"] == "last"
                            _console.sql_connection(sqlite3.connect(":memory:"))
                            """),
                    },
                }
            ),
            encoding="utf-8",
        )
        # Root and command overrides retain their order across the child launch.
        result = warmup(
            binary,
            workspace,
            execution,
            "-c",
            "environment.DOCTOR_LAYER=last",
            root_arguments=(
                "--config-file",
                str(config),
                "-c",
                "environment.DOCTOR_LAYER=first",
            ),
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "R: SKIP (disabled)\n"
            "Checking Python...\nPython: OK\n"
            "SQL: SKIP (disabled)\n"
            "Shutdown: OK\n"
            "All checks passed. Runtime caches are warm.\n"
        ), result
        return [{"stdout": result.stdout}]


@requires(R, SQL)
@executions(RUNTIME)
def test_reports_runtime_failure_and_finishes_other_checks(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / "session.json"
        config.write_text(
            json.dumps(
                {
                    "startup": {
                        "language": "python",
                        "code": "raise RuntimeError('warmup fixture startup failure')",
                    }
                }
            ),
            encoding="utf-8",
        )
        result = warmup(binary, workspace, execution, "--config-file", str(config))
        assert result.returncode == 1 and result.stderr == "", result
        assert "R: OK\n" in result.stdout, result
        assert "Python: OK\n" in result.stdout, result
        assert "SQL: FAIL\n" in result.stdout, result
        assert "warmup fixture startup failure" in result.stdout, result
        assert "Shutdown: OK\n" in result.stdout, result
        assert result.stdout.endswith("Warmup found 1 failed check.\n"), result
        assert "Runtime caches are warm" not in result.stdout, result
        return [{"exit_code": result.returncode, "stdout": result.stdout}]


@executions(RUNTIME)
def test_reports_configuration_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        result = warmup(
            binary, Path(temporary), execution, "--no-config", "-c", "unknown=true"
        )
        assert result.returncode == 1, result
        assert "unknown" in result.stderr, result
        assert "Server: FAIL\n" in result.stdout, result
        assert result.stdout.endswith(
            "Shutdown: FAIL\n"
            "server exited with status 1\n"
            "Warmup found 2 failed checks.\n"
        ), result
        assert "All checks passed" not in result.stdout, result
        return [
            {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        ]


@requires(POSIX, SQL)
@executions(RUNTIME)
def test_reports_missing_r_and_checks_python_and_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        result = warmup(
            binary, workspace, execution, environment=no_r_environment(workspace)
        )
        assert result.returncode == 1 and result.stderr == "", result
        assert "R: FAIL\n" in result.stdout, result
        assert "Python: OK\n" in result.stdout, result
        assert "SQL: OK\n" in result.stdout, result
        assert result.stdout.endswith("Warmup found 1 failed check.\n"), result
        return [{"exit_code": result.returncode, "stdout": result.stdout}]


@requires(POSIX)
@executions(SANDBOXED)
def test_timeout_retires_the_worker(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        pid_file = workspace / "worker.pid"
        started = workspace / "started"
        config = workspace / "session.json"
        config.write_text(
            json.dumps(
                {
                    "languages": ["python"],
                    "python": sys.executable,
                    "environment": {
                        "DOCTOR_PID_FILE": str(pid_file),
                        "WARMUP_STARTED": str(started),
                    },
                    "startup": {
                        "language": "python",
                        # fmt: python
                        "code": code("""
                            import os
                            import threading

                            with open(os.environ["DOCTOR_PID_FILE"], "w") as stream:
                                stream.write(str(os.getpid()))
                            with open(os.environ["WARMUP_STARTED"], "w") as stream:
                                stream.write("1")
                            threading.Event().wait()
                            """),
                    },
                }
            ),
            encoding="utf-8",
        )
        with (
            closing(FifoCheckpoint.create(started)) as checkpoint,
            ThreadPoolExecutor(max_workers=1) as commands,
        ):
            running = commands.submit(
                warmup,
                binary,
                workspace,
                execution,
                "--config-file",
                str(config),
                "--writable-root",
                str(workspace),
                "--timeout",
                "5",
                environment=no_r_environment(workspace),
            )
            checkpoint.wait("startup must reach the blocking checkpoint")
            # Resolve the namespace PID while the worker is still alive, then
            # retain its host identity so exit cannot be confused with PID reuse.
            worker = capture_process_identity(
                host_process_id(int(pid_file.read_text()), os.getpid())
            )
            result = running.result()
        assert result.returncode == 1 and result.stderr == "", result
        assert live_processes([worker]) == [], f"warmup left worker {worker} running"
        assert result.stdout == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "R: SKIP (disabled)\n"
            "Checking Python...\nPython: FAIL\n"
            "check timed out; stopping the server\n"
            "Shutdown: OK\n"
            "Warmup found 1 failed check.\n"
        ), result
        return [{"exit_code": result.returncode, "stdout": result.stdout}]


@requires(POSIX)
@executions(RUNTIME)
def test_reports_interactive_input(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        config = workspace / "session.json"
        config.write_text(
            json.dumps(
                {
                    "languages": ["python"],
                    "python": sys.executable,
                    "startup": {
                        "language": "python",
                        "code": "input('warmup fixture input: ')",
                    },
                }
            ),
            encoding="utf-8",
        )
        result = warmup(
            binary,
            workspace,
            execution,
            "--config-file",
            str(config),
            "--timeout",
            "10",
            environment=no_r_environment(workspace),
        )
        assert result.returncode == 1 and result.stderr == "", result
        assert "Python: FAIL\n" in result.stdout, result
        assert "warmup fixture input: " in result.stdout, result
        assert "[waiting for stdin]" in result.stdout, result
        assert "check requested interactive input\n" in result.stdout, result
        assert result.stdout.endswith("Shutdown: OK\nWarmup found 1 failed check.\n"), (
            result
        )
        return [{"exit_code": result.returncode, "stdout": result.stdout}]


if __name__ == "__main__":
    run_this_suite(__file__)
