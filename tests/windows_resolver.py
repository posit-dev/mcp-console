"""Windows acceptance for the same hidden resolver protocol used on Unix."""

import json
import ctypes
import os
from pathlib import Path
from queue import Queue
import shutil
import subprocess
import sys
import tempfile
from threading import Thread
import unittest

from windows_gate import Gate

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


class Resolver:
    def __init__(self, directory, environment, mode="PythonOnly"):
        self.process = subprocess.Popen(
            [BINARY, "resolve"],
            cwd=directory,
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        self.messages = Queue()

        def read():
            for line in self.process.stdout:
                self.messages.put(json.loads(line))
            self.messages.put(None)

        Thread(target=read, daemon=True).start()
        self.send({"Open": {"mode": mode}})

    def send(self, message):
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        self.process.stdin.flush()

    def receive(self, timeout=30):
        message = self.messages.get(timeout=timeout)
        if message is None:
            raise AssertionError(self.process.stderr.read().decode(errors="replace"))
        return message

    def ready(self):
        assert self.receive() == "Hello"
        discovery = self.receive()["Completed"]
        assert discovery["confirmed"], discovery
        assert "Ok" in discovery["result"], discovery
        return discovery["result"]["Ok"]

    def run(self, id, operation, timeout=30):
        self.send({"Run": {"id": id, "operation": operation}})
        completed = self.receive(timeout)["Completed"]
        assert completed["id"] == id, completed
        assert completed["confirmed"], completed
        return completed

    def close(self):
        if self.process.poll() is None:
            self.process.stdin.close()
            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=10)
                raise
        self.process.stdin.close()
        self.process.stdout.close()
        self.process.stderr.close()


