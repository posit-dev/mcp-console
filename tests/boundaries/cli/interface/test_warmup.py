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
from support.client import McpClient
from support.checkpoints import FifoCheckpoint
from support.processes import (
    capture_process_identity,
    host_process_id,
    live_processes,
)
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    R,
    SANDBOX,
    SQL,
    command,
    requires,
)
from support.snapshots import platform_snapshots
from support.suites import run_this_suite
from boundaries.client_server.requirements.test_cache_locations import CACHE_VARIABLES
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


def trusted_warmup_environment(workspace: Path) -> dict[str, str]:
    environment = no_r_environment(workspace)
    uv = workspace / "commands/uv"
    real_uv = uv.resolve()
    uv.unlink()
    variables = (*CACHE_VARIABLES, "XDG_CACHE_HOME", "MPLCONFIGDIR")
    uv.write_text(
        f"#!{sys.executable}\n"
        # fmt: python
        + code(f"""
            import errno
            import json
            import os
            import sys
            from pathlib import Path

            assert "MCP_CONSOLE_TRUSTED_WARMUP" not in os.environ
            root = Path(os.environ["WARMUP_TEST_CACHE"]) / "dependencies"
            caches = {{name: os.environ[name] for name in {variables!r}}}
            assert all(Path(path).is_relative_to(root) for path in caches.values()), caches
            marker = Path(os.environ["WARMUP_TEST_MARKER"])
            if os.environ["WARMUP_TEST_HOST"] == "1":
                marker.write_text("trusted preparation")
            else:
                try:
                    marker.write_text("unexpected resolver write")
                except OSError as error:
                    assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
                else:
                    raise AssertionError("normal resolver wrote outside its cache grants")
            Path(os.environ["UV_CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
            (Path(os.environ["UV_CACHE_DIR"]) / "warmup-environment.json").write_text(json.dumps(caches))
            os.execv({str(real_uv)!r}, [{str(real_uv)!r}, *sys.argv[1:]])
            """),
        encoding="utf-8",
    )
    uv.chmod(0o755)
    environment.update(
        MCP_CONSOLE_CACHE_DIR=str(workspace / "cache"),
        WARMUP_TEST_CACHE=str(workspace / "cache"),
        WARMUP_TEST_MARKER=str(workspace / "host-write"),
        WARMUP_TEST_HOST="1",
        UV_NO_CONFIG="1",
    )
    config = workspace / ".agents/console/config.yaml"
    config.parent.mkdir(parents=True)
    config.write_text(
        json.dumps(
            {
                "languages": ["python"],
                "resolver": {"sandbox": {"network": "restricted"}},
                "startup": {
                    "language": "python",
                    # fmt: python
                    "code": code("""
                        import errno
                        import os
                        import sqlite3
                        from pathlib import Path

                        assert "MCP_CONSOLE_TRUSTED_WARMUP" not in os.environ
                        try:
                            Path(os.environ["WARMUP_TEST_MARKER"]).write_text("unexpected worker write")
                        except OSError as error:
                            assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS), error
                        else:
                            raise AssertionError("warmup worker wrote outside its grants")
                        _console.sql_connection(sqlite3.connect(":memory:"))
                        """),
                },
            }
        ),
        encoding="utf-8",
    )
    return environment


@requires(POSIX, SANDBOX)
def test_trusted_preparation_warms_sandbox_caches(
    binary: Path,
) -> TranscriptWithCompanions:
    # A host-home fixture stays outside macOS's writable temporary directories.
    with TemporaryDirectory(prefix="console-warmup-", dir=Path.home()) as temporary:
        workspace = Path(temporary).resolve()
        environment = trusted_warmup_environment(workspace)
        result = warmup(binary, workspace, SANDBOXED, environment=environment)
        assert result.returncode == 0 and result.stderr == "", result
        assert "Python: OK\n" in result.stdout, result
        marker = workspace / "host-write"
        assert marker.read_text() == "trusted preparation"
        cache = workspace / "cache/dependencies"
        before = json.loads((cache / "uv/cache/warmup-environment.json").read_text())
        # The same settings now work offline under ordinary resolver enforcement.
        environment.update(WARMUP_TEST_HOST="0")
        with McpClient(binary, SANDBOXED.serve(), environment, workspace) as client:
            client.initialize_and_list_tools()
            client.expect("42\n", python="6 * 7")
            transcript = client.finish()
        assert marker.read_text() == "trusted preparation"
        after = json.loads((cache / "uv/cache/warmup-environment.json").read_text())
        assert after == before, (before, after)
        return TranscriptWithCompanions(
            [{"stdout": result.stdout, "cache_environment_reused_offline": True}],
            {"sandboxed-session.yaml": McpTranscript(transcript)},
        )


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
                    "tool": {
                        "parameters": {
                            "r": {"enabled": False},
                            "session": {"enabled": False},
                        }
                    },
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
    return configuration_failure(binary, execution)


@requires(NATIVE_FIXTURES)
@executions(RUNTIME)
def test_configuration_failure_before_initialize_write(
    binary: Path, execution: Execution
) -> Transcript:
    return configuration_failure(binary, execution, handshake="closed")


