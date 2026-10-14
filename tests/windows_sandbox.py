"""Public Windows configuration and explicitly provisioned sandbox acceptance."""

import ctypes
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import contextmanager
from ctypes import wintypes
from pathlib import Path
from textwrap import dedent
from support.installation import native_console
from support.normalization import code
from support.public_configuration import INVALID_CONFIGURATIONS

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@contextmanager
def workspace(parent: Path | None = None):
    # Inherit the user's ordinary ACLs. Python 3.14's private temp directories
    # allow only owner/admin/system, which a restricted token cannot traverse.
    root = (
        parent or Path(tempfile.gettempdir())
    ) / f"console sandbox {uuid.uuid4().hex}"
    root.mkdir()
    try:
        yield root
    finally:
        # Windows can retain an executable's image mapping briefly after its
        # waited-for process exits. Retry only that sharing violation.
        deadline = time.monotonic() + 5
        while True:
            try:
                shutil.rmtree(root)
                break
            except OSError as error:
                if error.winerror != 32 or time.monotonic() >= deadline:
                    raise
                time.sleep(0.05)


@unittest.skipUnless(os.name == "nt", "native Windows sandbox")
class WindowsSandbox(unittest.TestCase):
    def sandbox_state_directory(self):
        command = [str(BINARY), "sandbox-setup", "--status"]
        if state := os.environ.get("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR"):
            command.extend(["--state-dir", state])
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertIn(result.returncode, (0, 1), (result.stdout, result.stderr))
        status = json.loads(result.stdout)
        self.assertTrue(
            status["configured"],
            f"Windows sandbox is not provisioned at {status['state_dir']}; "
            f'run mcp-console sandbox-setup --state-dir "{status["state_dir"]}"',
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return status["state_dir"]

    def test_unconfigured_acceptance_fails_without_provisioning_opt_in(self):
        with workspace() as root:
            environment = dict(
                os.environ,
                LOCALAPPDATA=str(root / "local"),
                MCP_CONSOLE_HOME=str(root / "home"),
                MCP_CONSOLE_TEST_BINARY=str(BINARY),
            )
            environment.pop("MCP_CONSOLE_TEST_WINDOWS_STATE_DIR", None)
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "tests/windows.py",
                    "-v",
                    "WindowsSandbox.test_current_setup_reports_resources_without_reprovisioning",
                ],
                cwd=ROOT,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("skipped", result.stderr)
            self.assertIn("Windows sandbox is not provisioned", result.stderr)
            self.assertIn("mcp-console sandbox-setup", result.stderr)
            self.assertFalse((root / "local/mcp-console").exists())

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_native_r_startup_is_contained_and_reread_on_restart(self):
        from windows import Session

        with workspace() as root:
            home = root / "home"
            home.mkdir()
            environ = home / ".Renviron"
            environ.write_text("CONSOLE_NATIVE_STARTUP=first\n")
            profile = home / ".Rprofile"
            profile.write_text(
                # fmt: r
                code("""
                    native_value <- Sys.getenv("CONSOLE_NATIVE_STARTUP")
                    native_allowed <- file.path(tempdir(), "native-allowed")
                    writeLines(native_value, native_allowed)
                    native_blocked <- tryCatch(
                      {
                        suppressWarnings(writeLines(
                          "forbidden",
                          Sys.getenv("CONSOLE_NATIVE_FORBIDDEN")
                        ))
                        FALSE
                      },
                      error = function(error) TRUE
                    )
                    stopifnot(native_blocked)
                    options(width = 73L)
                    .First <- function() cat("native Windows startup\\n")
                    """)
            )
            environment = dict(
                os.environ,
                HOME=str(home),
                R_USER=str(home),
                R_ENVIRON=os.devnull,
                R_PROFILE=os.devnull,
                R_ENVIRON_USER=str(environ),
                R_PROFILE_USER=str(profile),
                MCP_CONSOLE_LANGUAGES="r",
                CONSOLE_NATIVE_FORBIDDEN=str(root / "forbidden"),
            )
            session = Session(environment, sandbox=True, use_r_startup_files=True)
            try:
                session.initialize()
                # fmt: r
                check = code("""
                    stopifnot(
                      native_blocked,
                      file.exists(native_allowed),
                      identical(getOption("width"), 73L),
                      identical(readLines(native_allowed), native_value)
                    )
                    cat(native_value)
                    """)
                self.assertIn("first", json.dumps(session.send(r=check)))
                self.assertFalse((root / "forbidden").exists())
                environ.write_text("CONSOLE_NATIVE_STARTUP=edited\n")
                result = session.send(control="restart", r=check)
                self.assertIn("edited", json.dumps(result))
                self.assertFalse((root / "forbidden").exists())
            finally:
                session.close()

    @unittest.skipUnless(shutil.which("uv"), "requires uv")
    def test_console_cache_paths_reach_resolver_and_worker(self):
        self.console_cache_paths(profile_fallback=False)

    @unittest.skipUnless(shutil.which("uv"), "requires uv")
    def test_console_cache_falls_back_to_userprofile_after_relative_home(self):
        self.console_cache_paths(profile_fallback=True)

    def console_cache_paths(self, *, profile_fallback: bool):
        from windows import Session
        from support.resolvers import preseeded_duckdb_python

        with workspace() as root:
            python = preseeded_duckdb_python(root)
            # Probe trusted preparation; selected environment startup hooks
            # execute under worker permissions rather than resolver grants.
            subprocess.run(
                [
                    "rustc",
                    str(ROOT / "tests/fixtures/windows_resolver.rs"),
                    "-o",
                    str(root / "uv.exe"),
                ],
                check=True,
            )
            # Hosted temp directories may exclude the sandbox account. Grant
            # reads for the selected venv, session cwd, and host-written probe.
            status = json.loads(
                subprocess.check_output(
                    [BINARY, "sandbox-setup", "--status"], text=True
                )
            )
            self.assertTrue(status["configured"], status)
            subprocess.run(
                [
                    "icacls",
                    str(root),
                    "/grant",
                    f"{status['online_account']}:(OI)(CI)RX",
                ],
                check=True,
                capture_output=True,
            )
            local = (
                root / "account/AppData/Local" if profile_fallback else root / "local"
            )
            cache = local / "mcp-console/cache/dependencies"
            environment = dict(
                os.environ,
                PATH=str(root),
                LOCALAPPDATA=str(local),
                UV_CACHE_DIR=str(root / "host-uv"),
                IR_CACHE_DIR=str(root / "host-ir"),
                CACHE_TEST_ROOT=str(cache),
                TEST_RESOLVER_CACHE_PROBE="1",
                TEST_RESOLVER_RECORD=str(cache / "resolver-commands.jsonl"),
                TEST_RESOLVER_PYTHON=str(python),
            )
            for name in (
                "XDG_CACHE_HOME",
                "R_HOME",
                "RETICULATE_PYTHON",
                "RETICULATE_UV",
            ):
                environment.pop(name, None)
            if profile_fallback:
                environment.pop("LOCALAPPDATA")
                environment.update(
                    HOME="relative-home", USERPROFILE=str(root / "account")
                )
            # Keep the cwd under the explicit read grant too. The native
            # runner's background read-ACL traversal can still be in progress.
            session = Session(
                os.environ,
                sandbox=True,
                temporary_root=root,
                overrides=[
                    'python={"managed":{}}',
                    'sandbox.network="enabled"',
                    "inherit_environment=false",
                    "environment=" + json.dumps(environment),
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    python=dedent("""
                        import os
                        from pathlib import Path

                        root = Path(os.environ["CACHE_TEST_ROOT"])
                        for name in ("UV_CACHE_DIR", "IR_CACHE_DIR", "R_USER_CACHE_DIR", "RENV_PATHS_CACHE", "PYTHONUSERBASE"):
                            assert Path(os.environ[name]).is_relative_to(root)
                        assert Path(os.environ["UV_CACHE_DIR"]).joinpath("resolver-probe").read_text() == "prepared"
                        print("Console caches retained")
                        """)
                )
                self.assertIn("Console caches retained", json.dumps(result), result)
            finally:
                session.close()
            self.assertFalse((root / "host-uv").exists())
            self.assertFalse((root / "host-ir").exists())

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_network_enabled_sql(self):
        from windows import Session, exercise_r_sql

        session = Session(sandbox=True, overrides=['sandbox.network="enabled"'])
        try:
            session.initialize()
            exercise_r_sql(session)
        finally:
            session.close()

    def test_network_enabled_python_sql(self):
        from windows import Session, exercise_sql_interrupt
        from support.resolvers import expose_uv

        with workspace() as root:
            expose_uv(root)
            environment = dict(
                os.environ,
                PATH=os.pathsep.join(
                    [
                        str(root),
                        str(Path(sys.executable).parent),
                        str(Path(os.environ["SystemRoot"]) / "System32"),
                    ]
                ),
                RETICULATE_PYTHON="managed",
                UV_PYTHON_PREFERENCE="system",
                UV_PYTHON_DOWNLOADS="never",
            )
            environment.pop("R_HOME", None)
            session = Session(
                environment,
                sandbox=True,
                overrides=['sandbox.network="enabled"'],
            )
            try:
                session.initialize()
                session.request("tools/list", {})
                session.expect("1234567", sql="SELECT 1234567 AS answer")
                session.expect(
                    "Count", sql="CREATE TABLE retained AS SELECT 1234567 AS answer"
                )
                exercise_sql_interrupt(session)
                session.expect(
                    "1234567", control="restart", sql="SELECT 1234567 AS answer"
                )
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_network_enabled_input_and_interrupt(self):
        from windows import Session, exercise_input_and_interrupt

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                exercise_input_and_interrupt(session)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_network_enabled_idle_later_callbacks(self):
        from windows import Session, exercise_later_callbacks

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                exercise_later_callbacks(session)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_provisioned_input_and_interrupt_with_private_installation(self):
        state = self.sandbox_state_directory()
        # A private bundle outside the user's profile models hosted CI's
        # nonstandard installation paths, without relying on another drive.
        public = Path(os.environ["PUBLIC"]).resolve()
        with tempfile.TemporaryDirectory(
            prefix="console-ci-assets-", dir=public
        ) as directory:
            prefix = Path(directory).resolve()
            self.assertTrue(prefix.is_relative_to(public))
            owner = f"{os.environ['USERDOMAIN']}\\{os.environ['USERNAME']}"
            subprocess.run(
                [
                    "icacls",
                    str(prefix),
                    "/inheritance:r",
                    "/grant:r",
                    f"{owner}:(OI)(CI)F",
                    "*S-1-5-18:(OI)(CI)F",
                    "*S-1-5-32-544:(OI)(CI)F",
                ],
                check=True,
                capture_output=True,
            )
            source = native_console(BINARY)
            (prefix / "bin").mkdir()
            binary = prefix / "bin/mcp-console.exe"
            shutil.copy2(source, binary)
            for relative in ("libexec", "share"):
                assets = source.resolve().parent.parent / relative
                if assets.exists():
                    shutil.copytree(assets, prefix / relative)
            prepared = subprocess.run(
                [
                    sys.executable,
                    ROOT / "scripts/prepare-windows-tests",
                    "--binary",
                    binary,
                    "--state-dir",
                    state,
                    "--github-env",
                    prefix / "github-env",
                    "--read-root",
                    prefix,
                    "--require-configured",
                ],
                cwd=ROOT,
                capture_output=True,
                timeout=60,
            )
            self.assertEqual(prepared.returncode, 0, (prepared.stdout, prepared.stderr))
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "tests/windows.py",
                    "WindowsSandbox.test_provisioned_network_restricted_input_and_interrupt",
                ],
                cwd=ROOT,
                env=os.environ
                | {
                    "MCP_CONSOLE_TEST_BINARY": str(binary),
                    "MCP_CONSOLE_TEST_NATIVE_BINARY": str(binary),
                },
                capture_output=True,
                timeout=240,
            )
            self.assertEqual(result.returncode, 0, (result.stdout, result.stderr))

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_provisioned_network_restricted_input_and_interrupt(self):
        from windows import Session, exercise_input_and_interrupt

        session = Session(
            sandbox=True,
            overrides=[
                'sandbox.network="restricted"',
            ],
        )
        try:
            session.initialize()
            exercise_input_and_interrupt(session)
        finally:
            session.close()

    def test_network_enabled_allows_loopback_exchange(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                # Complete runtime startup before starting the networking deadline.
                session.expect("[done]", python="pass")
                # This socket tests networking itself; ordinary sequencing uses
                # public stdin or host-only named pipes.
                with socket.create_server(("127.0.0.1", 0)) as listener:
                    listener.settimeout(10)
                    session.send(
                        # fmt: python
                        python=dedent(f"""
                            import socket

                            with socket.create_connection(
                                ("127.0.0.1", {listener.getsockname()[1]}), timeout=10
                            ) as peer:
                                peer.sendall(b"loopback request")
                                with peer.makefile("rb") as response:
                                    print(response.read().decode())
                            """),
                        timeout_ms=0,
                    )
                    with listener.accept()[0] as peer:
                        peer.settimeout(10)
                        request = peer.makefile("rb")
                        with request:
                            self.assertEqual(request.read(16), b"loopback request")
                        peer.sendall(b"loopback reply")
                    result = session.expect("loopback reply")
                    self.assertFalse(result.get("isError"), result)
            finally:
                session.close()

    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    def test_r_uses_private_storage_and_preserves_state(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    r=dedent("""
                        value <- 40L
                        path <- file.path(tempdir(), "allowed.txt")
                        writeLines("private", path)
                        cat(value + 2L, readLines(path))
                        """)
                )
                self.assertIn("42 private", json.dumps(result), result)
                result = session.send(r="value + 3L")
                self.assertIn("43", json.dumps(result))
                result = session.send(control="restart", r='exists("value")')
                self.assertIn("FALSE", json.dumps(result))
            finally:
                session.close()

    def test_setup_help_describes_provisioned_resources(self):
        for flag in ("--help", "-h"):
            with self.subTest(flag=flag):
                result = subprocess.run(
                    [str(BINARY), "sandbox-setup", flag],
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                for detail in (
                    "McpConsoleSandboxOff",
                    "McpConsoleSandboxOn",
                    "ConsoleSandboxUsers",
                    "Windows Firewall",
                    "loopback",
                    ".sandbox-secrets",
                    ".sandbox-bin",
                    "UAC",
                    "reused without changes",
                    "%LOCALAPPDATA%",
                ):
                    self.assertIn(detail, result.stdout)

    def test_current_setup_reports_resources_without_reprovisioning(self):
        state = self.sandbox_state_directory()
        command = [str(BINARY), "sandbox-setup", "--state-dir", state]
        before = subprocess.run(
            [*command, "--status"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(before.returncode, 0, before.stderr)
        self.assertTrue(json.loads(before.stdout)["configured"])
        # Refuse to provision an unconfigured machine in acceptance tests.
        # A current setup must reuse its records and credentials without UAC.
        # Runtime logs can append during other sessions; they are not setup records.
        records = {
            path: path.read_bytes()
            for directory in (".sandbox", ".sandbox-secrets")
            for path in (Path(state) / directory).rglob("*")
            if path.is_file()
            and not (directory == ".sandbox" and path.match("sandbox.*.log"))
        }
        result = subprocess.run(command, capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"Console sandbox is configured at {state}", result.stdout)
        for detail in (
            "McpConsoleSandboxOff",
            "McpConsoleSandboxOn",
            "ConsoleSandboxUsers",
            "Windows Firewall",
            "loopback",
            ".sandbox-secrets",
            ".sandbox-bin",
            "reused without changes",
        ):
            self.assertIn(detail, result.stdout)
        for path, content in records.items():
            self.assertEqual(path.read_bytes(), content, str(path))
        after = subprocess.run(
            [*command, "--status"], capture_output=True, text=True, timeout=30
        )
        self.assertEqual(after.returncode, 0, after.stderr)
        self.assertEqual(json.loads(after.stdout), json.loads(before.stdout))

    def test_setup_status_is_read_only(self):
        with workspace() as root:
            state = root / "state"
            result = subprocess.run(
                [str(BINARY), "sandbox-setup", "--status", "--state-dir", str(state)],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            status = json.loads(result.stdout)
            self.assertFalse(status["configured"])
            self.assertTrue(status["helpers_available"])
            self.assertFalse(state.exists())

    def test_unconfigured_elevated_sandbox_requires_explicit_setup(self):
        with workspace() as root:
            policy = {
                "extends": ":read-only",
                "windows_backend": "elevated",
                "windows_state_dir": str(root / "state"),
            }
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    "cmd.exe",
                    "/d",
                    "/c",
                    "echo launched",
                ],
                cwd=root,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(policy),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(b"Windows sandbox setup is required", result.stderr)
            self.assertIn(b"mcp-console-sandbox setup", result.stderr)
            self.assertFalse((root / "state").exists())

    def test_modified_helper_is_rejected_before_setup(self):
        with workspace() as root:
            native = native_console(BINARY)
            source = native.resolve().parent.parent
            (root / "bin").mkdir()
            shutil.copy2(native, root / "bin/mcp-console.exe")
            for directory in ("libexec", "share"):
                if (source / directory).exists():
                    shutil.copytree(source / directory, root / directory)
            helpers = list((root / "libexec").rglob("mcp-console-sandbox-runner.exe"))
            self.assertTrue(helpers)
            for helper in helpers:
                with helper.open("ab") as stream:
                    stream.write(b"modified")
            result = subprocess.run(
                [str(root / "bin/mcp-console.exe"), "sandbox-setup", "--status"],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(
                b"private artifact does not match this installation", result.stderr
            )

    def test_first_python_send_from_short_profile_directory(self):
        from windows import Session

        profile = Path(os.environ["USERPROFILE"])
        short_name = ctypes.create_unicode_buffer(32768)
        get_short_name = ctypes.windll.kernel32.GetShortPathNameW
        get_short_name.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD]
        get_short_name.restype = wintypes.DWORD
        length = get_short_name(str(profile), short_name, len(short_name))
        self.assertGreater(length, 0, ctypes.WinError())
        self.assertLess(length, len(short_name))
        if short_name.value.casefold() == str(profile).casefold():
            self.skipTest("user profile has no 8.3 alias")
        temporary = Path(short_name.value) / "AppData/Local/Temp"
        session = Session(
            sandbox=True,
            temporary_root=temporary,
            overrides=["r=null", 'sandbox.network="enabled"'],
        )
        try:
            session.initialize()
            result = session.send(python="print(6 * 7)")
            self.assertFalse(result.get("isError"), result)
            self.assertIn("42", json.dumps(result))
        except Exception:
            # Keep the failed cold startup's journal before fixture cleanup.
            recordings = Path(session.directory.name) / ".agents/console/sessions"
            for recording in sorted(recordings.glob("*")):
                for relative in ("internal/events.jsonl", "outputs/session.log"):
                    path = recording / relative
                    if path.is_file():
                        sys.stderr.write(
                            f"\n{path}\n{path.read_text(encoding='utf-8')}\n"
                        )
            raise
        finally:
            session.close()

    def test_python_state_and_restart_with_private_temporary_storage(self):
        from windows import Session

        with workspace() as root:
            session = Session(
                sandbox=True,
                overrides=[
                    'sandbox.network="enabled"',
                ],
            )
            try:
                session.initialize()
                result = session.send(
                    python=dedent("""
                        import os, tempfile
                        answer = 41
                        print(answer + 1, os.environ['MCP_CONSOLE_SANDBOX'])
                        print(tempfile.gettempdir())
                        """)
                )
                self.assertNotIn('"isError": true', json.dumps(result), result)
                self.assertIn("42 1", json.dumps(result))
                result = session.send(
                    python=dedent("""
                    print(answer + 2)
                    open('denied.txt', 'w')
                    """)
                )
                self.assertIn("43", json.dumps(result))
                self.assertIn("PermissionError", json.dumps(result))
                self.assertFalse((Path(session.directory.name) / "denied.txt").exists())
                result = session.send(
                    python=dedent("""
                        import subprocess, sys
                        child = subprocess.Popen(
                            [sys.executable, '-c', 'import time; time.sleep(120)'],
                            stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
                        )
                        print('DESCENDANT', child.pid)
                        """)
                )
                import re

                pid = int(re.search(r"DESCENDANT (\d+)", json.dumps(result)).group(1))
                kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                kernel.OpenProcess.argtypes = [
                    wintypes.DWORD,
                    wintypes.BOOL,
                    wintypes.DWORD,
                ]
                kernel.OpenProcess.restype = wintypes.HANDLE
                kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
                kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                handle = kernel.OpenProcess(0x100000, False, pid)
                self.assertTrue(handle)
                try:
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                    result = session.send(
                        control="restart", python="print('answer' in globals())"
                    )
                    self.assertIn("False", json.dumps(result))
                    self.assertEqual(
                        kernel.WaitForSingleObject(handle, 0),
                        0,
                        "restart preceded descendant retirement",
                    )
                finally:
                    kernel.CloseHandle(handle)
            finally:
                session.close()

    def test_shared_public_policies(self):
        from support.public_configuration import CONFIGURATION_EXAMPLES

        with workspace() as root:
            for name in ("data", "secrets"):
                (root / name).mkdir()
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            for name, document in CONFIGURATION_EXAMPLES.items():
                with self.subTest(name=name):
                    config.write_text(json.dumps(document))
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "--",
                            sys.executable,
                            "-c",
                            "print('launched')",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    if isinstance(document["sandbox"].get("network"), dict):
                        self.assertEqual(result.returncode, 1)
                        self.assertEqual(result.stdout, "")
                        self.assertIn(
                            "requires allow_local_binding: true", result.stderr
                        )
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout.strip(), "launched")

    def test_mxc_proxy_configuration_round_trips(self):
        with workspace() as root:
            for proxy in ({}, {"socks5": "disabled"}, {"mode": "limited"}):
                with self.subTest(proxy=proxy):
                    network = {"proxy": proxy, "allow_local_binding": True}
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "config",
                            "resolve",
                            "--no-config",
                            "--full",
                            "--format",
                            "json",
                            "-c",
                            "sandbox.network=" + json.dumps(network),
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    settings = json.loads(result.stdout)
                    selected = settings["sandbox"]["network"]
                    self.assertIs(selected["allow_local_binding"], True)
                    self.assertEqual(
                        selected["proxy"]["mode"], proxy.get("mode", "full")
                    )
                    self.assertEqual(selected["sockets"]["unix_sockets"], [])
                    if selected["proxy"]["mode"] == "full":
                        self.assertEqual(
                            selected["proxy"]["socks5"], proxy.get("socks5", "tcp")
                        )
                    self.assertIsNone(settings["resolver"]["sandbox"])
                    config = root / "resolved.json"
                    config.write_text(result.stdout, encoding="utf-8")
                    again = subprocess.run(
                        [
                            str(BINARY),
                            "config",
                            "resolve",
                            "--config-file",
                            str(config),
                            "--full",
                            "--format",
                            "json",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(again.returncode, 0, again.stderr)
                    self.assertEqual(json.loads(again.stdout), settings)

    def test_rejects_unsupported_public_permissions(self):
        with workspace() as root:
            for path, value in INVALID_CONFIGURATIONS:
                with self.subTest(path=path):
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "-c",
                            path + "=" + json.dumps(value),
                            "--",
                            "unused",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("configuration", result.stderr)
                    self.assertNotIn("secret sentinel", result.stderr)
                    self.assertNotIn("654987123", result.stderr)
            for override, diagnostic in (
                ("sandbox.network={proxy: {}}", "requires allow_local_binding: true"),
                (
                    "sandbox.network={proxy: {}, allow_local_binding: false}",
                    "requires allow_local_binding: true",
                ),
                (
                    "sandbox.network={proxy: {socks5: tcp_udp}, allow_local_binding: true}",
                    "SOCKS5 UDP is not supported on Windows",
                ),
                (
                    "sandbox.network={proxy: {}, allow_local_binding: true, sockets: {unix_sockets: dangerously_allow_all}}",
                    "Unix sockets are not supported on Windows",
                ),
                (
                    "sandbox.network={proxy: {}, allow_local_binding: true, sockets: {unix_sockets: [C:/service.sock]}}",
                    "Unix sockets are not supported on Windows",
                ),
                ("resolver.sandbox.network=restricted", "resolver.sandbox"),
                ("resolver.sandbox.filesystem={}", "resolver.sandbox"),
                ("sandbox.windows_sandbox_level=unelevated", "windows_sandbox_level"),
                ("sandbox.windows_state_dir=/tmp/state", "windows_state_dir"),
            ):
                with self.subTest(override=override):
                    result = subprocess.run(
                        [
                            str(BINARY),
                            "sandbox",
                            "-c",
                            override,
                            "--",
                            "cmd.exe",
                            "/c",
                            "echo launched",
                        ],
                        cwd=root,
                        env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                        capture_output=True,
                        text=True,
                        timeout=30,
                    )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(diagnostic, result.stderr)

    def test_elevated_cwd_reuses_existing_metadata_access(self):
        # Authenticated Users can read, while OWNER RIGHTS denies ACL writes.
        self.elevated_cwd_acl(
            "D:P(D;;WD;;;OW)(A;;GRGX;;;AU)(A;;RC;;;OW)",
            allowed=True,
        )

    def test_elevated_cwd_rejects_a_blocked_metadata_grant(self):
        # The existing Guests deny must not be overridden by a fresh grant.
        self.elevated_cwd_acl(
            "D:P(D;;GR;;;BG)(A;;FA;;;OW)(A;;FA;;;SY)(A;;FA;;;BA)",
            allowed=False,
        )

    def test_elevated_cwd_rejects_denied_metadata_with_existing_allow(self):
        # Everyone's deny applies despite Authenticated Users' read allow.
        self.elevated_cwd_acl(
            "D:P(D;;0x80;;;WD)(A;;GRGX;;;AU)(A;;FA;;;OW)",
            allowed=False,
        )

    def test_elevated_cwd_reuses_null_dacl(self):
        # A NULL DACL already grants access without another metadata grant.
        self.elevated_cwd_acl("D:NO_ACCESS_CONTROL", allowed=True)

    def test_elevated_cwd_rejects_account_metadata_denies(self):
        for account in ("McpConsoleSandboxOff", "McpConsoleSandboxOn"):
            with self.subTest(account=account):
                sid = self.account_sid(account)
                self.elevated_cwd_acl(
                    f"D:P(D;;0x80;;;{sid})(A;;GRGX;;;AU)(A;;FA;;;OW)",
                    allowed=False,
                )

    def test_elevated_cwd_preserves_a_contended_read_grant(self):
        self.elevated_cwd_acl("D:P(A;;FA;;;OW)", allowed=True, read_acl_checkpoint=True)

    def account_sid(self, account: str) -> str:
        security = ctypes.WinDLL("advapi32", use_last_error=True)
        security.LookupAccountNameW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPCWSTR,
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.POINTER(wintypes.DWORD),
        ]
        sid_size, domain_size, kind = (
            wintypes.DWORD(),
            wintypes.DWORD(),
            wintypes.DWORD(),
        )
        self.assertFalse(
            security.LookupAccountNameW(
                None,
                account,
                None,
                ctypes.byref(sid_size),
                None,
                ctypes.byref(domain_size),
                ctypes.byref(kind),
            )
        )
        self.assertEqual(ctypes.get_last_error(), 122)
        sid = ctypes.create_string_buffer(sid_size.value)
        domain = ctypes.create_unicode_buffer(domain_size.value)
        self.assertTrue(
            security.LookupAccountNameW(
                None,
                account,
                sid,
                ctypes.byref(sid_size),
                domain,
                ctypes.byref(domain_size),
                ctypes.byref(kind),
            ),
            ctypes.WinError(),
        )
        security.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        text = wintypes.LPWSTR()
        self.assertTrue(
            security.ConvertSidToStringSidW(sid, ctypes.byref(text)), ctypes.WinError()
        )
        try:
            return text.value
        finally:
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.LocalFree.argtypes = [ctypes.c_void_p]
            kernel.LocalFree(ctypes.cast(text, ctypes.c_void_p))

    def run_with_read_acl_checkpoint(
        self, command, options, work, handle, state, kernel, security
    ):
        pointer = ctypes.c_void_p

        def dacl_bytes():
            acl, descriptor = pointer(), pointer()
            status = security.GetSecurityInfo(
                handle,
                1,
                4,
                None,
                None,
                ctypes.byref(acl),
                None,
                ctypes.byref(descriptor),
            )
            try:
                self.assertEqual(status, 0, ctypes.WinError(status))
                size = int.from_bytes(ctypes.string_at(acl, 8)[2:4], "little")
                return ctypes.string_at(acl, size)
            finally:
                kernel.LocalFree(descriptor)

        kernel.CreateMutexW.argtypes = [pointer, wintypes.BOOL, wintypes.LPCWSTR]
        kernel.CreateMutexW.restype = wintypes.HANDLE
        kernel.ReleaseMutex.argtypes = [wintypes.HANDLE]
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        mutex = kernel.CreateMutexW(None, False, r"Local\ConsoleSandboxReadAcl")
        self.assertTrue(mutex, ctypes.WinError())
        ownership = kernel.WaitForSingleObject(mutex, 30_000)
        if ownership not in (0, 0x80):
            kernel.CloseHandle(mutex)
            self.fail(f"read ACL checkpoint could not acquire the mutex: {ownership}")
        process, grant = None, pointer()
        try:
            before = dacl_bytes()
            logs = Path(state) / ".sandbox"
            offsets = {
                path: len(path.read_bytes()) for path in logs.glob("sandbox*.log")
            }
            options = options.copy()
            options.pop("capture_output")
            process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **options
            )
            checkpoint = f"waiting for read ACL updates before granting directory metadata access to {work}"
            deadline = time.monotonic() + 10
            while True:
                activity = b"\n".join(
                    path.read_bytes()[offsets.get(path, 0) :]
                    for path in logs.glob("sandbox*.log")
                )
                if (
                    checkpoint.encode() in activity
                    or b"read ACL helper already running; skipping spawn" in activity
                ):
                    break
                self.assertIsNone(
                    process.poll(), "launch exited before the ACL checkpoint"
                )
                self.assertLess(time.monotonic(), deadline, activity)
                time.sleep(0.01)
            self.assertEqual(
                dacl_bytes(), before, "metadata grant raced the read ACL helper"
            )
            # Simulate the preceding helper's full read grant while it owns the
            # mutex. The waiting launch must re-read it before a metadata write.
            self.assertTrue(
                security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    "D:P(A;;GRGX;;;AU)(A;;FA;;;OW)",
                    1,
                    ctypes.byref(grant),
                    None,
                ),
                ctypes.WinError(),
            )
            self.assertTrue(
                security.SetFileSecurityW(str(work), 4, grant), ctypes.WinError()
            )
            complete = dacl_bytes()
            self.assertTrue(kernel.ReleaseMutex(mutex), ctypes.WinError())
            kernel.CloseHandle(mutex)
            mutex = None
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual(
                dacl_bytes(), complete, "metadata setup lost the completed read grant"
            )
            return subprocess.CompletedProcess(
                command, process.returncode, stdout, stderr
            )
        finally:
            if mutex:
                kernel.ReleaseMutex(mutex)
                kernel.CloseHandle(mutex)
            if process is not None and process.poll() is None:
                process.communicate(timeout=30)
            if grant.value:
                kernel.LocalFree(grant)

    def elevated_cwd_acl(
        self, descriptor: str, *, allowed: bool, read_acl_checkpoint: bool = False
    ):
        state = self.sandbox_state_directory()
        with workspace() as root:
            work = root / "read-only-cwd"
            work.mkdir()
            # The native setup log uses long names even when TEMP has an 8.3 alias.
            work = work.resolve(strict=True)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            security = ctypes.WinDLL("advapi32", use_last_error=True)
            pointer = ctypes.c_void_p
            kernel.CreateFileW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                wintypes.DWORD,
                pointer,
                wintypes.DWORD,
                wintypes.DWORD,
                pointer,
            ]
            kernel.CreateFileW.restype = wintypes.HANDLE
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel.LocalFree.argtypes = [pointer]
            kernel.LocalFree.restype = pointer
            security.GetSecurityInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.DWORD,
                pointer,
                pointer,
                ctypes.POINTER(pointer),
                pointer,
                ctypes.POINTER(pointer),
            ]
            security.GetSecurityInfo.restype = wintypes.DWORD
            security.SetSecurityInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.DWORD,
                pointer,
                pointer,
                pointer,
                pointer,
            ]
            security.SetSecurityInfo.restype = wintypes.DWORD
            security.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                ctypes.POINTER(pointer),
                pointer,
            ]
            security.SetFileSecurityW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.DWORD,
                pointer,
            ]
            # Retain WRITE_DAC before restricting the fixture so cleanup does
            # not need ownership changes or broader machine permissions.
            handle = kernel.CreateFileW(
                str(work), 0x60000, 7, None, 3, 0x02000000, None
            )
            self.assertNotEqual(handle, pointer(-1).value, ctypes.WinError())
            original_acl, original_descriptor, restricted = (
                pointer(),
                pointer(),
                pointer(),
            )
            try:
                status = security.GetSecurityInfo(
                    handle,
                    1,
                    4,
                    None,
                    None,
                    ctypes.byref(original_acl),
                    None,
                    ctypes.byref(original_descriptor),
                )
                self.assertEqual(status, 0, ctypes.WinError(status))
                self.assertTrue(
                    security.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                        descriptor,
                        1,
                        ctypes.byref(restricted),
                        None,
                    ),
                    ctypes.WinError(),
                )
                self.assertTrue(
                    security.SetFileSecurityW(str(work), 4, restricted),
                    ctypes.WinError(),
                )
                command = [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/c",
                    "echo existing metadata access",
                ]
                options = dict(
                    cwd=work,
                    env=os.environ
                    | {
                        "MCP_CONSOLE_HOME": str(root / "home"),
                        "TEST_POLICY": json.dumps(
                            {
                                "extends": ":read-only",
                                "network": "enabled",
                                "windows_backend": "elevated",
                                "windows_state_dir": state,
                            }
                        ),
                    },
                    capture_output=True,
                    text=True,
                )
                if read_acl_checkpoint:
                    result = self.run_with_read_acl_checkpoint(
                        command, options, work, handle, state, kernel, security
                    )
                else:
                    result = subprocess.run(command, **options, timeout=30)
                if allowed:
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "existing metadata access")
                else:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(
                        "grant command working-directory metadata access",
                        result.stderr,
                    )
                    self.assertIn(
                        "directory metadata access is blocked by an existing deny ACE",
                        result.stderr,
                    )
            finally:
                if original_descriptor.value:
                    status = security.SetSecurityInfo(
                        handle, 1, 4 | 0x20000000, None, None, original_acl, None
                    )
                    self.assertEqual(status, 0, ctypes.WinError(status))
                    kernel.LocalFree(original_descriptor)
                if restricted.value:
                    kernel.LocalFree(restricted)
                kernel.CloseHandle(handle)

    def test_stdio_write_policy_and_exit_status(self):
        with workspace() as root:
            work = root / "work"
            work.mkdir()
            (work / "input.txt").write_text("readable", encoding="utf-8")
            config = {
                "filesystem": {
                    "kind": "restricted",
                    "entries": [
                        {
                            "path": {"type": "special", "value": {"kind": "root"}},
                            "access": "read",
                        },
                        {
                            "path": {"type": "path", "path": str(work)},
                            "access": "write",
                        },
                    ],
                },
                "network": "enabled",
                "windows_backend": "unelevated",
                "windows_state_dir": str(root / "state"),
            }
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/v:on",
                    "/c",
                    "set /p INPUT=&echo !INPUT!&type input.txt&echo allowed>allowed.txt&echo denied>..\\denied.txt&exit /b 23",
                ],
                cwd=work,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(config),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                input=b"hello from stdin\r\n",
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 23, result.stderr.decode(errors="replace")
            )
            self.assertIn(b"hello from stdin", result.stdout)
            self.assertIn(b"readable", result.stdout)
            self.assertEqual((work / "allowed.txt").read_text().strip(), "allowed")
            self.assertFalse((root / "denied.txt").exists())
            # Preserve the high bit through the installed launcher as well as
            # the native runner/frontend's full-width Windows exit status.
            result = subprocess.run(
                [
                    str(BINARY),
                    "sandbox",
                    "--config-env",
                    "TEST_POLICY",
                    "--",
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/c",
                    "exit /b -1073741819",
                ],
                cwd=work,
                env=dict(
                    os.environ,
                    TEST_POLICY=json.dumps(config),
                    MCP_CONSOLE_HOME=str(root / "home"),
                ),
                capture_output=True,
                timeout=30,
            )
            self.assertEqual(result.returncode, 0xC0000005, result.stderr)


if __name__ == "__main__":
    unittest.main()
