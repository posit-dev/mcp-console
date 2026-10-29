#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12>=0.2.0", "joblib"]
# ///

from __future__ import annotations

import os
import json
import re
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from pathlib import Path
from unittest.mock import patch

from support.capture import read_lines
from support.events import Events
from support.native import SHARED_LIBRARY_FLAG
from support.normalization import code
from support.processes import (
    capture_process_identity,
    signal_process,
)
from support.requirements import POSIX, PROCESS_EVENTS

ROOT = Path(__file__).resolve().parent.parent
RUNNER = ROOT / "tests" / "boundaries" / "_run.py"

# fmt: python
PUBLIC_SUITE = """
from pathlib import Path


def record(binary: Path, name: str) -> list[dict[str, str]]:
    (binary.parents[2] / f"{name}.marker").touch()
    return [{"runner": name}]


def test_initializes_and_lists_tools(binary: Path) -> list[dict[str, str]]:
    return record(binary, "initialization")


def test_selected(binary: Path) -> list[dict[str, str]]:
    return record(binary, "selected")


def test_unselected(binary: Path) -> list[dict[str, str]]:
    return record(binary, "unselected")
""".lstrip()

# fmt: python
FAILING_SUITE = """
import os
from pathlib import Path


def fail_after_both_start(
    binary: Path,
    release_name: str,
    actual: str,
) -> list[dict[str, str]]:
    root = binary.parents[2]
    started = os.open(root / "started", os.O_WRONLY)
    try:
        assert os.write(started, b"1") == 1
    finally:
        os.close(started)
    release = os.open(root / release_name, os.O_RDONLY)
    try:
        assert os.read(release, 1)
    finally:
        os.close(release)
    return [{"runner": actual}]


def test_initializes_and_lists_tools(binary: Path) -> list[dict[str, str]]:
    return [{"runner": "initialization"}]


def test_first_failure(binary: Path) -> list[dict[str, str]]:
    return fail_after_both_start(binary, "release-first", "first actual")


def test_second_failure(binary: Path) -> list[dict[str, str]]:
    return fail_after_both_start(binary, "release-second", "second actual")
""".lstrip()

# fmt: python
HANGING_SUITE = """
import os
import signal
import subprocess
import sys
from pathlib import Path


def test_hangs(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    child = subprocess.Popen([sys.executable, __file__])
    try:
        with (root / "hang-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
        with (root / "hang-release").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
    finally:
        child.kill()
        child.wait(timeout=5)
        (root / "child-cleaned").touch()
        cleaned = os.open(root / "child-cleanup-complete", os.O_WRONLY | os.O_NONBLOCK)
        os.write(cleaned, b"1")
        os.close(cleaned)
    return [{"runner": "released"}]


def test_failure_beside_hang(binary: Path) -> list[dict[str, str]]:
    with (binary.parents[2] / "failure-release").open("rb", buffering=0) as release:
        assert release.read(1) == b"1"
    return [{"runner": "deliberate mismatch"}]


def test_fails_before_cleanup(binary: Path) -> list[dict[str, str]]:
    try:
        raise AssertionError("original failure before cleanup")
    finally:
        test_hangs(binary)


if __name__ == "__main__":
    signal.pause()
""".lstrip()

# fmt: python
SELF_INTERRUPTING_SUITE = """
import os
import signal
from pathlib import Path


def test_interrupts_itself(binary: Path) -> list[dict[str, str]]:
    os.kill(os.getpid(), signal.SIGINT)
    raise AssertionError("case continued after its own SIGINT")
""".lstrip()

# fmt: python
GIL_HOLDING_SUITE = """
import ctypes
import os
from pathlib import Path


def test_holds_gil(binary: Path) -> list[dict[str, str]]:
    root = binary.parents[2]
    (root / "gil-case-pid").write_text(str(os.getpid()), encoding="utf-8")
    library = ctypes.PyDLL(str(root / "gil-checkpoint.dylib"))
    wait_for_release = library.wait_for_probe_release
    wait_for_release.argtypes = (ctypes.c_int, ctypes.c_int)
    wait_for_release.restype = ctypes.c_int
    with (
        (root / "gil-started").open("wb", buffering=0) as started,
        (root / "gil-release").open("rb", buffering=0) as release,
    ):
        assert wait_for_release(started.fileno(), release.fileno()) == 0
    return [{"runner": "released"}]
""".lstrip()

# fmt: python
FORKING_SUITE = """
import os
import warnings
from pathlib import Path


def test_forks(binary: Path) -> list[dict[str, object]]:
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always", DeprecationWarning)
        child = os.fork()
        if child == 0:
            os._exit(0)
        assert os.waitpid(child, 0) == (child, 0)
    return [{"runner": "forked", "warnings": [str(item.message) for item in recorded]}]
""".lstrip()

# fmt: python
GATED_SNAPSHOT_CHECK = """
ungated_check_recording = check_recording


def check_recording(*arguments: object, **keywords: object) -> object:
    root = Path(__file__).resolve().parents[2]
    try:
        with (root / "snapshot-started").open("wb", buffering=0) as started:
            assert started.write(b"1") == 1
        with (root / "snapshot-release").open("rb", buffering=0) as release:
            assert release.read(1) == b"1"
        return ungated_check_recording(*arguments, **keywords)
    finally:
        (root / "snapshot-check-cleaned").touch()
""".lstrip()


# fmt: python
WORKER_DELIVERED_INTERRUPT = """
import signal
import sys
import threading
from queue import SimpleQueue
from types import FrameType
from typing import Any, TextIO


if "--record-case" not in sys.argv and "--supervise-case" not in sys.argv:

    class FailureReceipt:
        def __init__(self, stream: TextIO) -> None:
            self.stream = stream
            self.reported = False

        def write(self, text: str) -> int:
            result = self.stream.write(text)
            if " differs from its snapshot" in text:
                self.reported = True
            return result

        def __getattr__(self, name: str) -> Any:
            return getattr(self.stream, name)

    receipt = FailureReceipt(sys.stderr)
    sys.stderr = receipt
    interrupt = threading.Event()

    def deliver_interrupt() -> None:
        interrupt.wait()
        signal.pthread_kill(threading.get_ident(), signal.SIGINT)

    # Development and CI use GIL-enabled CPython. Once the gate opens, the
    # worker can acquire the GIL only when get() releases it, delivering the
    # signal on another thread while main is parked.
    sys.setswitchinterval(60)
    threading.Thread(target=deliver_interrupt, daemon=True).start()

    def interrupt_when_parked(_frame: FrameType, event: str, function: Any) -> None:
        if (
            event == "c_call"
            and isinstance(getattr(function, "__self__", None), SimpleQueue)
            and function.__name__ == "get"
            and receipt.reported
            and function.__self__.empty()
        ):
            sys.setprofile(None)
            interrupt.set()

    sys.setprofile(interrupt_when_parked)
""".lstrip()


