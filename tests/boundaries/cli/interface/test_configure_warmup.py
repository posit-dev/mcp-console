"""Registration starts one detached warmup without retaining setup's streams."""

import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli.interface.test_configure_client import environment
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.events import Events
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires
from support.suites import run_this_suite


def configure(
    binary: Path,
    root: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "configure-client", *arguments],
        cwd=root,
        env=environment(root)
        | dict.fromkeys(
            ("R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"),
            os.devnull,
        )
        | (extra_environment or {}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
    )


def test_no_warmup_and_dry_run_do_not_start_a_job(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        for flag in ("--no-warmup", "--dry-run"):
            workspace = root / flag.removeprefix("--")
            workspace.mkdir()
            result = configure(binary, workspace, "claude", flag)
            assert result.returncode == 0 and not result.stderr, result
            assert not (workspace / "console-home").exists(), result
            assert "Background warmup started" not in result.stdout, result
            if flag == "--dry-run":
                assert "Would start background warmup.\n" in result.stdout, result
                assert not (workspace / "claude-home").exists()
            else:
                assert "Would start background warmup" not in result.stdout, result
                assert (workspace / "claude-home/.claude.json").is_file()
            records.append({"option": flag, "warmup_started": False})
    return records


def test_failed_registration_does_not_start_a_job(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        configuration = root / "claude-home/.claude.json"
        configuration.parent.mkdir()
        configuration.write_text("not JSON", encoding="utf-8")
        result = configure(binary, root, "codex", "claude")
        assert result.returncode == 1 and not result.stdout, result
        assert result.stderr == (
            f"Cannot configure {configuration}: expected ident at line 1 column 2\n"
        ), result
        assert configuration.read_text() == "not JSON"
        assert not (root / "codex-home").exists()
        assert not (root / "console-home").exists()
        return [
            {
                "exit_code": result.returncode,
                "stderr": result.stderr.replace(
                    str(configuration), "<workspace>/claude-home/.claude.json"
                ),
                "warmup_started": False,
            }
        ]


@requires(POSIX)
def test_starts_one_detached_warmup_with_the_selected_launch(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        output = project / "output with spaces"
        output.mkdir()
        commands = root / "commands"
        commands.mkdir()
        invocation = root / "invocation.json"
        script = root / "uvx.py"
        script.write_text(
            # fmt: python
            code("""
                import json
                import os
                import sys
                from pathlib import Path

                record = Path(os.environ["TEST_WARMUP_INVOCATION"])
                temporary = record.with_suffix(".partial")
                temporary.write_text(
                    json.dumps(
                        {
                            "arguments": sys.argv[1:],
                            "directory": os.getcwd(),
                            "session": os.getsid(0),
                            "request": json.loads(sys.stdin.readline())["method"],
                        }
                    )
                )
                temporary.replace(record)
                print("warmup stderr", file=sys.stderr, flush=True)
                with open(os.environ["TEST_WARMUP_RELEASE"], "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                print("warmup fixture failure", file=sys.stderr, flush=True)
                raise SystemExit(7)
                """),
            encoding="utf-8",
        )
        wrapper = commands / "uvx"
        wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        release = FifoCheckpoint.create(root / "release.fifo")
        pid = None
        with Events() as events:
            try:
                result = configure(
                    binary,
                    root,
                    "codex",
                    "claude",
                    "codex",
                    "--pin-version",
                    "9.8.7",
                    "--directory",
                    str(project),
                    "--",
                    "--no-project-config",
                    "--writable-root",
                    str(output),
                    extra_environment={
                        "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                        "TEST_WARMUP_INVOCATION": str(invocation),
                        "TEST_WARMUP_RELEASE": str(release.path),
                    },
                )
                assert result.returncode == 0 and not result.stderr, result
                matches = re.findall(
                    r"^Background warmup started \(PID (\d+)\)\. Log: (.+)$",
                    result.stdout,
                    re.MULTILINE,
                )
                assert len(matches) == 1, result
                pid = int(matches[0][0])
                events.watch_process(pid)
                wait_for_path(invocation, "detached warmup reached its gate")
                observed = json.loads(invocation.read_text())
                expected_arguments = [
                    "mcp-console@9.8.7",
                    "serve",
                    "--default-config",
                    "sandbox.profile=inherit_codex",
                    "--no-project-config",
                    "--writable-root",
                    str(output),
                ]
                assert observed == {
                    "arguments": expected_arguments,
                    "directory": str(project),
                    "session": pid,
                    "request": "initialize",
                }, observed
                log = Path(matches[0][1])
                assert log.parent == root / "console-home/logs", log
                assert log.name.startswith("warmup-") and log.suffix == ".log", log
                assert (project / ".codex/config.toml").is_file()
                assert (project / ".mcp.json").is_file()
                # Captured setup streams have reached EOF while warmup is still gated.
                os.kill(pid, 0)
            finally:
                release.release()
                if pid is not None:
                    assert pid in events.wait(10), "warmup fixture did not exit"
                release.close()
        assert log.read_text() == (
            "MCP Console warmup\n"
            "warmup stderr\n"
            "warmup fixture failure\n"
            "Server: FAIL\nconnection closed: initialize response\n"
            "Shutdown: FAIL\nserver exited with status 7\n"
            "Warmup found 2 failed checks.\n"
        )
        observed["arguments"][-1] = "<output>"
        observed["directory"] = "<project>"
        observed["session"] = "<warmup-pid>"
        return [
            {
                "fixture": "uvx records its launch, waits at a gate, then exits with status 7",
                "configure_exit_code": result.returncode,
                **observed,
                "log": log.read_text(),
            }
        ]


@requires(POSIX, R, SQL)
def test_pinned_serve_only_release_warms_runtimes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        invocation = root / "invocation.json"
        script = root / "uvx.py"
        script.write_text(
            # fmt: python
            code("""
                import json
                import os
                import sys
                from pathlib import Path

                package, command, *arguments = sys.argv[1:]
                assert package == "mcp-console@0.0.1", package
                if command != "serve":
                    raise SystemExit(f"this release supports serve, not {command}")
                Path(os.environ["TEST_WARMUP_INVOCATION"]).write_text(json.dumps(sys.argv[1:]))
                binary = os.environ["TEST_WARMUP_BINARY"]
                os.execv(binary, [binary, command, *arguments])
                """),
            encoding="utf-8",
        )
        commands = root / "commands"
        commands.mkdir()
        wrapper = commands / "uvx"
        wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.join([sys.executable, str(script)]) + ' "$@"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        result = configure(
            binary,
            root,
            "claude",
            "--pin-version",
            "0.0.1",
            "--",
            "--no-config",
            extra_environment={
                "PATH": str(commands) + os.pathsep + os.environ["PATH"],
                "TEST_WARMUP_BINARY": str(binary),
                "TEST_WARMUP_INVOCATION": str(invocation),
            },
        )
        assert result.returncode == 0 and not result.stderr, result
        match = re.search(
            r"^Background warmup started \(PID (\d+)\)\. Log: (.+)$",
            result.stdout,
            re.MULTILINE,
        )
        assert match is not None, result
        pid = int(match[1])
        log = Path(match[2])
        with Events() as events:
            try:
                events.watch_process(pid)
            except ProcessLookupError:
                pass
            else:
                assert pid in events.wait(60), log.read_text()
        assert log.read_text() == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "Checking R...\nR: OK\n"
            "Checking Python...\nPython: OK\n"
            "Checking SQL...\nSQL: OK\n"
            "Shutdown: OK\n"
            "All checks passed. Runtime caches are warm.\n"
        ), log.read_text()
        arguments = json.loads(invocation.read_text())
        assert arguments == ["mcp-console@0.0.1", "serve", "--no-config"]
        return [{"arguments": arguments, "log": log.read_text()}]


@requires(POSIX, R, SQL)
def test_direct_launch_warms_real_runtimes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = configure(binary, root, "claude", "--no-uvx", "--", "--no-config")
        assert result.returncode == 0 and not result.stderr, result
        match = re.search(
            r"^Background warmup started \(PID (\d+)\)\. Log: (.+)$",
            result.stdout,
            re.MULTILINE,
        )
        assert match is not None, result
        pid = int(match[1])
        log = Path(match[2])
        with Events() as events:
            try:
                events.watch_process(pid)
            except ProcessLookupError:
                # Fast warmup may finish before setup's completed output is read.
                pass
            else:
                assert pid in events.wait(60), log.read_text()
        assert log.read_text() == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "Checking R...\nR: OK\n"
            "Checking Python...\nPython: OK\n"
            "Checking SQL...\nSQL: OK\n"
            "Shutdown: OK\n"
            "All checks passed. Runtime caches are warm.\n"
        )
        return [{"configure_exit_code": result.returncode, "log": log.read_text()}]