@unittest.skipUnless(os.name == "nt", "native Windows resolver")
class WindowsResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_directory = tempfile.TemporaryDirectory(
            prefix="console resolver fixture "
        )
        cls.fixture = Path(cls.fixture_directory.name) / "resolver.exe"
        subprocess.run(
            [
                "rustc",
                str(ROOT / "tests/fixtures/windows_resolver.rs"),
                "-o",
                str(cls.fixture),
            ],
            check=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.fixture_directory.cleanup()

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="console resolver 日本語 ")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for name in ("uv.exe", "ir.exe"):
            shutil.copyfile(self.fixture, self.root / name)
        self.library = self.root / "R library 日本語"
        self.library.mkdir()
        self.r_home = self.root / "R home"
        (self.r_home / "bin").mkdir(parents=True)
        shutil.copyfile(self.fixture, self.r_home / "bin/Rscript.exe")
        self.record = self.root / "commands.jsonl"
        self.environment = dict(
            os.environ,
            PATH=str(self.root),
            R_HOME=str(self.r_home),
            MCP_CONSOLE_HOME=str(self.root / "home"),
            TEST_RESOLVER_RECORD=str(self.record),
            TEST_RESOLVER_PYTHON=sys.executable,
            TEST_RESOLVER_LIBRARY=str(self.library),
        )
        self.environment.pop("RETICULATE_PYTHON", None)
        self.environment.pop("RETICULATE_UV", None)

    def resolver(self, mode="PythonOnly"):
        resolver = Resolver(self.root, self.environment, mode)
        self.addCleanup(resolver.close)
        resolver.ready()
        return resolver

    def test_resolves_python_through_uv_subcommand(self):
        resolver = self.resolver()
        version = resolver.run(1, {"PythonVersion": {"constraints": [">=3.12"]}})
        self.assertEqual(version["result"], {"Ok": "3.12.7"})
        manifest = {
            "packages": ["six>=1"],
            "python_version": [">=3.12"],
            "exclude_newer": "2026-01-01",
        }
        prepared = resolver.run(2, {"Python": {"requirements": manifest, "r": None}})
        self.assertEqual(
            prepared["result"],
            {"Ok": {"python": sys.executable, "requirements": manifest}},
        )
        commands = [
            json.loads(line)
            for line in self.record.read_text(encoding="utf-8").splitlines()
        ]
        invocation = commands[-1]
        self.assertEqual(
            invocation[:8],
            [
                "tool",
                "run",
                "--isolated",
                "--python-preference",
                "only-managed",
                "--python",
                "3.12.7",
                "--exclude-newer",
            ],
        )
        self.assertEqual(invocation[8:11], ["2026-01-01", "--with", "six>=1"])
        resolver.send("Close")
        self.assertEqual(resolver.receive(), "Closed")
        self.assertEqual(resolver.process.wait(timeout=10), 0)

    def test_resolves_r_through_ir_subcommand(self):
        resolver = self.resolver("R")
        self.assertEqual(resolver.run(1, "Bootstrap")["result"], {"Ok": None})
        prepared = resolver.run(2, {"R": {"requirements": ["jsonlite"]}})
        result = prepared["result"]["Ok"]
        self.assertEqual(result["library"], str(self.library))
        self.assertEqual(result["requirements"], ["jsonlite"])
        invocation = json.loads(
            self.record.read_text(encoding="utf-8").splitlines()[-1]
        )
        self.assertEqual(invocation[:2], ["run", "--rscript"])
        self.assertEqual(Path(invocation[2]), self.r_home / "bin/Rscript.exe")
        self.assertEqual(invocation[3:5], ["--with", "jsonlite"])

    def test_resolver_failure_preserves_diagnostics(self):
        self.environment["TEST_RESOLVER_MODE"] = "failed"
        resolver = self.resolver()
        result = resolver.run(
            1, {"Python": {"requirements": {"packages": ["six"]}, "r": None}}
        )
        self.assertIn("fixture resolver failure", result["result"]["Err"])

    def blocked_resolver(
        self, mode: str = "blocked", *, release: bool = True, gate: Gate | None = None
    ) -> tuple[Resolver, dict[int, int]]:
        if gate is None:
            gate = Gate()
        self.resolver_gate = gate
        self.addCleanup(gate.close)
        self.environment["TEST_RESOLVER_GATE"] = gate.name
        self.environment["TEST_RESOLVER_MODE"] = mode
        resolver = self.resolver()
        resolver.send(
            {
                "Run": {
                    "id": 1,
                    "operation": {
                        "Python": {"requirements": {"packages": ["six"]}, "r": None}
                    },
                }
            }
        )
        gate.accept(resolver.process)
        try:
            pids = [int(pid) for pid in gate.readline().split()]
            self.assertEqual(len(pids), 2)
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [
                ctypes.c_uint32,
                ctypes.c_int,
                ctypes.c_uint32,
            ]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            processes: dict[int, int] = {}
            for pid in pids:
                handle = kernel.OpenProcess(0x100000, 0, pid)
                self.assertTrue(handle, ctypes.get_last_error())
                self.addCleanup(kernel.CloseHandle, handle)
                processes[pid] = handle
            # Pin both identities before permitting normal exit or sending a
            # control. Reopening PIDs after retirement can observe their reuse.
            if release:
                gate.sendall(b"\x01")
        finally:
            if release:
                gate.close()
        return resolver, processes

    def assert_retired(self, processes: dict[int, int]) -> None:
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        # Root exit observation is part of Completed. Job accounting can reach
        # zero while descendant process objects finish kernel teardown; wait
        # on their pinned handles without reopening PIDs or interval polling.
        for index, (pid, handle) in enumerate(processes.items()):
            role = "root" if index == 0 else "descendant"
            self.assertEqual(
                kernel.WaitForSingleObject(handle, 0 if index == 0 else 5000),
                0,
                f"resolver {role} process {pid} survived completion",
            )

    def test_interrupt_confirms_descendant_retirement(self):
        resolver, processes = self.blocked_resolver()
        resolver.send({"Control": {"id": 1, "control": "Interrupted"}})
        self.assertEqual(
            resolver.receive(), {"Controlled": {"id": 1, "result": {"Ok": True}}}
        )
        completed = resolver.receive()["Completed"]
        self.assertTrue(completed["confirmed"], completed)
        self.assertEqual(completed["control"], "Interrupted")
        self.assertEqual(
            completed["result"], {"Err": "managed Python resolution interrupted"}
        )
        self.assert_retired(processes)
        # A settled interrupt belongs to the old operation; a later call works.
        self.assertEqual(
            resolver.run(2, {"PythonVersion": {"constraints": []}})["result"],
            {"Ok": "3.12.7"},
        )

    def test_eof_cancels_resolver_and_descendants(self):
        resolver, processes = self.blocked_resolver()
        resolver.process.stdin.close()
        resolver.process.wait(timeout=10)
        self.assert_retired(processes)

    def test_cancellation_before_gate_release_retires_resolver_and_descendants(self):
        for action in ("Interrupted", "Close"):
            with self.subTest(action=action):
                # Pin both identities without releasing the fixture's pipe
                # checkpoint. Cancellation must retire it and its descendant.
                resolver, processes = self.blocked_resolver(release=False)
                if action == "Close":
                    resolver.process.stdin.close()
                    resolver.process.wait(timeout=10)
                else:
                    resolver.send({"Control": {"id": 1, "control": action}})
                    self.assertEqual(
                        resolver.receive(),
                        {"Controlled": {"id": 1, "result": {"Ok": True}}},
                    )
                    completed = resolver.receive()["Completed"]
                    self.assertTrue(completed["confirmed"], completed)
                    self.assertEqual(completed["control"], action)
                    self.assertEqual(
                        completed["result"],
                        {"Err": "managed Python resolution interrupted"},
                    )
                self.assert_retired(processes)

    def debugger(self):
        # A held EXIT_PROCESS_DEBUG_EVENT delays kernel shutdown and process
        # handle signaling, without depending on a slow or faulty I/O driver.
        # https://learn.microsoft.com/en-us/windows/win32/debug/debugging-events
        class ExceptionRecord(ctypes.Structure):
            _fields_ = [
                ("code", ctypes.c_uint32),
                ("flags", ctypes.c_uint32),
                ("record", ctypes.c_void_p),
                ("address", ctypes.c_void_p),
                ("count", ctypes.c_uint32),
                ("parameters", ctypes.c_size_t * 15),
            ]

        class ExceptionInfo(ctypes.Structure):
            _fields_ = [("record", ExceptionRecord), ("first", ctypes.c_uint32)]

        class DebugInfo(ctypes.Union):
            # The exception member determines the union's native size/alignment.
            # CREATE_PROCESS and LOAD_DLL both begin with an owned file handle.
            _fields_ = [
                ("exception", ExceptionInfo),
                ("file", ctypes.c_void_p),
                ("exit_code", ctypes.c_uint32),
            ]

        class DebugEvent(ctypes.Structure):
            _fields_ = [
                ("code", ctypes.c_uint32),
                ("pid", ctypes.c_uint32),
                ("tid", ctypes.c_uint32),
                ("info", DebugInfo),
            ]

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.DebugActiveProcess.argtypes = [ctypes.c_uint32]
        kernel.DebugActiveProcessStop.argtypes = [ctypes.c_uint32]
        kernel.WaitForDebugEvent.argtypes = [
            ctypes.POINTER(DebugEvent),
            ctypes.c_uint32,
        ]
        kernel.ContinueDebugEvent.argtypes = [ctypes.c_uint32] * 3
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]

        def checked(result: int | None) -> None:
            if not result:
                raise ctypes.WinError(ctypes.get_last_error())

        def next_event() -> DebugEvent:
            event = DebugEvent()
            checked(kernel.WaitForDebugEvent(ctypes.byref(event), 10000))
            if event.code in (3, 6) and event.info.file:
                checked(kernel.CloseHandle(event.info.file))
            return event

        def resume(event: DebugEvent) -> None:
            checked(kernel.ContinueDebugEvent(event.pid, event.tid, 0x00010002))

        return kernel, checked, next_event, resume

    def test_interrupt_preserves_natural_exit_diagnostics(self) -> None:
        # Hold a natural EXIT_PROCESS_DEBUG_EVENT: try_wait still reports live,
        # but Job termination must not replace the resolver's original failure.
        # https://learn.microsoft.com/en-us/windows/win32/debug/debugging-events
        kernel, checked, next_event, resume = self.debugger()
        for exit_code in (1, 23, 0xC000013A):
            with self.subTest(exit_code=exit_code):
                self.environment["TEST_RESOLVER_EXIT_CODE"] = str(
                    ctypes.c_int32(exit_code).value
                )
                gate = Gate()
                resolver, processes = self.blocked_resolver(
                    "failed", release=False, gate=gate
                )
                pid, handle = next(iter(processes.items()))
                checked(kernel.DebugActiveProcess(pid))
                held = None
                try:
                    while True:
                        event = next_event()
                        resume(event)
                        if event.code == 1:
                            break
                    gate.sendall(b"\x01")
                    while True:
                        event = next_event()
                        if event.code == 5:
                            held = event
                            break
                        resume(event)
                    self.assertEqual(held.info.exit_code, exit_code)
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                    resolver.send({"Control": {"id": 1, "control": "Interrupted"}})
                    self.assertEqual(
                        resolver.receive(),
                        {"Controlled": {"id": 1, "result": {"Ok": True}}},
                    )
                finally:
                    if held is not None:
                        resume(held)
                    else:
                        checked(kernel.DebugActiveProcessStop(pid))
                completed = resolver.receive()["Completed"]
                self.assertTrue(completed["confirmed"], completed)
                diagnostic = completed["result"]["Err"]
                self.assertIn("managed Python resolution failed", diagnostic)
                self.assertIn("fixture resolver failure", diagnostic)
                self.assertNotIn("resolution interrupted", diagnostic)
                self.assert_retired(processes)

    def test_interrupt_preserves_failure_during_kernel_exit(self) -> None:
        resolver, processes = self.blocked_resolver("failed", release=False)
        pid, handle = next(iter(processes.items()))
        kernel, checked, next_event, resume = self.debugger()
        checked(kernel.DebugActiveProcess(pid))
        held = None
        try:
            while True:
                event = next_event()
                resume(event)
                if event.code == 1:
                    break
            self.resolver_gate.sendall(b"\x01")
            while True:
                event = next_event()
                if event.code == 5:
                    held = event
                    break
                resume(event)
            # Natural exit has fixed its status, but the process handle is
            # still unsignaled. try_wait therefore reports a live resolver.
            self.assertEqual(event.info.exit_code, 23)
            self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
            resolver.send({"Control": {"id": 1, "control": "Interrupted"}})
            self.assertEqual(
                resolver.receive(), {"Controlled": {"id": 1, "result": {"Ok": True}}}
            )
            resume(held)
            held = None
            completed = resolver.receive()["Completed"]
            self.assertTrue(completed["confirmed"], completed)
            self.assertIsNone(completed["control"], completed)
            self.assertEqual(
                completed["result"],
                {
                    "Err": "managed Python resolution failed:\nresolver input:\n"
                    + json.dumps(
                        {"python": "3.12.7 (reticulate default)", "packages": ["six"]},
                        indent=2,
                    )
                    + "\nuv output:\nfixture resolver failure"
                },
            )
            self.assert_retired(processes)
            self.assertEqual(
                resolver.run(2, {"PythonVersion": {"constraints": []}})["result"],
                {"Ok": "3.12.7"},
            )
        finally:
            if held is not None:
                resume(held)
            elif kernel.WaitForSingleObject(handle, 0) == 258:
                checked(kernel.DebugActiveProcessStop(pid))

    def test_delayed_exit_reports_unconfirmed_retirement(self) -> None:
        kernel, checked, next_event, resume = self.debugger()
        for action in ("Cancelled", "Close"):
            with self.subTest(action=action):
                resolver, processes = self.blocked_resolver()
                pid, handle = next(iter(processes.items()))
                checked(kernel.DebugActiveProcess(pid))
                held = None
                try:
                    # Consume the attachment events through its breakpoint so
                    # termination begins with a running, registered resolver.
                    while True:
                        event = next_event()
                        if event.code == 1:
                            self.assertEqual(
                                event.info.exception.record.code, 0x80000003
                            )
                        resume(event)
                        if event.code == 1:
                            break
                    if action == "Close":
                        resolver.send("Close")
                    else:
                        resolver.send({"Control": {"id": 1, "control": action}})
                        self.assertEqual(
                            resolver.receive(),
                            {"Controlled": {"id": 1, "result": {"Ok": True}}},
                        )
                    while True:
                        event = next_event()
                        if event.code == 5:
                            held = event
                            break
                        resume(event)
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                    # The five-second allowance must expire while exit remains
                    # held. An observer join before that allowance hangs here.
                    self.assertNotEqual(resolver.process.wait(timeout=10), 0)
                    self.assertIn(
                        "preparation retirement is unconfirmed",
                        resolver.process.stderr.read().decode(errors="replace"),
                    )
                    self.assertEqual(kernel.WaitForSingleObject(handle, 0), 258)
                finally:
                    if held is not None:
                        resume(held)
                    else:
                        checked(kernel.DebugActiveProcessStop(pid))
                self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0)
                self.assert_retired(processes)

    def test_success_retires_descendant_holding_output(self):
        resolver, processes = self.blocked_resolver("exited")
        completed = resolver.receive()["Completed"]
        self.assertTrue(completed["confirmed"], completed)
        self.assertIn("Ok", completed["result"])
        self.assert_retired(processes)


