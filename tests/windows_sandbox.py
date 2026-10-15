"""Public Windows configuration and capability-selected MXC acceptance."""

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
from support.windows_mxc import requires_mxc

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@contextmanager
def workspace(parent: Path | None = None):
    # Use ordinary inherited ACLs so filesystem-policy tests are independent
    # of Python's version-specific temporary-directory hardening.
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


@contextmanager
def without_backup_privileges():
    # Elevated test hosts can otherwise bypass the fixture's directory ACL.
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    security = ctypes.WinDLL("advapi32", use_last_error=True)
    pointer = ctypes.c_void_p

    class Luid(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.LONG)]

    class Privilege(ctypes.Structure):
        _fields_ = [("luid", Luid), ("attributes", wintypes.DWORD)]

    class Privileges(ctypes.Structure):
        _fields_ = [("count", wintypes.DWORD), ("entries", Privilege * 2)]

    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    security.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    security.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        pointer,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    security.AdjustTokenPrivileges.argtypes = [
        wintypes.HANDLE,
        wintypes.BOOL,
        pointer,
        wintypes.DWORD,
        pointer,
        pointer,
    ]
    security.LookupPrivilegeValueW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        ctypes.POINTER(Luid),
    ]
    token = wintypes.HANDLE()
    if not security.OpenProcessToken(
        kernel.GetCurrentProcess(), 0x28, ctypes.byref(token)
    ):
        raise ctypes.WinError()
    previous = None
    try:
        size = wintypes.DWORD()
        security.GetTokenInformation(token, 3, None, 0, ctypes.byref(size))
        state = ctypes.create_string_buffer(size.value)
        if not security.GetTokenInformation(token, 3, state, size, ctypes.byref(size)):
            raise ctypes.WinError()
        previous = state
        requested = Privileges(count=2)
        for entry, name in zip(
            requested.entries, ("SeBackupPrivilege", "SeRestorePrivilege")
        ):
            if not security.LookupPrivilegeValueW(None, name, ctypes.byref(entry.luid)):
                raise ctypes.WinError()
        if not security.AdjustTokenPrivileges(
            token, False, ctypes.byref(requested), 0, None, None
        ):
            raise ctypes.WinError()
        yield
    finally:
        if previous is not None:
            if not security.AdjustTokenPrivileges(
                token, False, previous, 0, None, None
            ):
                raise ctypes.WinError()
        kernel.CloseHandle(token)