class TranscriptRunnerFixture(unittest.TestCase):
    """Shared setup only; concrete classes own each test exactly once."""

    def setUp(self) -> None:
        # Copied runners select fixture executables; restore caller overrides
        # after each test, including when setup or an assertion fails.
        self.enterContext(patch.dict(os.environ))
        os.environ.pop("MCP_CONSOLE_TEST_BINARY", None)
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.boundaries = self.root / "tests" / "boundaries"
        self.suite = self.boundaries / "client_server" / "server" / "test_tools.py"
        self.snapshots = (
            self.root
            / "tests"
            / "snapshots"
            / "client_server"
            / "server"
            / "test_tools"
        )
        support = self.root / "tests" / "support"
        binary = self.root / "target" / "release" / "mcp-console"
        for directory in (self.suite.parent, self.snapshots, support, binary.parent):
            directory.mkdir(parents=True, exist_ok=True)

        shutil.copy2(RUNNER, self.boundaries / "_run.py")
        if os.name == "nt":
            scripts = self.root / "scripts"
            scripts.mkdir()
            shutil.copy2(
                ROOT / "scripts/checkout_windows.py", scripts / "checkout_windows.py"
            )
        (self.boundaries / "_profiles.py").write_text(
            # fmt: python
            code("""
                SMOKE = (
                    "client_server/server/test_tools::initializes_and_lists_tools",
                    "client_server/server/test_tools::selected",
                )
                STRESS = ()
                """)
        )
        for name in (
            "__init__.py",
            "cases.py",
            "records.py",
            "snapshots.py",
            "progress.py",
            "requirements.py",
            "linux_sandbox.py",
            "execution.py",
        ):
            shutil.copy2(ROOT / "tests" / "support" / name, support / name)
        self.suite.write_text(PUBLIC_SUITE, encoding="utf-8")
        binary.touch()
        if os.name == "nt":
            os.environ["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        for name in ("initializes_and_lists_tools", "selected", "unselected"):
            value = "initialization" if name == "initializes_and_lists_tools" else name
            (self.snapshots / f"{name}.yaml").write_text(
                f"---\nrunner: {value}\n...\n", encoding="utf-8"
            )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def start_runner(self, *arguments: str) -> subprocess.Popen[str]:
        return subprocess.Popen(
            [sys.executable, self.boundaries / "_run.py", *arguments],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            start_new_session=True,
        )

    def run_runner(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, self.boundaries / "_run.py", *arguments],
            cwd=self.root,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def read_snapshot_failure(
        self, process: subprocess.Popen[str], selector: str
    ) -> str:
        assert process.stderr is not None
        terminator = f"{selector} differs from its snapshot\n".encode()
        output = bytearray()
        deadline = time.monotonic() + 10
        while terminator not in output:
            remaining = deadline - time.monotonic()
            self.assertGreater(
                remaining, 0, "snapshot failure diagnostic was not reported"
            )
            ready, _, _ = select.select([process.stderr], [], [], remaining)
            self.assertTrue(ready, "snapshot failure diagnostic was not reported")
            data = os.read(process.stderr.fileno(), 4096)
            self.assertTrue(data, "runner exited before reporting snapshot failure")
            output.extend(data)
        return output.decode()

    def prepare_script(self) -> dict[str, str]:
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts" / "test", scripts / "test")
        shutil.copy2(
            ROOT / "scripts/checkout_workflow.py", scripts / "checkout_workflow.py"
        )
        commands = self.root / "commands"
        commands.mkdir()
        cargo = commands / "cargo"
        cargo.write_text(
            f"#!{sys.executable}\n"
            # fmt: python
            + code("""
                import sys
                from pathlib import Path

                profile = "release" if "--release" in sys.argv else "debug"
                binary = Path("target") / profile / "mcp-console"
                binary.parent.mkdir(parents=True, exist_ok=True)
                binary.write_text(profile, encoding="utf-8")
                """),
            encoding="utf-8",
        )
        cargo.chmod(0o755)
        return self.script_environment(commands)

    def script_environment(self, commands: Path) -> dict[str, str]:
        # Exercise the bootstrap argv with this test's prepared interpreter.
        # Resolving the copied runner's SDK dependencies would make each temporary
        # checkout depend on registry access inside the command's ten-second limit.
        uv = commands / "uv"
        uv.write_text(
            f"#!{sys.executable}\n"
            # fmt: python
            + code("""
                import os
                import sys

                assert sys.argv[1:3] == ["run", "--script"], sys.argv
                os.execv(sys.executable, [sys.executable, *sys.argv[3:]])
                """),
            encoding="utf-8",
        )
        uv.chmod(0o755)
        return os.environ | {
            "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
            # Isolate the fake build's host concurrency budget.
            "XDG_CACHE_HOME": str(self.root / "cache"),
            # Any accidental real dependency resolution must fail immediately.
            "UV_CACHE_DIR": str(self.root / "cache/uv"),
            "UV_OFFLINE": "1",
        }


@unittest.skipUnless(POSIX.available, POSIX.reason)
class TranscriptRunnerTests(TranscriptRunnerFixture):
    def run_runner(
        self, *arguments: str, timeout: float = 10
    ) -> subprocess.CompletedProcess[str]:
        process = self.start_runner(*arguments)
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"runner did not exit; stdout={stdout!r}; stderr={stderr!r}")
        return subprocess.CompletedProcess(
            arguments, process.returncode, stdout, stderr
        )

    def test_script_builds_and_uses_release_with_a_stale_debug_binary(self) -> None:
        environment = self.prepare_script()
        debug = self.root / "target" / "debug" / "mcp-console"
        debug.parent.mkdir()
        debug.write_text("stale debug", encoding="utf-8")
        (self.root / "target" / "release" / "mcp-console").unlink()
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                def test_selected(binary: Path) -> list[dict[str, str]]:
                    assert binary.read_text(encoding="utf-8") == "release"
                    return record(binary, "selected")
                """),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["scripts/test", "client_server/server/test_tools::selected"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "selected.marker").exists())

    def test_script_uses_installed_binary_without_building(self) -> None:
        environment = self.prepare_script()
        installed = self.root / "installation" / "bin" / "mcp-console"
        installed.parent.mkdir(parents=True)
        installed.write_text("installed", encoding="utf-8")
        (installed.parent.parent / "installation.marker").touch()
        command = self.root / "bin" / "mcp-console"
        command.parent.mkdir()
        command.symlink_to(installed)
        environment["MCP_CONSOLE_TEST_BINARY"] = str(command)
        cargo = self.root / "commands" / "cargo"
        cargo.write_text("#!/bin/sh\nexit 91\n", encoding="utf-8")
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                def test_selected(binary: Path) -> list[dict[str, str]]:
                    assert binary.read_text(encoding="utf-8") == "installed"
                    assert (binary.parent.parent / "installation.marker").is_file()
                    return record(binary, "selected")
                """),
            encoding="utf-8",
        )
        result = subprocess.run(
            ["scripts/test", "client_server/server/test_tools::selected"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "selected.marker").exists())

    def test_cases_isolate_console_home_and_preserve_host_environment(self) -> None:
        ambient_home = self.root / "ambient-home"
        console_home = self.root / "ambient-console"
        project_console = self.root / ".agents/console"
        project_console.mkdir(parents=True)
        (project_console / "config.yaml").write_text("invalid: [", encoding="utf-8")
        config = console_home / "config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("invalid: [", encoding="utf-8")
        commands = self.root / "commands"
        commands.mkdir()
        environment = os.environ | {
            "HOME": str(ambient_home),
            "MCP_CONSOLE_HOME": str(console_home),
            "R_HOME": str(self.root / "missing-R"),
            "PATH": str(commands),
        }
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                import json
                import os


                def test_selected(binary: Path) -> list[dict[str, str]]:
                    root = binary.parents[2]
                    expected = json.loads((root / "environment.json").read_text())
                    actual = {name: os.environ.get(name) for name in expected}
                    assert actual == expected, (actual, expected)
                    console = Path(os.environ["MCP_CONSOLE_HOME"])
                    assert console.is_absolute()
                    assert console != root / "ambient-console"
                    assert not (console / "config.yaml").exists()
                    workspace = Path.cwd()
                    assert workspace != root.resolve(), workspace
                    assert not (workspace / ".agents").exists()
                    (root / "workspace.txt").write_text(str(workspace))
                    return record(binary, "selected")
                """),
            encoding="utf-8",
        )
        names = (
            "UV_CACHE_DIR",
            "UV_PYTHON_INSTALL_DIR",
            "R_LIBS_USER",
            "R_USER_CACHE_DIR",
            "R_USER_DATA_DIR",
            "DOCKER_CONFIG",
            "XDG_CACHE_HOME",
            "XDG_DATA_HOME",
        )
        for explicit in (False, True):
            with self.subTest(explicit=explicit):
                for name in names:
                    if explicit:
                        environment[name] = str(self.root / name)
                    else:
                        environment.pop(name, None)
                expected = {
                    name: environment.get(name) for name in ("HOME", "R_HOME", *names)
                }
                (self.root / "environment.json").write_text(json.dumps(expected))
                result = subprocess.run(
                    [
                        sys.executable,
                        self.boundaries / "_run.py",
                        "client_server/server/test_tools::selected",
                    ],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(
                    Path((self.root / "workspace.txt").read_text()).exists()
                )
        self.assertEqual(config.read_text(), "invalid: [")

    def test_focused_cases_do_not_probe_r(self) -> None:
        environment = self.prepare_script()
        r_home = self.root / "selected-R"
        (r_home / "bin").mkdir(parents=True)
        rscript = (
            f"#!{sys.executable}\n"
            # fmt: python
            + code("""
                from pathlib import Path

                Path(__file__).with_suffix(".probed").touch()
                raise SystemExit(86)
                """)
        )
        for executable in (r_home / "bin/Rscript", self.root / "commands/Rscript"):
            executable.write_text(rscript, encoding="utf-8")
            executable.chmod(0o755)
        for selected_home in (str(r_home), None):
            with self.subTest(selected_home=selected_home):
                environment.pop("R_HOME", None)
                if selected_home is not None:
                    environment["R_HOME"] = selected_home
                result = subprocess.run(
                    ["scripts/test", "client_server/server/test_tools::selected"],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((r_home / "bin/Rscript.probed").exists())
        self.assertFalse((self.root / "commands/Rscript.probed").exists())

    def test_script_creates_empty_timings_when_selected_case_is_skipped(self) -> None:
        environment = self.prepare_script()
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.requirements import Requirement, requires

                test_selected = requires(Requirement("fixture", False, "unavailable"))(test_selected)
                """),
        )
        result = subprocess.run(
            ["scripts/test", "--quick", "client_server/server/test_tools::selected"],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "selected.marker").exists())
        (record_path,) = (self.root / ".dev-workflow/runs").glob("*/result.json")
        record = json.loads(record_path.read_text())
        (phase,) = (p for p in record["phases"] if p["name"] == "transcripts")
        self.assertEqual(phase["exit_status"], 0)
        self.assertEqual(Path(phase["case_timings"]).read_text(), "")

    def test_script_discovers_and_rejects_arguments_without_building(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts/test", scripts / "test")
        shutil.copy2(
            ROOT / "scripts/checkout_workflow.py", scripts / "checkout_workflow.py"
        )
        commands = self.root / "commands"
        commands.mkdir()
        cargo = commands / "cargo"
        cargo.write_text("#!/bin/sh\nexit 99\n")
        cargo.chmod(0o755)
        environment = self.script_environment(commands)
        (self.root / "target/release/mcp-console").unlink()
        suite = "client_server/server/test_tools"
        for arguments, status, expected in (
            (("--help",), 0, "usage: scripts/test"),
            (("--quick", "--full"), 2, "not allowed with argument"),
            (("--list",), 0, f"{suite}::selected"),
            (("--locate", f"{suite}::selected"), 0, "source: tests/boundaries/"),
            (("--execution", "direct"), 2, "execution modes"),
            (("--jobs", "0"), 2, "--jobs must be at least 1"),
            (("--timeout", "nan"), 2, "--timeout must be a positive finite number"),
            (("unknown/suite",), 2, "unknown transcript suite"),
            ((f"{suite}::missing",), 2, "unknown transcript case"),
            (("--locate", suite, "--update"), 2, "cannot be combined"),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [scripts / "test", *arguments],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode, status, result.stdout + result.stderr
                )
                self.assertIn(expected, result.stdout + result.stderr)
                self.assertFalse((self.root / "target/release/mcp-console").exists())
        self.assertFalse((self.root / ".dev-workflow").exists())

    def test_execution_fixture_explains_conflicting_arguments(self) -> None:
        for arguments, expected in (
            (("--no-sandbox",), "execution.serve() selects --no-sandbox"),
            (("--writable-root", "/workspace"), "use SANDBOXED.serve()"),
            (("--writable-root=/workspace",), "use SANDBOXED.serve()"),
        ):
            with self.subTest(arguments=arguments):
                self.suite.write_text(
                    PUBLIC_SUITE
                    # fmt: python
                    + code("""
                        from support.execution import DIRECT, executions


                        @executions(DIRECT)
                        def test_selected(binary, execution):
                            execution.serve(*ARGUMENTS)
                            return record(binary, "selected")
                        """).replace("ARGUMENTS", repr(arguments))
                )
                result = self.run_runner("client_server/server/test_tools::selected")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected, result.stderr)

    def test_help_and_syntax_errors_do_not_resolve_script_dependencies(self) -> None:
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(ROOT / "scripts/test", scripts / "test")
        commands = self.root / "commands"
        commands.mkdir()
        uv = commands / "uv"
        uv.write_text("""#!/bin/sh
echo invoked > uv-receipt
exit 97
""")
        uv.chmod(0o755)
        for arguments, status in (
            (("--help",), 0),
            (("--execution", "direct"), 2),
            (("--jobs", "0"), 2),
            (("--timeout", "nan"), 2),
            (("--locate", "cli/example", "extra"), 2),
        ):
            with self.subTest(arguments=arguments):
                result = subprocess.run(
                    [scripts / "test", *arguments],
                    cwd=self.root,
                    env=os.environ
                    | {"PATH": f"{commands}{os.pathsep}{os.environ['PATH']}"},
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(
                    result.returncode, status, result.stdout + result.stderr
                )
                self.assertFalse((self.root / "uv-receipt").exists())

    def test_case_requirements_and_skip_reporting(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.requirements import Requirement, command, requires

                available = Requirement("available fixture", True, "fixture is available")
                missing = Requirement("missing fixture", False, "fixture deliberately unavailable")
                test_selected = requires(available)(test_selected)
                test_unselected = requires(
                    available, missing, command("mcp-console-deliberately-missing-test-command")
                )(test_unselected)
                """),
            encoding="utf-8",
        )
        listed = self.run_runner("--full", "--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertIn("::unselected", listed.stdout)
        result = self.run_runner("--full", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "selected.marker").exists())
        self.assertFalse((self.root / "unselected.marker").exists())
        self.assertIn(
            "::unselected: skipped; missing fixture: fixture deliberately unavailable",
            result.stdout,
        )
        self.assertIn(
            "mcp-console-deliberately-missing-test-command is missing from PATH",
            result.stdout,
        )
        selected_skip = self.run_runner("client_server/server/test_tools::unselected")
        self.assertEqual(selected_skip.returncode, 0, selected_skip.stderr)
        self.assertIn("fixture deliberately unavailable", selected_skip.stdout)

    def test_r_cases_run_with_home_or_path_selection(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.requirements import R, requires

                test_selected = requires(R)(test_selected)
                """),
            encoding="utf-8",
        )
        commands = self.root / "commands"
        commands.mkdir()
        executable = commands / "R"
        marker = self.root / "selected.marker"
        for home, entry, expected in (
            (None, None, False),
            (str(self.root / "selected-R-home"), None, True),
            ("", None, True),
            (None, "executable", True),
            (None, "non-executable", True),
            (None, "broken symlink", True),
        ):
            with self.subTest(home=home, entry=entry):
                executable.unlink(missing_ok=True)
                if entry == "broken symlink":
                    executable.symlink_to(commands / "missing-R")
                elif entry is not None:
                    executable.write_text("#!/bin/sh\nexit 99\n")
                    executable.chmod(0o755 if entry == "executable" else 0o644)
                environment = os.environ | {"PATH": str(commands)}
                environment.pop("R_HOME", None)
                if home is not None:
                    environment["R_HOME"] = home
                marker.unlink(missing_ok=True)
                result = subprocess.run(
                    [
                        sys.executable,
                        self.boundaries / "_run.py",
                        "client_server/server/test_tools::selected",
                    ],
                    cwd=self.root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(marker.exists(), expected, result.stdout)
                self.assertEqual("skipped; R:" in result.stdout, not expected)

    def test_script_profiles_and_explicit_selectors(self) -> None:
        environment = self.prepare_script()
        environment.update(
            {
                "MCP_CONSOLE_TEST_INPUT": "fixture-input",
                "MCP_CONSOLE_TEST_FIXTURE": "fixture-value",
            }
        )
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                import os

                assert os.environ["MCP_CONSOLE_TEST_INPUT"] == "fixture-input"
                assert os.environ["MCP_CONSOLE_TEST_FIXTURE"] == "fixture-value"
                """),
        )
        for profile in ((), ("--quick",), ("--full",)):
            for selectors in (
                (),
                ("client_server/server/test_tools::unselected",),
                ("client_server/server/test_tools",),
            ):
                with self.subTest(profile=profile, selectors=selectors):
                    expected = (
                        {"unselected"}
                        if selectors and "::" in selectors[0]
                        else {"initialization", "selected", "unselected"}
                        if selectors or profile == ("--full",)
                        else {"initialization", "selected"}
                    )
                    for marker in self.root.glob("*.marker"):
                        marker.unlink()
                    for listing in (True, False):
                        result = subprocess.run(
                            [
                                "scripts/test",
                                *profile,
                                *(["--list"] if listing else []),
                                *selectors,
                            ],
                            cwd=self.root,
                            env=environment,
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)
                        if listing:
                            names = {
                                line.split("::")[1]
                                for line in result.stdout.splitlines()
                            }
                            self.assertEqual(
                                names,
                                {
                                    "initializes_and_lists_tools"
                                    if name == "initialization"
                                    else name
                                    for name in expected
                                },
                            )
                    self.assertEqual(
                        {p.stem for p in self.root.glob("*.marker")}, expected
                    )

    def test_stress_profile_and_explicit_selection(self) -> None:
        suite = "client_server/server/test_tools"
        with (self.boundaries / "_profiles.py").open("a") as profile:
            profile.write(f'STRESS = ("{suite}::unselected",)\n')
        for arguments, expected in (
            (("--full", "--list"), {"initializes_and_lists_tools", "selected"}),
            (("--stress", "--list"), {"unselected"}),
            (("--full", "--list", f"{suite}::unselected"), {"unselected"}),
            (("--stress", "--list", f"{suite}::selected"), {"selected"}),
            (
                ("--full", "--list", suite),
                {"initializes_and_lists_tools", "selected", "unselected"},
            ),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_runner(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    {line.split("::")[1] for line in result.stdout.splitlines()},
                    expected,
                )
        stress_snapshot = self.snapshots / "unselected.yaml"
        original = stress_snapshot.read_bytes()
        full = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(full.returncode, 0, full.stderr)
        self.assertFalse((self.root / "unselected.marker").exists())
        self.assertEqual(stress_snapshot.read_bytes(), original)
        stress = self.run_runner("--stress", "--jobs", "1")
        self.assertEqual(stress.returncode, 0, stress.stderr)
        self.assertTrue((self.root / "unselected.marker").is_file())
        invalid = self.run_runner("--full", "--stress")
        self.assertEqual(invalid.returncode, 2, invalid.stderr)

    def test_smoke_and_focused_runs_do_not_import_unselected_suites(self) -> None:
        other = self.suite.with_name("test_expensive.py")
        other.write_text('raise RuntimeError("unselected suite imported")')
        other_snapshots = self.snapshots.with_name("test_expensive")
        other_snapshots.mkdir()
        (other_snapshots / "example.yaml").write_text("""---
runner: example
...
""")
        for arguments in (
            (),
            ("--quick",),
            ("client_server/server/test_tools::selected",),
            ("--full", "client_server/server/test_tools::selected"),
            ("--full", "client_server/server/test_tools"),
            ("--full", "--list", "client_server/server/test_tools"),
            ("--full", "--locate", "client_server/server/test_tools::selected"),
            ("--full", "--update", "client_server/server/test_tools::selected"),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_runner(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
        full = self.run_runner("--full")
        self.assertNotEqual(full.returncode, 0)
        self.assertIn("unselected suite imported", full.stderr)

    def test_smoke_update_preserves_unselected_and_orphan_snapshots(self) -> None:
        orphan = self.snapshots / "deleted_case.yaml"
        orphan.write_text("""---
runner: orphan
...
""")
        unselected = self.snapshots / "unselected.yaml"
        original = unselected.read_bytes()
        for profile in ((), ("--quick",)):
            result = self.run_runner(*profile, "--update")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(orphan.exists())
            self.assertEqual(unselected.read_bytes(), original)

    def test_full_profile_selected_updates_preserve_unrelated_orphans(self) -> None:
        orphan = self.snapshots / "deleted_case.yaml"
        orphan.write_text("""---
runner: orphan
...
""")
        selected = self.snapshots / "selected.yaml"
        expected = b"---\nrunner: selected\n"
        for selector in (
            "client_server/server/test_tools::selected",
            "client_server/server/test_tools",
        ):
            with self.subTest(selector=selector):
                selected.write_text("""---
runner: outdated
...
""")
                result = self.run_runner("--full", "--update", selector)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(selected.read_bytes(), expected)
                self.assertTrue(orphan.exists())

    def test_records_timings_for_each_execution_and_snapshot_failure(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions


                @executions(Execution("first"), Execution("second"))
                def test_selected(binary, execution):
                    return record(binary, "selected")
                """),
        )
        timing = self.root / "timings.jsonl"
        selector = "client_server/server/test_tools::selected"
        for failed in (False, True):
            if failed:
                (self.snapshots / "selected.yaml").write_text(
                    """---
runner: different
...
"""
                )
            result = subprocess.run(
                [sys.executable, self.boundaries / "_run.py", selector],
                env=os.environ | {"MCP_CONSOLE_TEST_TIMINGS": str(timing)},
                capture_output=True,
                text=True,
                timeout=10,
            )
            self.assertEqual(result.returncode == 0, not failed, result.stderr)
        rows = [json.loads(line) for line in timing.read_text().splitlines()]
        self.assertEqual(
            [(r["selector"], r["execution"], r["status"]) for r in rows],
            [
                (selector, "first", "passed"),
                (selector, "second", "passed"),
                (selector, "first", "failed"),
            ],
        )
        self.assertTrue(all(r["elapsed_seconds"] > 0 for r in rows))

    def test_repository_cases_skip_missing_commands(self) -> None:
        shutil.copytree(
            ROOT / "tests" / "support",
            self.root / "tests" / "support",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        selectors = {
            "client_server/recording/test_markdown::emits_yamark_formatted_documents": (
                "yamark",
            ),
            "client_server/lifecycle/test_startup": ("ir", "uv"),
            "client_server/lifecycle/test_startup_interrupt": ("ir", "uv"),
            "client_server/requirements/test_r_automatic": ("ir",),
            "client_server/requirements/test_r::failed_mixed_preparation_retains_live_python_activation": (
                "ir",
                "uv",
            ),
            "cli/sandbox/test_configuration::child_specific_shell_python_and_processx_examples": (
                "Rscript",
            ),
        }
        for selector, commands in selectors.items():
            suite = selector.partition("::")[0] + ".py"
            destination = self.boundaries / suite
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(RUNNER.parent / suite, destination)
            for missing in commands:
                with self.subTest(selector=selector, missing=missing):
                    with tempfile.TemporaryDirectory(dir=self.root) as path:
                        for command in set(commands) - {missing}:
                            executable = Path(path) / command
                            executable.touch()
                            executable.chmod(0o755)
                        result = subprocess.run(
                            [
                                sys.executable,
                                self.boundaries / "_run.py",
                                "--jobs",
                                "1",
                                selector,
                            ],
                            cwd=self.root,
                            env={**os.environ, "PATH": path},
                            capture_output=True,
                            text=True,
                            timeout=10,
                        )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    skipped = result.stdout.splitlines()
                    self.assertTrue(skipped, result.stdout)
                    for line in skipped:
                        self.assertIn(": skipped;", line)
                        self.assertIn(
                            f"{missing}: {missing} is missing from PATH", line
                        )

    def test_repository_sklearn_case_skips_one_effective_cpu(self) -> None:
        shutil.copytree(
            ROOT / "tests" / "support",
            self.root / "tests" / "support",
            dirs_exist_ok=True,
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        suite = Path("client_server/python/test_processes.py")
        destination = self.boundaries / suite
        destination.parent.mkdir(parents=True)
        shutil.copy2(RUNNER.parent / suite, destination)
        selector = "client_server/python/test_processes::runs_sklearn_parallel_search"
        result = subprocess.run(
            [sys.executable, self.boundaries / "_run.py", selector],
            cwd=self.root,
            env={**os.environ, "LOKY_MAX_CPU_COUNT": "1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        for execution in ("direct", "sandbox"):
            self.assertIn(f"{selector}[{execution}]: skipped;", result.stdout)
        self.assertIn("requires at least two effective joblib CPUs", result.stdout)

    def test_full_update_preserves_skipped_case_and_companions(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.requirements import Requirement, requires

                test_unselected = requires(Requirement("unavailable", False, "deliberate skip"))(
                    test_unselected
                )
                """),
            encoding="utf-8",
        )
        companion = self.snapshots / "unselected.md"
        companion.write_text("retained companion", encoding="utf-8")
        stale = self.snapshots / "selected.md"
        stale.write_text("obsolete companion", encoding="utf-8")
        before = (self.snapshots / "unselected.yaml").read_bytes()
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.snapshots / "unselected.yaml").read_bytes(), before)
        self.assertEqual(companion.read_text(), "retained companion")
        self.assertFalse(stale.exists())

    def test_full_update_preserves_other_platform_companions(self) -> None:
        other_platform = "linux" if sys.platform == "darwin" else "darwin"
        self.suite.write_text(
            PUBLIC_SUITE
            + "\nfrom support.snapshots import platform_snapshots\n"
            + f"test_selected = platform_snapshots('{other_platform}', reason='fixture OS contract')(test_selected)\n",
            encoding="utf-8",
        )
        companion = self.snapshots / f"selected.{other_platform}.yaml"
        companion.write_text("other platform output\n", encoding="utf-8")
        stale = self.snapshots / f"selected.{sys.platform}.yaml"
        stale.write_text("obsolete local output\n", encoding="utf-8")
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(companion.is_file(), result.stdout)
        self.assertEqual(companion.read_text(), "other platform output\n")
        self.assertFalse(stale.exists())

    def test_execution_requirements_share_one_behavior_snapshot(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions
                from support.requirements import Requirement

                first = Execution("first")
                second = Execution("second")
                missing = Execution("missing", (Requirement("mode", False, "unavailable mode"),))


                @executions(first, missing, second)
                def test_selected(binary, execution):
                    record(binary, execution.name)
                    return [{"runner": "selected"}]
                """),
            encoding="utf-8",
        )
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / "first.marker").exists())
        self.assertTrue((self.root / "second.marker").exists())
        self.assertFalse((self.root / "missing.marker").exists())
        self.assertIn(
            "::selected[missing]: skipped; mode: unavailable mode", result.stdout
        )
        self.assertFalse(list(self.snapshots.glob("selected.*.yaml")))
        # Updating must compare subsequent modes, never overwrite the first one.
        self.suite.write_text(
            self.suite.read_text().replace(
                'return [{"runner": "selected"}]',
                'return [{"runner": execution.name}]',
            )
        )
        differing = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertNotEqual(differing.returncode, 0)
        self.assertIn("second", differing.stderr)

    def test_compacts_each_complete_session_and_preserves_differences(self) -> None:
        handshake = [
            {"input": {"method": "initialize"}, "result": {"protocolVersion": "test"}},
            {"notification": {"method": "notifications/initialized"}},
            {"input": {"method": "tools/list"}, "result": {"tools": []}},
        ]
        changed = [
            *handshake[:-1],
            {"input": {"method": "tools/list"}, "result": {"tools": ["different"]}},
        ]
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code(f"""
                def test_initializes_and_lists_tools(binary):
                    return {handshake!r}

                def test_selected(binary):
                    return [{{"runner": "before"}}] + {handshake!r} + [{{"runner": "between"}}] + {handshake!r} + {changed!r} + {handshake[:-1]!r}
                """),
            encoding="utf-8",
        )
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = (self.snapshots / "selected.yaml").read_text()
        self.assertEqual(snapshot.count("!same-as"), 2, snapshot)
        self.assertIn("different", snapshot)
        # The differing complete session and incomplete session remain visible.
        self.assertEqual(snapshot.count("method: initialize"), 2, snapshot)

    def test_sessions_use_the_reference_for_their_execution(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions
                from support.records import TranscriptWithCompanions

                sandbox = [{"id": 1, "input": {"method": "initialize"}, "result": "sandbox"}]
                direct = [{"id": 2, "input": {"method": "initialize"}, "result": "direct"}]
                bare_sandbox = [{"id": 3, "input": {"method": "initialize"}, "result": "bare sandbox"}]
                bare_direct = [{"id": 4, "input": {"method": "initialize"}, "result": "bare direct"}]


                def test_initializes_and_lists_tools(binary):
                    return TranscriptWithCompanions(
                        sandbox,
                        {
                            "direct.yaml": direct,
                            "aaa.yaml": sandbox,
                            "aaa.direct.yaml": direct,
                            "bare.yaml": bare_sandbox,
                            "bare.direct.yaml": bare_direct,
                        },
                    )


                @executions(Execution("sandbox"), Execution("direct"))
                def test_selected(binary, execution):
                    handshake = sandbox if execution.name == "sandbox" else direct
                    bare = bare_sandbox if execution.name == "sandbox" else bare_direct
                    return handshake + [{"runner": "between sessions"}] + handshake + bare


                def test_unselected(binary):
                    return TranscriptWithCompanions(sandbox + direct, {"wire.yaml": direct})
                """),
            encoding="utf-8",
        )
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        for reference in self.snapshots.glob("initializes_and_lists_tools*.yaml"):
            self.assertNotIn("id:", reference.read_text())
        self.assertIn("id: 2", (self.snapshots / "unselected.wire.yaml").read_text())
        selected = (self.snapshots / "selected.yaml").read_text()
        self.assertEqual(selected.count("!same-as"), 3, selected)
        self.assertIn("bare MCP initialization for this execution mode", selected)
        self.assertNotIn("aaa", selected)
        mixed = (self.snapshots / "unselected.yaml").read_text()
        self.assertIn("initializes_and_lists_tools.yaml", mixed)
        self.assertIn("initializes_and_lists_tools.direct.yaml", mixed)
        self.assertNotIn("aaa", mixed)
        self.suite.write_text(
            self.suite.read_text().replace("else direct", "else sandbox")
        )
        differing = self.run_runner("--full", "--jobs", "1")
        self.assertNotEqual(differing.returncode, 0)
        self.assertIn("result: sandbox", differing.stderr)
        self.assertIn("::selected[direct] differs", differing.stderr)

    def test_mcp_companions_preserve_each_invocation_and_execution(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions
                from support.records import McpTranscript, TranscriptWithCompanions


                def handshake(tool):
                    return [
                        {
                            "id": 1,
                            "input": {"method": "initialize"},
                            "result": {"protocolVersion": "test"},
                        },
                        {"notification": {"method": "notifications/initialized"}},
                        {"id": 2, "input": {"method": "tools/list"}, "result": {"tools": [tool]}},
                    ]


                def test_initializes_and_lists_tools(binary):
                    return TranscriptWithCompanions(
                        handshake("sandbox"),
                        {
                            "direct.yaml": handshake("direct"),
                            "sql-python.yaml": handshake("sql-python sandbox"),
                            "sql-python.direct.yaml": handshake("sql-python direct"),
                        },
                    )


                @executions(Execution("sandbox"), Execution("direct"))
                def test_selected(binary, execution):
                    primary = handshake(execution.name)
                    companion = handshake("sql-python " + execution.name)
                    send = [
                        {
                            "id": 4,
                            "send": {"python": "print(42)"},
                            "result": {"content": [{"type": "text", "text": "42"}]},
                        }
                    ]
                    return TranscriptWithCompanions(
                        [{"configuration": "default"}] + primary + send,
                        {
                            "sql-python.yaml": McpTranscript(
                                [{"configuration": "sql-python"}] + companion + send
                            ),
                            "changed.yaml": McpTranscript(handshake("different") + send),
                            "partial.yaml": McpTranscript(primary[:2]),
                            "wire.yaml": handshake("direct") + send,
                        },
                    )
                """),
            encoding="utf-8",
        )
        updated = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        for suffix, reference in (
            ("", "MCP initialization for this execution mode"),
            (".sql-python", "sql-python MCP initialization for this execution mode"),
        ):
            snapshot = (self.snapshots / f"selected{suffix}.yaml").read_text()
            self.assertEqual(snapshot.count("!same-as"), 1, snapshot)
            self.assertIn(reference, snapshot)
            self.assertLess(
                snapshot.index("configuration:"), snapshot.index("!same-as")
            )
            self.assertLess(snapshot.index("!same-as"), snapshot.index("send:"))
            self.assertNotIn("id:", snapshot)
        for suffix in ("changed", "partial"):
            snapshot = (self.snapshots / f"selected.{suffix}.yaml").read_text()
            self.assertNotIn("!same-as", snapshot)
            self.assertEqual(snapshot.count("method: initialize"), 1, snapshot)
            self.assertNotIn("id:", snapshot)
        changed = (self.snapshots / "selected.changed.yaml").read_text()
        self.assertIn("different", changed)
        wire = (self.snapshots / "selected.wire.yaml").read_text()
        self.assertNotIn("!same-as", wire)
        self.assertIn("id: 2", wire)
        self.assertIn("id: 4", wire)
        strict = self.run_runner("--full", "--jobs", "1")
        self.assertEqual(strict.returncode, 0, strict.stderr)

        # A second execution must compare its companions, including during updates.
        self.suite.write_text(
            self.suite.read_text().replace(
                'handshake("sql-python " + execution.name)',
                'handshake("sql-python sandbox")',
            )
        )
        for arguments in (("--full",), ("--full", "--update")):
            differing = self.run_runner(*arguments, "--jobs", "1")
            self.assertNotEqual(differing.returncode, 0)
            self.assertIn("selected.sql-python.yaml", differing.stderr)
            self.assertIn("::selected[direct] differs", differing.stderr)

    def test_project_proxy_sessions_use_a_canonical_reference(self) -> None:
        references = ROOT / "tests/snapshots/client_server/server/test_tools"
        for reference in references.glob("initializes_and_lists_tools*.yaml"):
            shutil.copy2(reference, self.snapshots / reference.name)
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from yaml12 import read_yaml


                def test_selected(binary):
                    reference = (
                        binary.parents[2]
                        / "tests/snapshots/client_server/server/test_tools/initializes_and_lists_tools.yaml"
                    )
                    handshake = read_yaml(reference, multi=True)
                    send = handshake[-1]["result"]["tools"][0]
                    send["description"] = send["description"].replace(
                        "cannot directly access the network",
                        "can access the network subject to the launcher's proxy settings",
                    )
                    return [{"runner": "before"}] + handshake + [{"runner": "between"}] + handshake
                """),
            encoding="utf-8",
        )
        result = self.run_runner(
            "--update", "client_server/server/test_tools::selected"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        snapshot = (self.snapshots / "selected.yaml").read_text()
        self.assertEqual(snapshot.count("!same-as"), 2, snapshot)
        self.assertEqual(snapshot.count("initializes_and_lists_tools.proxy.yaml"), 2)
        self.assertNotIn("method: initialize", snapshot)

    def test_initialization_updates_only_available_execution_references(self) -> None:
        source = (
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions
                from support.records import TranscriptWithCompanions
                from support.requirements import Requirement


                @executions(
                    Execution("direct"),
                    Execution("sandbox", (Requirement("sandbox", AVAILABLE, "unavailable"),)),
                )
                def test_initializes_and_lists_tools(binary, execution):
                    return TranscriptWithCompanions(
                        [{"mode": execution.name}], {"bare.yaml": [{"bare": execution.name}]}
                    )
                """)
        )
        self.suite.write_text(source.replace("AVAILABLE", "True"))
        result = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(result.returncode, 0, result.stderr)
        references = {
            path: path.read_bytes()
            for path in self.snapshots.glob("initializes_and_lists_tools*.yaml")
        }
        self.assertEqual(len(references), 4)
        self.suite.write_text(source.replace("AVAILABLE", "False"))
        for arguments in (
            ("--list",),
            (
                "--locate",
                "client_server/server/test_tools::initializes_and_lists_tools",
            ),
            ("--update",),
            ("--full", "--update"),
            (
                "--update",
                "client_server/server/test_tools::initializes_and_lists_tools",
            ),
        ):
            result = self.run_runner(*arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                {path: path.read_bytes() for path in references}, references
            )

    @contextmanager
    def hanging_runner(
        self, *arguments: str
    ) -> Iterator[tuple[subprocess.Popen[str], int, int]]:
        self.suite.write_text(PUBLIC_SUITE + HANGING_SUITE, encoding="utf-8")
        for name in ("hangs", "failure_beside_hang"):
            (self.snapshots / f"{name}.yaml").write_text(
                """---
runner: released
...
""",
                encoding="utf-8",
            )
        checkpoints = []
        for name in (
            "hang-started",
            "hang-release",
            "failure-release",
            "child-cleanup-complete",
        ):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        process = self.start_runner(*arguments)
        try:
            yield process, checkpoints[0], checkpoints[2]
        finally:
            # Cases have their own sessions. Release their fixture waits even
            # when the runner itself fails before it can interrupt them.
            try:
                for checkpoint in checkpoints[1:3]:
                    os.write(checkpoint, b"1")
                try:
                    process.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    with suppress(ProcessLookupError):
                        os.killpg(process.pid, signal.SIGKILL)
                    process.communicate(timeout=10)
                ready, _, _ = select.select([checkpoints[3]], [], [], 5)
                self.assertTrue(ready, "released case did not finish child cleanup")
                self.assertEqual(os.read(checkpoints[3], 1), b"1")
            finally:
                for checkpoint in checkpoints:
                    os.close(checkpoint)

    def test_case_deadline_stops_a_hanging_case(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "2", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{selector}: failed", stderr)
            self.assertIn("timed out", stderr)
            self.assertTrue((self.root / "child-cleaned").is_file())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_case_deadline_stops_hang_during_other_failure_cleanup(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        failure = "client_server/server/test_tools::failure_beside_hang"
        with self.hanging_runner(
            "--timeout", "2", "--jobs", "2", selector, failure
        ) as (
            process,
            started,
            release_failure,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            assert process.stderr is not None
            reported = ""
            deadline = time.monotonic() + 10
            while f"{failure}: failed" not in reported:
                remaining = deadline - time.monotonic()
                self.assertGreater(remaining, 0, "snapshot failure was not reported")
                ready, _, _ = select.select([process.stderr], [], [], remaining)
                self.assertTrue(ready, "snapshot failure was not reported")
                data = os.read(process.stderr.fileno(), 4096)
                self.assertTrue(data, "runner exited before reporting snapshot failure")
                reported += data.decode()
            self.assertNotIn(
                "timed out", reported[: reported.index(f"{failure}: failed")]
            )
            stdout, stderr = process.communicate(timeout=10)
            stderr = reported + stderr
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertIn(selector, stderr)
            self.assertIn("timed out", stderr)
            self.assertIn("multiple transcript cases failed", stderr)
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_failure_limit_finishes_running_sibling(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        failure = "client_server/server/test_tools::failure_beside_hang"
        queued = "client_server/server/test_tools::selected"
        with self.hanging_runner(
            "--timeout", "60", "--jobs", "2", selector, failure, queued
        ) as (
            process,
            started,
            release_failure,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            receipt = self.read_snapshot_failure(process, failure)
            self.assertIn(f"{failure}: failed", receipt)
            with (self.root / "hang-release").open("wb", buffering=0) as release:
                self.assertEqual(release.write(b"1"), 1)
            stdout, stderr = process.communicate(timeout=10)
            stderr = receipt + stderr
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertNotIn(f"{selector}: cancelled", stdout + stderr)
            self.assertNotIn(f"{selector}: failed", stdout + stderr)
            self.assertNotIn("timed out", stderr)
            self.assertNotIn("multiple transcript cases failed", stderr)
            self.assertIn("1 of 3 transcript cases failed; not started: 1", stderr)
            self.assertEqual(
                stderr.count(f"{failure} differs from its snapshot"), 1, stderr
            )
            self.assertFalse((self.root / "selected.marker").exists())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_independent_case_interrupt_remains_a_failure(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + SELF_INTERRUPTING_SUITE, encoding="utf-8")
        selector = "client_server/server/test_tools::interrupts_itself"
        result = self.run_runner(selector)
        self.assertNotEqual(result.returncode, 0, result.stdout)
        self.assertIn(f"{selector}: failed", result.stderr)
        self.assertIn("KeyboardInterrupt", result.stderr)
        self.assertNotIn(f"{selector}: cancelled", result.stdout + result.stderr)

    def assert_cancellation_preserves_the_original_failure(
        self, *, signal_from_worker: bool
    ) -> None:
        if signal_from_worker:
            runner = self.boundaries / "_run.py"
            runner.write_text(
                WORKER_DELIVERED_INTERRUPT + "\n" + runner.read_text(encoding="utf-8"),
                encoding="utf-8",
            )
        selector = "client_server/server/test_tools::fails_before_cleanup"
        failure = "client_server/server/test_tools::failure_beside_hang"
        with self.hanging_runner(
            "--timeout", "60", "--jobs", "2", selector, failure
        ) as (process, started, release_failure):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "failed case did not enter its cleanup")
            self.assertEqual(os.read(started, 1), b"1")
            self.assertEqual(os.write(release_failure, b"1"), 1)
            receipt = self.read_snapshot_failure(process, failure)
            self.assertIn(f"{failure}: failed", receipt)
            if not signal_from_worker:
                process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            stderr = receipt + stderr
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(f"{failure}: failed", stderr)
            self.assertIn("runner: deliberate mismatch", stderr)
            self.assertIn(f"{selector}: cancelled", stdout + stderr)
            self.assertIn("AssertionError: original failure before cleanup", stderr)
            self.assertIn("KeyboardInterrupt", stderr)
            self.assertIn("multiple transcript cases failed", stderr)

    def test_user_cancellation_preserves_the_original_failure(self) -> None:
        self.assert_cancellation_preserves_the_original_failure(
            signal_from_worker=False
        )

    def test_worker_delivered_cancellation_preserves_the_original_failure(self) -> None:
        self.assert_cancellation_preserves_the_original_failure(signal_from_worker=True)

    def assert_signal_retires_case(self, number: signal.Signals) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "60", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "hanging case did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(number)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn(selector, stderr)
            self.assertIn("KeyboardInterrupt", stderr)
            if number != signal.SIGINT:
                self.assertIn(f"transcript runner received {number.name}", stderr)
            self.assertTrue((self.root / "child-cleaned").is_file())
            with self.assertRaises(ProcessLookupError):
                os.killpg(process.pid, 0)

    def test_interrupt_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGINT)

    def test_termination_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGTERM)

    def test_hangup_stops_hanging_case_and_runs_cleanup(self) -> None:
        self.assert_signal_retires_case(signal.SIGHUP)

    def test_interrupt_during_final_snapshot_check_is_reported(self) -> None:
        snapshots = self.root / "tests" / "support" / "snapshots.py"
        with snapshots.open("a", encoding="utf-8") as source:
            source.write("\n" + GATED_SNAPSHOT_CHECK)
        checkpoints = []
        for name in ("snapshot-started", "snapshot-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        started, release = checkpoints
        process = self.start_runner("client_server/server/test_tools::selected")
        try:
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "final snapshot check did not start")
            self.assertEqual(os.read(started, 1), b"1")
            process.send_signal(signal.SIGINT)
            stdout, stderr = process.communicate(timeout=10)
            self.assertNotEqual(process.returncode, 0, stdout)
            self.assertIn("KeyboardInterrupt", stderr)
            self.assertTrue((self.root / "snapshot-check-cleaned").is_file())
        finally:
            os.write(release, b"1")
            try:
                process.communicate(timeout=10)
            except subprocess.TimeoutExpired:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=10)
            for checkpoint in checkpoints:
                os.close(checkpoint)

    def assert_signal_stops_blocked_snapshot_check(
        self, number: signal.Signals
    ) -> None:
        snapshots = self.root / "tests" / "support" / "snapshots.py"
        with snapshots.open("a", encoding="utf-8") as source:
            source.write("\n" + GATED_SNAPSHOT_CHECK)
        checkpoints = []
        for name in ("snapshot-started", "snapshot-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        checking, release = checkpoints
        try:
            with self.hanging_runner(
                "--timeout",
                "60",
                "--jobs",
                "2",
                "client_server/server/test_tools::selected",
                "client_server/server/test_tools::hangs",
            ) as (process, sibling_started, _):
                try:
                    for checkpoint in (checking, sibling_started):
                        ready, _, _ = select.select([checkpoint], [], [], 10)
                        self.assertTrue(
                            ready, "snapshot check and sibling did not start"
                        )
                        self.assertEqual(os.read(checkpoint, 1), b"1")
                    process.send_signal(number)
                    try:
                        stdout, stderr = process.communicate(timeout=10)
                    except subprocess.TimeoutExpired:
                        self.fail(
                            "signal did not stop the blocked snapshot check and sibling"
                        )
                    self.assertNotEqual(process.returncode, 0, stdout)
                    self.assertIn("KeyboardInterrupt", stderr)
                    if number != signal.SIGINT:
                        self.assertIn(
                            f"transcript runner received {number.name}", stderr
                        )
                    self.assertTrue((self.root / "snapshot-check-cleaned").is_file())
                    self.assertTrue((self.root / "child-cleaned").is_file())
                finally:
                    # Either case can reach its snapshot check during cleanup.
                    os.write(release, b"11")
        finally:
            for checkpoint in checkpoints:
                os.close(checkpoint)

    def test_interrupt_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGINT)

    def test_termination_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGTERM)

    def test_hangup_during_snapshot_check_stops_siblings(self) -> None:
        self.assert_signal_stops_blocked_snapshot_check(signal.SIGHUP)

    def test_runner_loss_retires_its_detached_case(self) -> None:
        selector = "client_server/server/test_tools::hangs"
        with self.hanging_runner("--timeout", "60", "--jobs", "1", selector) as (
            process,
            started,
            _,
        ):
            cleaned = os.open(
                self.root / "child-cleanup-complete", os.O_RDONLY | os.O_NONBLOCK
            )
            try:
                ready, _, _ = select.select([started], [], [], 10)
                self.assertTrue(ready, "hanging case did not start")
                self.assertEqual(os.read(started, 1), b"1")
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
                ready, _, _ = select.select([cleaned], [], [], 5)
                self.assertTrue(
                    ready, "detached case outlived its runner without cleanup"
                )
                self.assertTrue((self.root / "child-cleaned").is_file())
            finally:
                os.close(cleaned)

    @unittest.skipUnless(PROCESS_EVENTS.available, PROCESS_EVENTS.reason)
    def test_runner_loss_retires_case_holding_the_gil(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + GIL_HOLDING_SUITE, encoding="utf-8")
        (self.snapshots / "holds_gil.yaml").write_text(
            """---
runner: released
...
""",
            encoding="utf-8",
        )
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                self.root / "gil-checkpoint.dylib",
                ROOT / "tests" / "fixtures" / "native" / "python_probe_checkpoint.c",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        checkpoints = []
        for name in ("gil-started", "gil-release"):
            os.mkfifo(self.root / name)
            checkpoints.append(os.open(self.root / name, os.O_RDWR | os.O_NONBLOCK))
        started, release = checkpoints
        process = self.start_runner(
            "--timeout",
            "60",
            "--jobs",
            "1",
            "client_server/server/test_tools::holds_gil",
        )
        identity = None
        exits = Events()
        try:
            ready, _, _ = select.select([started], [], [], 10)
            self.assertTrue(ready, "case did not enter its native GIL-holding call")
            self.assertEqual(os.read(started, 1), b"1")
            pid = int((self.root / "gil-case-pid").read_text(encoding="utf-8"))
            identity = capture_process_identity(pid)
            exits.watch_process(pid)
            os.killpg(process.pid, signal.SIGKILL)
            process.wait(timeout=5)
            observed = exits.wait(20)
            self.assertTrue(
                observed, "GIL-holding case outlived its runner's cleanup deadline"
            )
            self.assertEqual(observed, {pid})
        finally:
            os.write(release, b"1")
            if identity is not None:
                signal_process(identity, signal.SIGKILL)
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
            exits.close()
            for checkpoint in checkpoints:
                os.close(checkpoint)

    @unittest.skipUnless(
        sys.version_info >= (3, 12), "requires Python fork diagnostics"
    )
    def test_case_can_fork_without_thread_safety_warnings(self) -> None:
        self.suite.write_text(PUBLIC_SUITE + FORKING_SUITE, encoding="utf-8")
        (self.snapshots / "forks.yaml").write_text(
            """---
runner: forked
warnings: []
...
""",
            encoding="utf-8",
        )
        result = self.run_runner("client_server/server/test_tools::forks")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_orphan_rejection_and_full_update_cleanup(self) -> None:
        orphan = self.snapshots / "deleted_case.yaml"
        orphan.write_text(
            """---
runner: orphan
...
""",
            encoding="utf-8",
        )

        rejected = self.run_runner("--full", "--list")
        self.assertNotEqual(rejected.returncode, 0)
        self.assertIn(
            "orphan snapshot: "
            "tests/snapshots/client_server/server/test_tools/deleted_case.yaml",
            rejected.stderr,
        )
        self.assertIn(
            "run scripts/test --full --update to remove orphan snapshots",
            rejected.stderr,
        )

        updated = self.run_runner("--full", "--update", "--jobs", "2")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        self.assertFalse(orphan.exists())
        self.assertIn(
            "removed tests/snapshots/client_server/server/test_tools/deleted_case.yaml",
            updated.stdout,
        )
        self.assertEqual(
            {path.name for path in self.snapshots.iterdir()},
            {
                "initializes_and_lists_tools.yaml",
                "selected.yaml",
                "unselected.yaml",
            },
        )

    def test_focused_failure_rerun_omits_profiles_and_preserves_timeout(self) -> None:
        (self.snapshots / "selected.yaml").write_text("""---
runner: mismatch
...
""")
        for profile in ([], ["--quick"], ["--full"]):
            with self.subTest(profile=profile):
                arguments = [
                    "--timeout",
                    "1200.5",
                    "client_server/server/test_tools::selected",
                ]
                result = self.run_runner(*profile, *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(
                    f"rerun: scripts/test {shlex.join(arguments)}", result.stderr
                )

    def test_failure_rerun_preserves_snapshot_update(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                def test_selected(binary):
                    raise RuntimeError("fixture failed before snapshot update")
                """)
        )
        result = self.run_runner(
            "--update", "client_server/server/test_tools::selected"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(
            "rerun: scripts/test --update client_server/server/test_tools::selected",
            result.stderr,
        )

    def test_failure_rerun_preserves_full_update_with_orphans(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                def test_selected(binary):
                    raise RuntimeError("fixture failed before snapshot update")
                """)
        )
        orphan = self.snapshots / "deleted_case.yaml"
        orphan.write_text("""---
runner: orphan
...
""")
        arguments = ["--full", "--update", "--jobs", "1"]
        result = self.run_runner(*arguments)
        self.assertNotEqual(result.returncode, 0)
        receipt = next(
            line for line in result.stderr.splitlines() if line.startswith("rerun: ")
        )
        self.assertEqual(receipt, f"rerun: scripts/test {shlex.join(arguments)}")
        self.assertTrue(orphan.exists())
        retried = self.run_runner(*shlex.split(receipt)[2:])
        self.assertIn("fixture failed before snapshot update", retried.stderr)
        self.assertNotIn("orphan snapshot:", retried.stderr)

    def write_failure_collection_suite(self, failures: set[int]) -> list[str]:
        names = ["initializes_and_lists_tools", *[f"case_{i:02}" for i in range(1, 20)]]
        for snapshot in self.snapshots.glob("*.yaml"):
            snapshot.unlink()
        programs = ["from pathlib import Path\n"]
        for index, name in enumerate(names):
            programs.append(
                # fmt: python
                code("""
                    def test_NAME(binary: Path):
                        (binary.parents[2] / "NAME.marker").touch()
                        assert SUCCEEDS, "failure in NAME"
                        return [{"runner": "NAME"}]
                    """)
                .replace("NAME", name)
                .replace("SUCCEEDS", repr(index not in failures))
            )
            (self.snapshots / f"{name}.yaml").write_text(f"---\nrunner: {name}\n...\n")
        self.suite.write_text("\n".join(programs))
        selectors = [f"client_server/server/test_tools::{name}" for name in names]
        (self.boundaries / "_profiles.py").write_text(
            f"SMOKE = {selectors!r}\nSTRESS = ()\n"
        )
        return names

    def test_collects_all_failures_through_fifteen_percent(self) -> None:
        names = self.write_failure_collection_suite({0, 7, 19})
        for arguments in ((), ("--full",)):
            with self.subTest(arguments=arguments):
                for marker in self.root.glob("*.marker"):
                    marker.unlink()
                # Twenty serial subprocesses also inventory host capabilities.
                result = self.run_runner("--jobs", "1", *arguments, timeout=60)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(
                    sorted(path.stem for path in self.root.glob("*.marker")),
                    sorted(names),
                )
                self.assertIn("3 of 20 transcript cases failed", result.stderr)
                for index in (0, 7, 19):
                    self.assertIn(f"failure in {names[index]}", result.stderr)
                self.assertNotIn("Stopping", result.stderr)

    def test_stops_starting_cases_above_fifteen_percent(self) -> None:
        names = self.write_failure_collection_suite({0, 1, 2, 3})
        result = self.run_runner("--full", "--jobs", "1")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(
            sorted(path.stem for path in self.root.glob("*.marker")),
            sorted(names[:4]),
        )
        self.assertIn("4 of 20 transcript cases failed", result.stderr)
        self.assertIn("more than 15%", result.stderr)
        self.assertIn("not started: 16", result.stderr)

    def test_reports_diagnostics_for_more_than_fifteen_failures(self) -> None:
        names = self.write_failure_collection_suite(set(range(20)))
        # Initialization fails first; the other 19 cases are already admitted
        # together when the failure limit is crossed.
        result = self.run_runner("--full", "--jobs", "20")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("20 of 20 transcript cases failed", result.stderr)
        for name in names:
            self.assertEqual(
                result.stderr.count(f"AssertionError: failure in {name}"),
                1,
                result.stderr,
            )

    def test_parallel_failure_exits_and_reports_every_failure(self) -> None:
        self.suite.write_text(FAILING_SUITE, encoding="utf-8")
        for name in ("selected", "unselected"):
            (self.snapshots / f"{name}.yaml").unlink()
        for name in ("first_failure", "second_failure"):
            (self.snapshots / f"{name}.yaml").write_text(
                f"---\nrunner: {name} expected\n...\n",
                encoding="utf-8",
            )
        os.mkfifo(self.root / "started")
        os.mkfifo(self.root / "release-first")
        os.mkfifo(self.root / "release-second")
        started = os.open(self.root / "started", os.O_RDWR | os.O_NONBLOCK)
        release_first = os.open(self.root / "release-first", os.O_RDWR)
        release_second = os.open(self.root / "release-second", os.O_RDWR)
        process = self.start_runner("--full", "--jobs", "2")
        try:
            acknowledgements = b""
            while len(acknowledgements) < 2:
                ready, _, _ = select.select([started], [], [], 10)
                self.assertTrue(ready, "both failing cases did not start")
                acknowledgements += os.read(started, 2 - len(acknowledgements))
            self.assertEqual(os.write(release_first, b"1"), 1)
            # Keep the sibling gated until the complete first diagnostic arrives.
            observed_stderr = self.read_snapshot_failure(
                process, "client_server/server/test_tools::first_failure"
            )
            self.assertEqual(
                observed_stderr.splitlines()[:3],
                [
                    "client_server/server/test_tools::first_failure: failed",
                    "rerun: scripts/test client_server/server/test_tools::first_failure",
                    "Stopping new cases: 1 of 3 transcript cases failed (more than 15%); finishing running cases.",
                ],
            )
            self.assertIn("runner: first actual", observed_stderr)
            self.assertNotIn("runner: second actual", observed_stderr)
            self.assertIsNone(process.poll())
            self.assertEqual(os.write(release_second, b"2"), 1)
            stdout, remaining_stderr = process.communicate(timeout=10)
            stderr = observed_stderr + remaining_stderr
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
            self.fail(f"runner did not exit; stdout={stdout!r}; stderr={stderr!r}")
        finally:
            os.close(started)
            os.close(release_first)
            os.close(release_second)
            if process.poll() is None:
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

        self.assertNotEqual(process.returncode, 0)
        self.assertIn("client_server/server/test_tools::first_failure: failed", stderr)
        self.assertIn("client_server/server/test_tools::second_failure: failed", stderr)
        for name in ("first_failure", "second_failure"):
            self.assertIn(
                f"rerun: scripts/test client_server/server/test_tools::{name}",
                stderr,
            )
            self.assertEqual(
                stderr.count(
                    f"client_server/server/test_tools::{name} differs from its snapshot"
                ),
                1,
                stderr,
            )
        self.assertIn("runner: first actual", stderr)
        self.assertIn("runner: second actual", stderr)
        self.assertIn("multiple transcript cases failed (2 sub-exceptions)", stderr)
        with self.assertRaises(ProcessLookupError):
            os.killpg(process.pid, 0)


