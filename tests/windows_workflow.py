"""Public native development-command regressions, without R or a Cargo build."""

import json
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from queue import Queue
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from threading import Thread
from textwrap import dedent
import unittest

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "native Windows development commands")
class WindowsWorkflow(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="console workflow space ")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        shutil.copytree(ROOT / "scripts", self.root / "scripts")
        (self.root / "tests").mkdir()
        shutil.copy2(
            ROOT / "tests/windows_runner.py", self.root / "tests/windows_runner.py"
        )
        self.commands = self.root / "commands"
        self.commands.mkdir()
        self.environment = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith("MCP_CONSOLE_")
        } | {
            "MCP_CONSOLE_HOME": str(self.root / "home"),
            "PATH": str(self.commands) + os.pathsep + os.environ["PATH"],
            "PYTHONUTF8": "1",
        }

    def run_command(self, name, *arguments, environment=None):
        return subprocess.run(
            [sys.executable, self.root / "scripts" / name, *arguments],
            cwd=self.root.parent,
            env=environment or self.environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
        )

    def stub(self, name, source):
        (self.commands / f"{name}.py").write_text(source, encoding="utf-8")
        (self.commands / f"{name}.cmd").write_text(
            f'@echo off\n"{sys.executable}" "%~dp0{name}.py" %*\n',
            encoding="utf-8",
        )

    def write(self, name, source):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(dedent(source).lstrip("\n"), encoding="utf-8")

    def start(self, *arguments):
        process = subprocess.Popen(
            [sys.executable, self.root / "scripts/with-checkout", *arguments],
            cwd=self.root,
            env=self.environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )

        def stop():
            if process.poll() is None:
                process.send_signal(signal.CTRL_BREAK_EVENT)
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=15)
                raise
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()

        self.addCleanup(stop)
        lines = Queue()

        def read():
            for line in process.stdout:
                lines.put(line.strip())
            lines.put(None)

        Thread(target=read, daemon=True).start()
        return process, lines

    def test_nested_packaging_shares_ownership_and_excludes_other_mutators(self):
        shutil.copy2(
            ROOT / "tests/fixtures/windows_build.py", self.root / "build_fixture.py"
        )
        process, lines = self.start(
            sys.executable,
            self.root / "scripts/with-checkout",
            sys.executable,
            self.root / "build_fixture.py",
            "build_wheel",
        )
        self.assertEqual(lines.get(timeout=15), "ready")
        self.assertEqual(lines.get(timeout=15), "building")
        (self.root / "target").mkdir()
        (self.root / "target").rename(self.root / "hidden-target")
        conflict = self.run_command(
            "with-checkout", sys.executable, "-c", "print('must not run')"
        )
        self.assertNotEqual(conflict.returncode, 0)
        self.assertIn("checkout is busy", conflict.stderr)
        self.assertNotIn("must not run", conflict.stdout)
        process.stdin.write("finish\n")
        process.stdin.flush()
        self.assertEqual(lines.get(timeout=15), "fixture.whl")
        self.assertEqual(process.wait(timeout=15), 0, process.stderr.read())
        admitted = self.run_command(
            "with-checkout", sys.executable, "-c", "print('admitted')"
        )
        self.assertEqual(admitted.returncode, 0, admitted.stderr)
        self.assertEqual(admitted.stdout.strip(), "admitted")

    def test_normal_exit_and_cancellation_retire_nested_descendants(self):
        self.write(
            "child.py",
            """
            import os
            import socket
            import sys
            from threading import Event
            print(os.getpid(), flush=True)
            with socket.create_connection(('127.0.0.1', int(sys.argv[1]))) as ready:
                ready.sendall(b'1')
            Event().wait()
        """,
        )
        self.write(
            "parent.py",
            """
            import socket
            import subprocess
            import sys
            with socket.socket() as ready:
                ready.bind(('127.0.0.1', 0))
                ready.listen()
                subprocess.Popen([sys.executable, "child.py", str(ready.getsockname()[1])])
                with ready.accept()[0] as peer:
                    assert peer.recv(1) == b'1'
            sys.stdin.readline()
        """,
        )
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                process, lines = self.start(
                    sys.executable,
                    self.root / "scripts/with-checkout",
                    sys.executable,
                    self.root / "parent.py",
                )
                child = int(lines.get(timeout=15))
                handle = kernel.OpenProcess(0x100000, False, child)
                self.assertTrue(handle)
                try:
                    if cancel:
                        process.send_signal(signal.CTRL_BREAK_EVENT)
                    else:
                        process.stdin.write("finish\n")
                        process.stdin.flush()
                    self.assertEqual(
                        process.wait(timeout=15),
                        149 if cancel else 0,
                        process.stderr.read(),
                    )
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0)
                    self.assertIsNone(lines.get(timeout=15))
                finally:
                    kernel.CloseHandle(handle)
                result = self.run_command("with-checkout", sys.executable, "-c", "pass")
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_check_records_native_scope_failure_and_full_installation(self):
        for name in ("scripts/check-core", "scripts/test", "tests/windows_install.py"):
            self.write(
                name,
                """
                import os
                import sys
                print(repr(sys.argv[1:]))
                if os.environ.get("FAIL_CORE") and sys.argv[0].endswith("check-core"):
                    print("fixture failure", file=sys.stderr)
                    sys.exit(23)
            """,
            )
        for arguments in ((), ("--quick",), ("--full",)):
            result = self.run_command("check", *arguments)
            self.assertEqual(result.returncode, 0, result.stderr)
            records = [
                json.loads(p.read_text())
                for p in (self.root / ".dev-workflow/runs").glob("*/result.json")
            ]
            (record,) = [r for r in records if r["command"] == ["check", *arguments]]
            self.assertEqual(
                [p["name"] for p in record["phases"]],
                [
                    "core",
                    "native-tests",
                    *(["installation"] if arguments == ("--full",) else []),
                ],
            )
            self.assertEqual(record["exit_status"], 0)
            self.assertIsNone(record["revision"])
            for phase in record["phases"][:2]:
                self.assertEqual(
                    Path(phase["log"]).read_text().strip(),
                    "['--full']" if arguments == ("--full",) else "[]",
                )
        result = self.run_command(
            "check", environment=self.environment | {"FAIL_CORE": "1"}
        )
        self.assertEqual(result.returncode, 23, result.stderr)
        self.assertIn("fixture failure", result.stderr)
        failures = [
            json.loads(p.read_text())
            for p in (self.root / ".dev-workflow/runs").glob("*/result.json")
        ]
        (failure,) = [r for r in failures if r["exit_status"] == 23]
        self.assertEqual([p["name"] for p in failure["phases"]], ["core"])

    def test_record_publication_waits_for_a_transient_windows_reader(self):
        self.write(
            "scripts/check-core",
            """
            import json
            import os
            import socket
            with socket.create_connection(('127.0.0.1', int(os.environ['RECORD_READER_PORT']))) as peer:
                peer.sendall((json.dumps(os.environ['MCP_CONSOLE_VALIDATION_RUN']) + '\\n').encode())
                assert peer.recv(1) == b'1'
        """,
        )
        self.write("scripts/test", "pass")
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen()
            listener.settimeout(15)
            self.environment["RECORD_READER_PORT"] = str(listener.getsockname()[1])
            process, _ = self.start(sys.executable, self.root / "scripts/check")
            with listener.accept()[0] as peer:
                peer.settimeout(15)
                with peer.makefile("r") as messages:
                    record = Path(json.loads(messages.readline()))
                # A reader that shares reads/writes but not deletion prevents
                # atomic replacement on Windows, unlike an ordinary Unix read.
                handle = kernel.CreateFileW(
                    str(record), 0x80000000, 3, None, 3, 0x80, None
                )
                self.assertNotEqual(handle, ctypes.c_void_p(-1).value)
                try:
                    peer.sendall(b"1")
                    with self.assertRaises(subprocess.TimeoutExpired):
                        process.wait(timeout=0.3)
                    self.assertEqual(json.loads(record.read_text())["phases"], [])
                finally:
                    kernel.CloseHandle(handle)
            self.assertEqual(process.wait(timeout=15), 0, process.stderr.read())
            result = json.loads(record.read_text())
            self.assertEqual(result["exit_status"], 0)
            self.assertEqual(
                [p["name"] for p in result["phases"]], ["core", "native-tests"]
            )

    def test_native_discovery_and_installed_binary_skip_builds(self):
        for name in (
            "windows.py",
            "windows_runner.py",
            "windows_gate.py",
            "windows_relay.py",
            "windows_cargo.py",
            "windows_resolver.py",
            "windows_mxc.py",
            "windows_sandbox.py",
        ):
            path = self.root / "tests" / name
            path.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "tests" / name, path)
        # Native discovery imports shared helpers and their dependencies. Keep
        # the package intact instead of maintaining a second import inventory.
        shutil.copytree(
            ROOT / "tests/support",
            self.root / "tests/support",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        result = self.run_command("test", "--list")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("WindowsConsole.test_python_without_r", result.stdout)
        self.assertIn("WindowsMxc.test_proxy_launch", result.stdout)
        result = self.run_command(
            "test", "--locate", "WindowsRelay.test_drains_sideband_after_exit"
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("windows_relay.py:", result.stdout)
        for flags in [("--bogus",), ("NoSuchCase",)]:
            result = self.run_command("test", *flags)
            self.assertEqual(result.returncode, 2, result.stderr)
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / ".dev-workflow").exists())
        self.write(
            "tests/windows.py",
            """
            import unittest
            class Fixture(unittest.TestCase):
                def test_installed(self):
                    import os
                    self.assertEqual(os.environ['MCP_CONSOLE_TEST_BINARY'], 'installed.exe')
            if __name__ == '__main__':
                unittest.main()
        """,
        )
        result = self.run_command(
            "test",
            "Fixture.test_installed",
            environment=self.environment | {"MCP_CONSOLE_TEST_BINARY": "installed.exe"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse((self.root / "target").exists())
        (record,) = (self.root / ".dev-workflow/runs").glob("*/result.json")
        self.assertEqual(
            [p["name"] for p in json.loads(record.read_text())["phases"]],
            ["native-tests"],
        )

    def test_wrapped_command_preserves_arguments_streams_and_exit_status(self):
        arguments = ["space here", 'a"b', "x&y", "café", "trailing\\"]
        result = self.run_command(
            "with-checkout",
            sys.executable,
            "-c",
            "import json,sys; print(json.dumps(sys.argv[1:])); "
            "print('diagnostic', file=sys.stderr); sys.exit(7)",
            *arguments,
        )
        self.assertEqual(result.returncode, 7, result.stderr)
        self.assertEqual(json.loads(result.stdout), arguments)
        self.assertEqual(result.stderr.strip(), "diagnostic")

    def test_full_validation_and_boundary_selectors_run_shared_cases(self):
        self.write(
            "tests/windows.py",
            "import unittest\nclass Fixture(unittest.TestCase):\n    def test_native(self): pass\nif __name__ == '__main__': unittest.main()\n",
        )
        self.stub(
            "uv",
            "import json,sys\nfrom pathlib import Path\nPath('shared-arguments.json').write_text(json.dumps(sys.argv[1:]))\n",
        )
        environment = self.environment | {"MCP_CONSOLE_TEST_BINARY": "installed.exe"}
        for arguments, expected in (
            (
                ("client_server/server/test_tools::initializes_and_lists_tools",),
                ["transcripts"],
            ),
            (("--full",), ["native-tests", "transcripts"]),
            (("--stress",), ["transcripts"]),
        ):
            with self.subTest(arguments=arguments):
                result = self.run_command(
                    "checkout_workflow.py", "test", *arguments, environment=environment
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                records = list((self.root / ".dev-workflow/runs").glob("*/result.json"))
                record = max(records, key=lambda path: path.stat().st_mtime_ns)
                self.assertEqual(
                    [
                        phase["name"]
                        for phase in json.loads(record.read_text())["phases"]
                    ],
                    expected,
                )
                self.assertEqual(
                    json.loads((self.root / "shared-arguments.json").read_text())[
                        -len(arguments) :
                    ],
                    list(arguments),
                )
        self.assertFalse((self.root / "target").exists())

    def test_cmd_launchers_work_from_a_different_directory(self):
        for name in (
            "preflight",
            "test",
            "format",
            "review-diff",
            "stage-sandbox-runner",
            "prune-r-cache",
            "check",
            "check-core",
        ):
            with self.subTest(command=name):
                result = subprocess.run(
                    [str(self.root / "scripts" / f"{name}.cmd"), "--help"],
                    cwd=self.root.parent,
                    env=self.environment,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertEqual(
                    result.returncode,
                    2 if name in {"check", "check-core"} else 0,
                    result.stderr,
                )
                self.assertIn("usage:", result.stdout + result.stderr)
        self.write(
            "arguments.py",
            "import json,sys; print(json.dumps(sys.argv[1:])); sys.exit(17)",
        )
        result = subprocess.run(
            [
                str(self.root / "scripts/with-checkout.cmd"),
                sys.executable,
                str(self.root / "arguments.py"),
                "space here",
            ],
            cwd=self.root.parent,
            env=self.environment,
            capture_output=True,
            text=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 17, result.stderr)
        self.assertEqual(json.loads(result.stdout), ["space here"])

    def test_format_attempts_every_tool_and_reports_missing_tools(self):
        for name in ("ruff", "yamark", "cargo", "air"):
            self.stub(name, f"raise SystemExit({7 if name == 'yamark' else 0})")
        for flags, status in [((), 0), (("--strict",), 1)]:
            result = self.run_command("format", *flags)
            self.assertEqual(result.returncode, status, result.stderr)
            self.assertEqual(
                result.stdout.splitlines(),
                ["ruff: ok", "yamark: failed (7)", "rustfmt: ok", "air: ok"],
            )
        (self.commands / "air.cmd").unlink()
        result = self.run_command(
            "format",
            "--strict",
            environment=self.environment | {"PATH": str(self.commands)},
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("air: failed (127)", result.stdout)
        self.assertIn("rustfmt: ok", result.stdout)

    def test_preflight_skips_unimplemented_platform_capabilities(self):
        for name in ("git", "uv", "cargo", "rustup"):
            self.stub(name, f"print('{name} fixture')")
        environment = self.environment | {"PATH": str(self.commands)}
        environment.pop("R_HOME", None)
        result = self.run_command("preflight", "--json", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["required_missing"], [])
        self.assertEqual(report["companion"]["status"], "skip")
        self.assertTrue(report["executable"]["development"].endswith("mcp-console.exe"))
        self.assertNotIn("providers", report)
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / ".dev-workflow").exists())
        self.stub(
            "cargo", "import sys; print('broken cargo', file=sys.stderr); sys.exit(9)"
        )
        result = self.run_command("preflight", "--json", environment=environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("cargo", json.loads(result.stdout)["required_missing"])

    def test_preflight_optional_r_version_errors_do_not_fail(self):
        for name in ("git", "uv", "cargo", "rustup"):
            self.stub(name, f"print('{name} fixture')")
        for name in ("R", "Rscript"):
            self.stub(
                name,
                f"import sys\nprint('broken {name}', file=sys.stderr)\nsys.exit(9)\n",
            )
        for r_home in (None, str(self.root / "missing-R")):
            with self.subTest(r_home=r_home):
                environment = self.environment | {"PATH": str(self.commands)}
                environment.pop("R_HOME", None)
                if r_home:
                    environment["R_HOME"] = r_home
                result = self.run_command(
                    "preflight", "--json", environment=environment
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(report["required_missing"], [])
                for name in ("R", "Rscript"):
                    tool = report["tools"][name]
                    self.assertFalse(tool["required"])
                    self.assertIsNone(tool["version"])
                    self.assertTrue(tool["error"])
                    self.assertEqual(
                        tool["error"], report["probe_errors"][f"{name}_version"]
                    )

    def test_preflight_optional_r_home_error_keeps_required_probes_fatal(self):
        for name in ("git", "uv", "cargo", "rustup", "Rscript"):
            self.stub(name, f"print('{name} fixture')")
        self.stub(
            "R",
            dedent("""
                import sys
                if sys.argv[1:] == ["--version"]:
                    print("R fixture")
                else:
                    print("broken R home", file=sys.stderr)
                    sys.exit(9)
                """),
        )
        environment = self.environment | {"PATH": str(self.commands)}
        environment.pop("R_HOME", None)
        result = self.run_command("preflight", "--json", environment=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertIsNone(report["runtime"]["r_home"])
        self.assertIn("broken R home", report["probe_errors"]["r_home"])
        self.stub(
            "uv",
            dedent("""
                import sys
                if sys.argv[1:] == ["--version"]:
                    print("uv fixture")
                else:
                    print("broken cache", file=sys.stderr)
                    sys.exit(9)
                """),
        )
        result = self.run_command("preflight", "--json", environment=environment)
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["required_missing"], [])
        self.assertIn("broken cache", report["probe_errors"]["uv_cache"])

    def test_check_rejects_invalid_flags_without_creating_build_state(self):
        result = self.run_command("check", "--bogus")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("accepts --full or --quick", result.stderr)
        self.assertFalse((self.root / "target").exists())


if __name__ == "__main__":
    unittest.main()