@unittest.skipUnless(os.name == "nt", "native Windows materialization")
class WindowsResolverMaterialization(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(
            prefix="console materialization 日本語 "
        )
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        uv = shutil.which("uv")
        self.assertIsNotNone(uv, "Windows resolver acceptance requires uv")
        shutil.copyfile(uv, self.root / "uv.exe")
        self.environment = dict(
            os.environ,
            PATH=os.pathsep.join(
                [
                    str(self.root),
                    str(Path(sys.executable).parent),
                    str(Path(os.environ["SystemRoot"]) / "System32"),
                ]
            ),
            MCP_CONSOLE_HOME=str(self.root / "home"),
            LC_ALL="C",
        )
        self.environment.pop("RETICULATE_PYTHON", None)
        self.environment.pop("RETICULATE_UV", None)

    def resolver(self, mode):
        resolver = Resolver(self.root, self.environment, mode)
        self.addCleanup(resolver.close)
        resolver.ready()
        return resolver

    def test_uv_materializes_retained_python_environment(self):
        resolver = self.resolver("PythonOnly")
        prepared = resolver.run(
            1,
            {
                "Python": {
                    "requirements": {"packages": ["six==1.17.0"]},
                    "r": None,
                    "selected_python": sys.executable,
                }
            },
        )
        candidate = prepared["result"]["Ok"]["python"]
        self.assertNotEqual(Path(candidate), Path(sys.executable))
        output = subprocess.check_output(
            [candidate, "-I", "-c", "import six; print(six.__version__)"], text=True
        )
        self.assertEqual(output.strip(), "1.17.0")

    def test_uv_bootstraps_ir_and_materializes_r_library(self):
        r_home = os.environ.get("R_HOME")
        if not r_home:
            r_home = subprocess.check_output(
                [shutil.which("R") or "R", "RHOME"], text=True
            ).strip()
        self.environment["R_HOME"] = r_home
        resolver = self.resolver("R")
        self.assertEqual(
            resolver.run(1, "Bootstrap", timeout=180)["result"], {"Ok": None}
        )
        prepared = resolver.run(2, {"R": {"requirements": ["jsonlite"]}}, timeout=180)
        result = prepared["result"]["Ok"]
        self.assertTrue(
            (Path(result["library"]) / "jsonlite/DESCRIPTION").is_file(), result
        )
        self.assertEqual(result["requirements"], ["jsonlite"])
        # The real ir child executes the embedded multiline program with Rscript.
        self.assertTrue(prepared["confirmed"])


if __name__ == "__main__":
    unittest.main()