class TranscriptDiscoveryTests(TranscriptRunnerFixture):
    def test_documentation_companion_links_check_repository_and_nested_guides(
        self,
    ) -> None:
        selector = "cli/test_documentation::current_companion_links_match_pin"
        suite = self.boundaries / "cli/test_documentation.py"
        suite.parent.mkdir()
        shutil.copy2(ROOT / "tests/boundaries/cli/test_documentation.py", suite)
        snapshot = self.root / "tests/snapshots/cli/test_documentation"
        snapshot.mkdir(parents=True)
        shutil.copy2(
            ROOT
            / "tests/snapshots/cli/test_documentation/current_companion_links_match_pin.yaml",
            snapshot,
        )
        for name in ("README.md", "RELEASE.md"):
            (self.root / name).touch()
        # A renamed companion must still reject links to its old repository.
        repository = "new-owner/native-runner"
        revision = "1" * 40
        (self.root / "sandbox-runner.json").write_text(
            json.dumps({"repository": repository, "commit": revision}),
            encoding="utf-8",
        )
        for name, linked_repository, linked_revision, valid in (
            ("README.md", "t-kalinowski/cobox", revision, False),
            ("RELEASE.md", "old-owner/native-runner", revision, False),
            ("docs/python/index.qmd", repository, "0" * 40, False),
            ("docs/guides/nested/guide.md", repository, "main", False),
            ("docs/python/index.qmd", "t-kalinowski/cobox", revision, False),
            ("docs/python/index.qmd", repository, revision, True),
            ("docs/guides/nested/guide.md", repository, revision, True),
            ("docs/RESOLVER_RELEASE_AUDIT.md", "t-kalinowski/cobox", "0" * 40, True),
            ("docs/benchmarks/nested/record.qmd", "t-kalinowski/cobox", "0" * 40, True),
            ("docs/_site/python/index.md", "t-kalinowski/cobox", "0" * 40, True),
            ("docs/.quarto/record.md", "t-kalinowski/cobox", "0" * 40, True),
            ("docs/TOOL_DESCRIPTIONS.md", "openai/codex", "0" * 40, True),
        ):
            with self.subTest(document=name, repository=linked_repository):
                document = self.root / name
                document.parent.mkdir(parents=True, exist_ok=True)
                (self.root / "docs").mkdir(exist_ok=True)
                document.write_text(
                    f"[Source](https://github.com/{linked_repository}/blob/"
                    f"{linked_revision}/codex-rs/mcp-console-sandbox/src/main.rs)\n",
                    encoding="utf-8",
                )
                try:
                    result = self.run_runner(selector)
                    output = result.stdout + result.stderr
                    if valid:
                        self.assertEqual(result.returncode, 0, output)
                    else:
                        self.assertNotEqual(result.returncode, 0, output)
                        self.assertIn(
                            f"{document.relative_to(self.root)}:1: companion reference",
                            output,
                        )
                        self.assertIn(linked_repository, output)
                        self.assertIn(linked_revision, output)
                finally:
                    document.write_text("", encoding="utf-8")

    def test_shared_direct_handshake_precedes_equal_variants(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import DIRECT, executions
                from support.records import TranscriptWithCompanions


                def handshake():
                    return [
                        {"input": {"method": "initialize"}, "result": {"protocolVersion": "test"}},
                        {"notification": {"method": "notifications/initialized"}},
                        {"input": {"method": "tools/list"}, "result": {"tools": ["direct"]}},
                    ]


                @executions(DIRECT)
                def test_initializes_and_lists_tools(binary, execution):
                    return TranscriptWithCompanions(handshake(), {"bare.yaml": handshake()})


                @executions(DIRECT)
                def test_selected(binary, execution):
                    return handshake() + [{"runner": "selected"}]
                """),
            encoding="utf-8",
        )
        updated = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        snapshot = (self.snapshots / "selected.yaml").read_text()
        self.assertIn("!same-as MCP initialization for this execution mode", snapshot)
        strict = self.run_runner("--full", "--jobs", "1")
        self.assertEqual(strict.returncode, 0, strict.stderr)

    def test_shared_sessions_canonicalize_fixture_writable_roots(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                from support.execution import Execution, executions
                from support.records import McpTranscript, TranscriptWithCompanions


                def handshake(tool):
                    return [
                        {"input": {"method": "initialize"}, "result": {"protocolVersion": "test"}},
                        {"notification": {"method": "notifications/initialized"}},
                        {"input": {"method": "tools/list"}, "result": {"tools": [tool]}},
                    ]


                def test_initializes_and_lists_tools(binary):
                    return TranscriptWithCompanions(
                        handshake("sandbox"),
                        {
                            "direct.yaml": handshake("direct"),
                            "writable.yaml": handshake("sandbox with fixture grant"),
                            "custom.direct.yaml": handshake("custom direct"),
                            "custom-writable.yaml": handshake("custom sandbox with fixture grant"),
                        },
                    )


                @executions(Execution("direct"), Execution("sandbox"))
                def test_selected(binary, execution):
                    tool = "direct" if execution.name == "direct" else "sandbox with fixture grant"
                    return TranscriptWithCompanions(
                        handshake(tool) + [{"runner": "selected"}],
                        {"custom.yaml": McpTranscript(handshake("custom " + tool))},
                    )
                """),
            encoding="utf-8",
        )
        updated = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(updated.returncode, 0, updated.stderr)
        for suffix, reference in (
            ("", "MCP initialization for this execution mode"),
            (".custom", "custom MCP initialization for this execution mode"),
        ):
            snapshot = (self.snapshots / f"selected{suffix}.yaml").read_text()
            self.assertIn("!same-as " + reference, snapshot)
            self.assertNotIn("writable", snapshot)
        self.assertFalse((self.snapshots / "selected.sandbox.yaml").exists())
        strict = self.run_runner("--full", "--jobs", "1")
        self.assertEqual(strict.returncode, 0, strict.stderr)

        # A grant that differs from the canonical handshake remains visible.
        original = self.suite.read_text()
        self.suite.write_text(
            original.replace(
                'else "sandbox with fixture grant"',
                'else "sandbox with different grant"',
            )
        )
        differing = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertNotEqual(differing.returncode, 0)
        self.assertIn("sandbox with different grant", differing.stderr)

        # Cases that own captured policy retain its reference and separate modes.
        self.suite.write_text(
            original.replace(
                '@executions(Execution("direct"), Execution("sandbox"))',
                'from support.snapshots import execution_snapshots\n\n@execution_snapshots\n@executions(Execution("direct"), Execution("sandbox"))',
            )
        )
        separate = self.run_runner("--full", "--update", "--jobs", "1")
        self.assertEqual(separate.returncode, 0, separate.stderr)
        snapshot = (self.snapshots / "selected.sandbox.yaml").read_text()
        self.assertIn("!same-as writable MCP initialization", snapshot)
        companion = (self.snapshots / "selected.sandbox.custom.yaml").read_text()
        self.assertIn("!same-as custom-writable MCP initialization", companion)

    def test_focused_update_preserves_unavailable_execution_companions(self) -> None:
        self.suite.write_text(
            PUBLIC_SUITE
            + "\nfrom support.snapshots import execution_snapshots\n"
            + "from support.execution import DIRECT, Execution, executions\n"
            + "from support.requirements import Requirement\n"
            + "portable_initialization = test_initializes_and_lists_tools\n"
            + "@executions(DIRECT)\n"
            + "def test_initializes_and_lists_tools(binary, execution):\n"
            + "    return portable_initialization(binary)\n"
            + "portable_selected = test_selected\n"
            + "@execution_snapshots\n"
            + "@executions(DIRECT, Execution('sandbox', (Requirement('fixture sandbox', False, 'unavailable fixture mode'),)))\n"
            + "def test_selected(binary, execution):\n"
            + "    return portable_selected(binary)\n",
            encoding="utf-8",
        )
        companion = self.snapshots / "selected.sandbox.yaml"
        companion.write_text("unavailable execution output\n", encoding="utf-8")
        result = self.run_runner(
            "--update",
            "client_server/server/test_tools::initializes_and_lists_tools",
            "client_server/server/test_tools::selected",
            "--jobs",
            "1",
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(companion.read_text(), "unavailable execution output\n")

    def test_platform_snapshot_requires_contract_reason(self) -> None:
        for declaration in (
            "platform_snapshots('win32')",
            "platform_snapshots('win32', reason=' ')",
        ):
            with self.subTest(declaration=declaration):
                self.suite.write_text(
                    PUBLIC_SUITE
                    + "\nfrom support.snapshots import platform_snapshots\n"
                    + f"test_selected = {declaration}(test_selected)\n",
                    encoding="utf-8",
                )
                result = self.run_runner("--list")
                self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertIn("reason", result.stderr)

    def test_focused_update_retires_local_platform_variant(self) -> None:
        stale = self.snapshots / f"selected.{sys.platform}.yaml"
        stale.write_text("obsolete local output\n", encoding="utf-8")
        other_platform = "linux" if sys.platform == "darwin" else "darwin"
        self.suite.write_text(
            PUBLIC_SUITE
            + "\nfrom support.snapshots import platform_snapshots\n"
            + f"test_selected = platform_snapshots('{other_platform}', reason='fixture OS contract')(test_selected)\n",
            encoding="utf-8",
        )
        companion = self.snapshots / f"selected.{other_platform}.yaml"
        companion.write_text("other platform output\n", encoding="utf-8")
        unselected = self.snapshots / f"unselected.{sys.platform}.yaml"
        unselected.write_text("unselected output\n", encoding="utf-8")
        result = self.run_runner(
            "--update", "client_server/server/test_tools::selected", "--jobs", "1"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stale.exists(), result.stdout)
        self.assertEqual(companion.read_text(), "other platform output\n")
        self.assertEqual(unselected.read_text(), "unselected output\n")

    def test_focused_update_retires_undeclared_other_platform_variant(self) -> None:
        other_platform = "linux" if sys.platform == "darwin" else "darwin"
        stale = self.snapshots / f"selected.{other_platform}.yaml"
        stale.write_text("retired platform output\n", encoding="utf-8")
        result = self.run_runner(
            "--update", "client_server/server/test_tools::selected", "--jobs", "1"
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(stale.exists(), result.stdout)

    def test_failed_focused_update_preserves_platform_snapshots(self) -> None:
        stale = self.snapshots / f"selected.{sys.platform}.yaml"
        stale.write_text("retained platform output\n", encoding="utf-8")
        self.suite.write_text(
            PUBLIC_SUITE.replace(
                'return record(binary, "selected")',
                'raise RuntimeError("fixture failed")',
            ),
            encoding="utf-8",
        )
        result = self.run_runner(
            "--update", "client_server/server/test_tools::selected", "--jobs", "1"
        )
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(stale.read_text(), "retained platform output\n")

    def test_help_reports_default_concurrency_for_available_cpus(self) -> None:
        for cpu_count, jobs in ((None, 2), (0, 2), (1, 2), (2, 4), (8, 16), (64, 128)):
            with self.subTest(cpu_count=cpu_count):
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        # fmt: python
                        code("""
                            import json
                            import runpy
                            import sys
                            from unittest.mock import patch

                            cpu_count = json.loads(sys.argv.pop(1))
                            runner = sys.argv.pop(1)
                            with patch("os.cpu_count", return_value=cpu_count):
                                runpy.run_path(runner, run_name="__main__")
                            """),
                        json.dumps(cpu_count),
                        self.boundaries / "_run.py",
                        "--bootstrap",
                        "--help",
                    ],
                    cwd=self.root,
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn(
                    f"concurrent transcript cases (default: {jobs}; "
                    "twice the logical CPU count, at least 2)",
                    " ".join(result.stdout.split()),
                )

    def test_collection_selectors_and_locate(self) -> None:
        (self.root / "target/release/mcp-console").unlink()
        hidden = (
            self.boundaries / "client_server" / "server" / "_private" / "test_hidden.py"
        )
        hidden.parent.mkdir()
        hidden.write_text(
            "def test_hidden(binary):\n    return [{'runner': 'hidden'}]\n",
            encoding="utf-8",
        )
        suite = "client_server/server/test_tools"
        cases = [
            f"{suite}::initializes_and_lists_tools",
            f"{suite}::selected",
            f"{suite}::unselected",
        ]
        source = self.suite.relative_to(self.root)
        snapshots = self.snapshots.relative_to(self.root)

        listed = self.run_runner("--full", "--list")
        self.assertEqual(listed.returncode, 0, listed.stderr)
        self.assertEqual(listed.stdout.splitlines(), cases)

        located = self.run_runner("--locate", f"{suite}::selected")
        self.assertEqual(located.returncode, 0, located.stderr)
        self.assertEqual(located.stdout.splitlines()[0], f"{suite}::selected")
        self.assertIn(
            f"source: {source}:",
            located.stdout,
        )
        self.assertIn(
            f"snapshot: {snapshots / 'selected.yaml'}",
            located.stdout,
        )

        located_suite = self.run_runner("--locate", suite)
        self.assertEqual(located_suite.returncode, 0, located_suite.stderr)
        located_lines = located_suite.stdout.splitlines()
        self.assertEqual(len(located_lines), 3 * len(cases))
        for index, case in enumerate(cases):
            case_name = case.rsplit("::", 1)[1]
            self.assertEqual(located_lines[3 * index], case)
            self.assertRegex(
                located_lines[3 * index + 1],
                rf"^  source: {re.escape(str(source))}:\d+$",
            )
            self.assertEqual(
                located_lines[3 * index + 2],
                f"  snapshot: {snapshots / (case_name + '.yaml')}",
            )


@unittest.skipUnless(os.name == "nt", "native Windows case supervision")
class WindowsTranscriptTests(TranscriptRunnerFixture):
    def test_platform_update_preserves_shared_snapshots_and_prunes_stale_windows_companions(
        self,
    ) -> None:
        shared = (self.snapshots / "selected.yaml").read_bytes()
        self.suite.write_text(
            PUBLIC_SUITE
            + "\nfrom support.snapshots import platform_snapshots\ntest_selected = platform_snapshots('win32', reason='fixture Windows contract')(test_selected)\n"
        )
        stale = self.snapshots / "selected.win32.stale.yaml"
        stale.write_text("---\nold: companion\n...\n")
        result = self.run_runner("--full", "--update")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual((self.snapshots / "selected.yaml").read_bytes(), shared)
        self.assertTrue((self.snapshots / "selected.win32.yaml").exists())
        self.assertFalse(stale.exists())

    def test_deadline_and_owner_loss_retire_blocked_cases_and_descendants(self) -> None:
        import ctypes
        from ctypes import wintypes
        import socket

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for owner_loss in (False, True):
            with (
                self.subTest(owner_loss=owner_loss),
                socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as checkpoint,
            ):
                checkpoint.bind(("127.0.0.1", 0))
                checkpoint.settimeout(10)
                # fmt: python
                self.suite.write_text(
                    code(f"""
                    import ctypes
                    import json
                    import os
                    import socket
                    import subprocess
                    import sys

                    def test_selected(binary):
                        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
                        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as checkpoint:
                            checkpoint.sendto(json.dumps([os.getpid(), child.pid]).encode(), {checkpoint.getsockname()!r})
                        ctypes.PyDLL("kernel32").Sleep(60000)
                        return []
                    """)
                )
                process = self.start_runner(
                    "client_server/server/test_tools::selected", "--timeout", "3"
                )
                try:
                    pids = json.loads(checkpoint.recv(1000))
                    handles = [kernel.OpenProcess(0x100000, False, pid) for pid in pids]
                    self.assertTrue(all(handles))
                    try:
                        if owner_loss:
                            process.kill()
                        stdout, stderr = process.communicate(timeout=25)
                        self.assertNotEqual(process.returncode, 0)
                        if not owner_loss:
                            self.assertIn("timed out after 3 seconds", stderr)
                        for handle in handles:
                            self.assertEqual(
                                kernel.WaitForSingleObject(handle, 5000),
                                0,
                                stdout + stderr,
                            )
                    finally:
                        for handle in handles:
                            kernel.CloseHandle(handle)
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.communicate(timeout=10)

    def test_shared_cases_record_timings_and_report_capability_skips(self) -> None:
        timing = self.root / "timings.jsonl"
        with patch.dict(os.environ, {"MCP_CONSOLE_TEST_TIMINGS": str(timing)}):
            result = self.run_runner("--full", "--jobs", "2")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        records = [json.loads(line) for line in timing.read_text().splitlines()]
        self.assertEqual(len(records), 3, records)
        self.assertEqual(
            {record["status"] for record in records},
            {"passed"},
        )
        self.assertEqual(
            {record["selector"].rsplit("::", 1)[1] for record in records},
            {"initializes_and_lists_tools", "selected", "unselected"},
        )
        self.suite.write_text(
            PUBLIC_SUITE
            + "\nfrom support.requirements import POSIX, requires\ntest_unselected = requires(POSIX)(test_unselected)\n"
        )
        result = self.run_runner("client_server/server/test_tools::unselected")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unselected: skipped; POSIX:", result.stdout)

    def test_failed_shared_case_preserves_its_timing(self) -> None:
        timing = self.root / "timings.jsonl"
        self.suite.write_text(
            PUBLIC_SUITE
            # fmt: python
            + code("""
                def test_selected(binary):
                    raise RuntimeError("timing fixture failure")
                """)
        )
        with patch.dict(os.environ, {"MCP_CONSOLE_TEST_TIMINGS": str(timing)}):
            result = self.run_runner("client_server/server/test_tools::selected")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timing fixture failure", result.stderr)
        records = [json.loads(line) for line in timing.read_text().splitlines()]
        self.assertEqual(len(records), 1, records)
        self.assertEqual(records[0]["status"], "failed")
        self.assertEqual(
            records[0]["selector"], "client_server/server/test_tools::selected"
        )

    def test_deadline_runs_finally_blocks_before_reporting_timeout(self) -> None:
        # fmt: python
        self.suite.write_text(
            code("""
            import time
            from pathlib import Path


            def test_selected(binary):
                try:
                    print("case diagnostic", flush=True)
                    while True:
                        pass
                finally:
                    (binary.parents[2] / "cleaned").touch()
                return []
            """)
        )
        result = self.run_runner(
            "client_server/server/test_tools::selected", "--timeout", "2"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("timed out after 2 seconds", result.stderr)
        self.assertIn("case diagnostic", result.stderr)
        self.assertTrue((self.root / "cleaned").exists(), result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