@requires(POSIX)
def test_background_warmup_uses_custom_worker_and_relay(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        cells = root / "cells.jsonl"
        result = configure(
            binary,
            root,
            "claude",
            "--no-uvx",
            "--",
            "--no-config",
            "--no-sandbox",
            "-c",
            "languages=[python]",
            "--worker",
            str(fixtures / "zod"),
            "--relay",
            str(fixtures / "identified_relay"),
            extra_environment={
                "TMPDIR": str(root),
                "MCP_CONSOLE_TEST_BINARY": str(binary),
                "MCP_CONSOLE_TEST_PYTHON": sys.executable,
                "MCP_CONSOLE_TEST_ZOD_PYTHON_CELLS": str(cells),
            },
        )
        assert result.returncode == 0 and not result.stderr, result
        match = re.search(
            r"^Background warmup started \(PID (\d+)\)\. Log: (.+)$",
            result.stdout,
            re.MULTILINE,
        )
        assert match is not None, result
        pid = int(match[1])
        log = Path(match[2])
        with Events() as events:
            try:
                events.watch_process(pid)
            except ProcessLookupError:
                pass
            else:
                assert pid in events.wait(10), log.read_text()
        assert log.read_text() == (
            "MCP Console warmup\n"
            "Server: OK\n"
            "R: SKIP (disabled)\n"
            "Checking Python...\nPython: OK\n"
            "SQL: SKIP (disabled)\n"
            "Shutdown: OK\n"
            "All checks passed. Runtime caches are warm.\n"
        ), log.read_text()
        assert int((root / "zod-relay-pid").read_text()) > 0
        assert [json.loads(line) for line in cells.read_text().splitlines()] == [
            {
                "kind": "evaluate",
                "language": "python",
                "source": "assert sum((1, 1)) == 2\nprint('mcp-console warmup: Python OK')",
            }
        ]
        return [{"configure_exit_code": result.returncode, "log": log.read_text()}]


if __name__ == "__main__":
    run_this_suite(__file__)