@unittest.skipUnless(os.name == "nt", "native Windows sandbox")
class WindowsSandbox(unittest.TestCase):
    @unittest.skipUnless(os.environ.get("R_HOME"), "configured R runtime")
    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
    def test_console_cache_paths_reach_resolver_and_worker(self):
        self.console_cache_paths(profile_fallback=False)

    @unittest.skipUnless(shutil.which("uv"), "requires uv")
    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
    def test_network_enabled_sql(self):
        from windows import Session, exercise_r_sql

        session = Session(sandbox=True, overrides=['sandbox.network="enabled"'])
        try:
            session.initialize()
            exercise_r_sql(session)
        finally:
            session.close()

    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
    def test_input_and_interrupt_with_private_installation(self):
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
            result = subprocess.run(
                [
                    sys.executable,
                    ROOT / "tests/windows.py",
                    "WindowsSandbox.test_network_restricted_input_and_interrupt",
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
    @requires_mxc(BINARY)
    def test_network_restricted_input_and_interrupt(self):
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

    @requires_mxc(BINARY)
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
    @requires_mxc(BINARY)
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

    def test_modified_companion_is_rejected_before_execution(self):
        with workspace() as root:
            native = native_console(BINARY)
            source = native.resolve().parent.parent
            (root / "bin").mkdir()
            shutil.copy2(native, root / "bin/mcp-console.exe")
            for directory in ("libexec", "share"):
                if (source / directory).exists():
                    shutil.copytree(source / directory, root / directory)
            helpers = list((root / "libexec").rglob("mcp-console-sandbox.exe"))
            self.assertTrue(helpers)
            for helper in helpers:
                with helper.open("ab") as stream:
                    stream.write(b"modified")
            result = subprocess.run(
                [
                    str(root / "bin/mcp-console.exe"),
                    "sandbox",
                    "--no-config",
                    "--",
                    "cmd.exe",
                    "/c",
                    "echo launched",
                ],
                check=False,
                capture_output=True,
                timeout=30,
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(result.stdout, b"")
            self.assertIn(
                b"private artifact does not match this installation", result.stderr
            )

    @requires_mxc(BINARY)
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

    @requires_mxc(BINARY)
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

    @requires_mxc(BINARY)
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
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(result.stdout.strip(), "launched")

    def test_tempdir_modes(self):
        # fmt: python
        program = code(r"""
            import errno
            import json
            import os
            from pathlib import Path

            temporary = Path(os.environ["TMPDIR"])
            mode = os.environ["EXPECTED_MODE"]
            assert (temporary == Path(os.environ["EXPECTED_TEMP"])) is (mode != "managed")
            try:
                _ = (temporary / "probe").write_text("temporary")
            except OSError as error:
                assert mode == "read_only" and error.errno in (
                    errno.EACCES,
                    errno.EPERM,
                    errno.EROFS,
                ), error
            else:
                assert mode != "read_only"
            print(json.dumps({"temporary": str(temporary)}))
            """)
        for mode in ("managed", "read_write", "read_only"):
            with self.subTest(mode=mode), workspace() as root:
                temporary = root / "host-temp"
                temporary.mkdir()
                config = root / ".agents/console/config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text(
                    json.dumps(
                        {
                            "sandbox": {"tempdir": mode},
                            "environment": {"TmpDir": str(temporary)},
                        }
                    )
                )
                result = subprocess.run(
                    [str(BINARY), "sandbox", "--", sys.executable, "-c", program],
                    cwd=root,
                    env=dict(
                        os.environ,
                        MCP_CONSOLE_HOME=str(root / "home"),
                        EXPECTED_TEMP=str(temporary),
                        EXPECTED_MODE=mode,
                    ),
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                selected = Path(json.loads(result.stdout)["temporary"])
                self.assertTrue(temporary.is_dir())
                if mode == "managed":
                    self.assertFalse(selected.exists())

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
                (
                    "sandbox.network={proxy: {}, allow_local_binding: false}",
                    "false cannot be enforced",
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

    @requires_mxc(BINARY)
    def test_cwd_alias_does_not_require_parent_enumeration(self):
        with workspace() as root, without_backup_privileges():
            parent = root / "blocked"
            work = parent / "long child directory"
            work.mkdir(parents=True)
            expected = work.resolve(strict=True)
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
            kernel.GetShortPathNameW.argtypes = [
                wintypes.LPCWSTR,
                wintypes.LPWSTR,
                wintypes.DWORD,
            ]
            short_name = ctypes.create_unicode_buffer(32768)
            length = kernel.GetShortPathNameW(str(work), short_name, len(short_name))
            self.assertGreater(length, 0, ctypes.WinError())
            self.assertLess(length, len(short_name))
            short_work = Path(short_name.value)
            if short_work.name.casefold() == work.name.casefold():
                self.skipTest("working-directory filesystem has no 8.3 alias")
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
            security.SetSecurityInfo.argtypes = [
                wintypes.HANDLE,
                ctypes.c_int,
                wintypes.DWORD,
                pointer,
                pointer,
                pointer,
                pointer,
            ]
            # Retain ACL access before the parent denies metadata reads.
            handle = kernel.CreateFileW(
                str(parent), 0x60000, 7, None, 3, 0x02000000, None
            )
            self.assertNotEqual(handle, pointer(-1).value, ctypes.WinError())
            original_acl, original_descriptor = pointer(), pointer()
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
                subprocess.run(
                    ["icacls", str(parent), "/deny", "*S-1-1-0:(RD,RA)"],
                    check=True,
                    capture_output=True,
                    timeout=30,
                )
                kernel.GetLongPathNameW.argtypes = [
                    wintypes.LPCWSTR,
                    wintypes.LPWSTR,
                    wintypes.DWORD,
                ]
                self.assertEqual(kernel.GetLongPathNameW(str(short_work), None, 0), 0)
                self.assertEqual(ctypes.get_last_error(), 5)
                command = [
                    str(Path(os.environ["SystemRoot"]) / "System32/cmd.exe"),
                    "/d",
                    "/c",
                    "cd",
                ]
                direct = subprocess.run(
                    command, cwd=short_work, capture_output=True, text=True, timeout=30
                )
                self.assertEqual(direct.returncode, 0, direct.stderr)
                self.assertTrue(direct.stdout.strip(), direct.stdout)
                result = subprocess.run(
                    [
                        str(BINARY),
                        "sandbox",
                        "--config-env",
                        "TEST_POLICY",
                        "--",
                        *command,
                    ],
                    cwd=short_work,
                    env=os.environ
                    | {
                        "MCP_CONSOLE_HOME": str(root / "home"),
                        "TEST_POLICY": json.dumps(
                            {
                                "extends": ":read-only",
                                "network": "enabled",
                            }
                        ),
                    },
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), str(expected))
            finally:
                if original_descriptor.value:
                    status = security.SetSecurityInfo(
                        handle, 1, 4, None, None, original_acl, None
                    )
                    self.assertEqual(status, 0, ctypes.WinError(status))
                    kernel.LocalFree(original_descriptor)
                kernel.CloseHandle(handle)

    @requires_mxc(BINARY)
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