@requires(NATIVE_FIXTURES)
@executions(RUNTIME)
def test_configuration_failure_after_initialize_write(
    binary: Path, execution: Execution
) -> Transcript:
    return configuration_failure(binary, execution, handshake="written")


def configuration_failure(
    binary: Path, execution: Execution, *, handshake: str | None = None
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = os.environ.copy()
        if handshake is not None:
            os.mkfifo(workspace / "attempt")
            os.mkfifo(workspace / "lifetime")
            environment.update(
                MCP_CONSOLE_TEST_WARMUP_HANDSHAKE=handshake,
                MCP_CONSOLE_TEST_WARMUP_HANDSHAKE_REACHED=str(workspace / "handshake"),
                MCP_CONSOLE_TEST_WARMUP_ATTEMPT=str(workspace / "attempt"),
                MCP_CONSOLE_TEST_WARMUP_LIFETIME=str(workspace / "lifetime"),
            )
            environment[LOADER_VARIABLE] = str(
                build_interposer(workspace, "warmup_handshake")
            )
        result = warmup(
            binary,
            workspace,
            execution,
            "--no-config",
            "-c",
            "unknown=true",
            environment=environment,
        )
        assert result.returncode == 1, result
        if handshake is not None:
            assert (workspace / "handshake").read_text() == "1", result
        assert "unknown" in result.stderr, result
        assert result.stdout == (
            "MCP Console warmup\n"
            "Server: FAIL\n"
            "server connection closed before initialization completed\n"
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


@requires(SQL)
@executions(RUNTIME)
def test_reports_missing_r_and_checks_python_and_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        result = warmup(
            binary, workspace, execution, environment=no_r_environment(workspace)
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert "R: SKIP (not installed)\n" in result.stdout, result
        assert "Python: OK\n" in result.stdout, result
        assert "SQL: OK\n" in result.stdout, result
        assert result.stdout.endswith(
            "All checks passed. Runtime caches are warm.\n"
        ), result
        return [{"exit_code": result.returncode, "stdout": result.stdout}]


@platform_snapshots("win32", reason="native missing R executable diagnostic")
@executions(RUNTIME)
def test_rejects_explicit_missing_r(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        missing = workspace / "missing-R"
        result = warmup(
            binary,
            workspace,
            execution,
            "-c",
            f"r.executable={missing}",
            environment=no_r_environment(workspace),
        )
        assert result.returncode == 1, result
        assert "R: FAIL\n" in result.stdout, result
        assert f"r.executable {missing}" in result.stdout, result
        assert "SKIP (not installed)" not in result.stdout, result
        assert "All checks passed" not in result.stdout, result
        return [
            {
                "exit_code": result.returncode,
                "stdout": result.stdout.replace(str(missing), "<workspace>/missing-R"),
                "stderr": result.stderr.replace(str(missing), "<workspace>/missing-R"),
            }
        ]


@requires(command("rustc"))
@executions(RUNTIME)
def test_rejects_broken_discovered_r(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        environment = no_r_environment(workspace)
        r = workspace / "commands" / ("R.exe" if os.name == "nt" else "R")
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                str(Path(__file__).resolve().parents[3] / "fixtures/r_empty_home.rs"),
                "-o",
                str(r),
            ],
            check=True,
        )
        result = warmup(binary, workspace, execution, environment=environment)
        assert result.returncode == 1, result
        assert "R: FAIL\n" in result.stdout, result
        assert "worker R returned an empty home path" in result.stdout, result
        assert "SKIP (not installed)" not in result.stdout, result
        assert "All checks passed" not in result.stdout, result
        return [
            {
                "exit_code": result.returncode,
                "stdout": result.stdout.replace(str(workspace), "<workspace>"),
                "stderr": result.stderr.replace(str(workspace), "<workspace>"),
            }
        ]


@executions(RUNTIME)
def test_missing_r_does_not_skip_requested_packages(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        result = warmup(
            binary,
            workspace,
            execution,
            "-c",
            "r.packages=[praise]",
            environment=no_r_environment(workspace),
        )
        assert result.returncode == 1, result
        assert "R: FAIL\n" in result.stdout, result
        assert "requested startup packages require R" in result.stdout, result
        assert "SKIP (not installed)" not in result.stdout, result
        assert "All checks passed" not in result.stdout, result
        return [
            {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        ]


@requires(R, SQL)
@executions(RUNTIME)
def test_reports_r_execution_failure(binary: Path, execution: Execution) -> Transcript:
    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        profile = workspace / "Rprofile"
        profile.write_text(
            'stopifnot <- function(...) stop("warmup fixture R failure")\n'
        )
        config = workspace / "session.json"
        config.write_text(json.dumps({"environment": {"R_PROFILE_USER": str(profile)}}))
        result = warmup(binary, workspace, execution, "--config-file", str(config))
        assert result.returncode == 1 and result.stderr == "", result
        assert "R: FAIL\n" in result.stdout, result
        assert "warmup fixture R failure" in result.stdout, result
        assert "Python: OK\n" in result.stdout, result
        assert "SQL: OK\n" in result.stdout, result
        assert "SKIP (not installed)" not in result.stdout, result
        assert result.stdout.endswith("Shutdown: OK\nWarmup found 1 failed check.\n"), (
            result
        )
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
