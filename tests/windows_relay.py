"""Windows acceptance through the server-relay and relay-worker wire protocols."""

import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
from queue import Queue
import subprocess
import tempfile
from threading import Thread
from textwrap import dedent
import unittest

from support.relay_commands import (
    SHUTDOWN,
    WORKER_COMMANDS,
    assert_forwarding,
    command_batch,
)
from support.relay_lifecycle import (
    LIFECYCLE_COMMANDS,
    assert_exit_tail,
    assert_failure_tail,
    exercise_shutdown_admission,
)
from support.normalization import code
from windows_gate import Gate

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(
    os.environ.get("MCP_CONSOLE_TEST_BINARY", ROOT / "target/debug/mcp-console.exe")
)


@unittest.skipUnless(os.name == "nt", "native Windows relay")
class WindowsRelay(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="console relay worker ")
        cls.addClassCleanup(cls.directory.cleanup)
        cls.worker = Path(cls.directory.name) / "worker.exe"
        subprocess.run(
            [
                "rustc",
                "--edition=2024",
                str(ROOT / "tests/fixtures/windows_worker.rs"),
                "-o",
                str(cls.worker),
            ],
            check=True,
        )

    def command_pipe(self):
        """Give the test a named pipe server writer with a consumption barrier."""
        import msvcrt
        import uuid

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.CreateNamedPipeW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
        ]
        kernel.CreateNamedPipeW.restype = wintypes.HANDLE
        kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.LPVOID,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        kernel.CreateFileW.restype = wintypes.HANDLE
        name = rf"\\.\pipe\console-framing-{uuid.uuid4().hex}"
        writer = kernel.CreateNamedPipeW(name, 2, 0, 1, 65536, 65536, 0, None)
        self.assertNotEqual(writer, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        writer = os.fdopen(
            msvcrt.open_osfhandle(writer, os.O_WRONLY | os.O_BINARY), "wb"
        )
        self.addCleanup(writer.close)
        reader = kernel.CreateFileW(name, 0x80000000, 0, None, 3, 0, None)
        self.assertNotEqual(reader, ctypes.c_void_p(-1).value, ctypes.get_last_error())
        reader = os.fdopen(
            msvcrt.open_osfhandle(reader, os.O_RDONLY | os.O_BINARY), "rb"
        )
        self.addCleanup(reader.close)
        return reader, writer

    def start(self, scenario: str, *, command_pipe: bool = False):
        ready = Gate()
        self.addCleanup(ready.close)
        directory = tempfile.TemporaryDirectory(prefix="console relay records ")
        self.addCleanup(directory.cleanup)
        marker = Path(directory.name) / "dispatched"
        command_reader, command_writer = (
            self.command_pipe() if command_pipe else (subprocess.PIPE, None)
        )
        process = subprocess.Popen(
            [str(BINARY), "worker-relay", str(self.worker)],
            env=dict(
                os.environ,
                # Built-in worker scenarios must exclude host R startup files.
                R_ENVIRON=os.devnull,
                R_ENVIRON_USER=os.devnull,
                R_PROFILE=os.devnull,
                R_PROFILE_USER=os.devnull,
                R_DEFAULT_PACKAGES="NULL",
                TEST_WORKER_SCENARIO=scenario,
                TEST_WORKER_READY=ready.name,
                TEST_DISPATCHED=str(marker),
                TEST_CONSOLE_BINARY=str(BINARY),
                TMPDIR=directory.name,
                MCP_CONSOLE_HOME=directory.name,
            ),
            stdin=command_reader,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        if command_pipe:
            command_reader.close()
            process.stdin = command_writer
        self.addCleanup(self.stop, process)
        try:
            ready.accept(process)
        except (TimeoutError, RuntimeError, OSError):
            if process.poll() is not None:
                output, errors = process.communicate()
                self.fail(
                    f"relay exited before fixture readiness ({process.returncode}): "
                    f"{output.decode(errors='replace')}{errors.decode(errors='replace')}"
                )
            raise
        pid = int(ready.readline())
        if scenario.startswith(("framing_", "retirement_")):
            self.framing_control = ready
            self.framing_stream = ready
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        owned_worker = kernel.OpenProcess(
            0x100001 if scenario.startswith("retirement_") else 0x100000, False, pid
        )
        self.assertTrue(owned_worker, ctypes.get_last_error())
        self.addCleanup(kernel.CloseHandle, owned_worker)
        if scenario.startswith("retirement_"):
            kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
            kernel.TerminateProcess.restype = wintypes.BOOL

            def retire():
                if kernel.WaitForSingleObject(owned_worker, 0) != 0:
                    kernel.TerminateProcess(owned_worker, 1)
                    kernel.WaitForSingleObject(owned_worker, 10000)

            self.addCleanup(retire)
        ready.sendall(b"1")
        if not scenario.startswith(("framing_", "retirement_")):
            ready.close()
        started = json.loads(process.stdout.readline())
        self.assertEqual(started, {"kind": "worker_started", "pid": pid})
        self.assertEqual(json.loads(process.stdout.readline()), {"kind": "ready"})
        return process, kernel, owned_worker, marker

    def framing_reader(self, process: subprocess.Popen[bytes]):
        events = Queue()

        def read():
            for line in process.stdout:
                events.put(json.loads(line))
            events.put(None)

        reader = Thread(target=read, daemon=True)
        reader.start()
        return events, reader

    def framing_finish(
        self, process: subprocess.Popen[bytes], events: Queue, reader: Thread
    ) -> list[dict]:
        process.stdin.close()
        process.wait(timeout=10)
        reader.join(timeout=5)
        self.assertFalse(reader.is_alive())
        self.assertEqual(process.returncode, 0, process.stderr.read().decode())
        tail = []
        while (event := events.get(timeout=5)) is not None:
            tail.append(event)
        return tail

    def owned_descendant(self, kernel):
        pid = int(self.framing_stream.readline())
        handle = kernel.OpenProcess(0x100001, False, pid)
        self.assertTrue(handle, ctypes.get_last_error())
        kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel.TerminateProcess.restype = wintypes.BOOL

        def retire():
            kernel.TerminateProcess(handle, 1)
            kernel.WaitForSingleObject(handle, 10000)
            kernel.CloseHandle(handle)

        self.addCleanup(retire)
        return handle

    def test_framing_fragmented_semantics_and_retiring_tail(self) -> None:
        process, kernel, worker, _ = self.start("framing_fragmented")
        events, reader = self.framing_reader(process)
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        self.assertEqual(self.framing_stream.readline(), b"prefix consumed\n")
        self.assertTrue(events.empty(), "incomplete semantic frame was forwarded")
        self.framing_control.sendall(b"1")
        self.assertEqual(
            events.get(timeout=10), {"kind": "console_output", "data": "fragmented 🦀"}
        )
        self.assertEqual(events.get(timeout=10), {"kind": "completed"})
        holder = self.owned_descendant(kernel)
        self.framing_control.sendall(b"1")
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        # Keep stdin and the inherited writer live until retirement completes.
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(kernel.WaitForSingleObject(holder, 0), 258)
        # The holder also inherits the relay's outer stdout handle, so relay
        # exit alone cannot produce EOF for the test's stdout reader.
        self.assertTrue(kernel.TerminateProcess(holder, 1), ctypes.get_last_error())
        self.assertEqual(kernel.WaitForSingleObject(holder, 10000), 0)
        tail = self.framing_finish(process, events, reader)
        self.assertEqual(
            tail,
            [
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_framing_fragmented_commands_and_batch_tail(self) -> None:
        import msvcrt

        process, kernel, _, marker = self.start("framing_echo", command_pipe=True)
        events, reader = self.framing_reader(process)
        source = "command 🦀" + "x" * (128 * 1024)
        frame = json.dumps(
            {"kind": "evaluate", "language": "r", "source": source}, ensure_ascii=False
        ).encode()
        split = frame.index("🦀".encode()) + 1
        process.stdin.write(frame[:split])
        process.stdin.flush()
        kernel.FlushFileBuffers.argtypes = [wintypes.HANDLE]
        kernel.FlushFileBuffers.restype = wintypes.BOOL
        consumed = Queue()

        def flush():
            consumed.put(
                kernel.FlushFileBuffers(msvcrt.get_osfhandle(process.stdin.fileno()))
            )

        barrier = Thread(target=flush, daemon=True)
        barrier.start()
        self.assertTrue(consumed.get(timeout=10), ctypes.get_last_error())
        barrier.join(timeout=5)
        self.assertFalse(marker.exists(), "partial command was dispatched")
        process.stdin.write(frame[split:] + b'\r\n{"kind":')
        process.stdin.flush()
        self.assertEqual(events.get(timeout=10), {"kind": "completed"})
        self.assertEqual(json.loads(marker.read_bytes())["source"], source)
        tail = self.framing_finish(process, events, reader)
        self.assertIn(
            {
                "kind": "fatal",
                "message": "relay stdin frame is invalid: relay stdin closed midway through a frame",
            },
            tail,
        )

    def test_framing_inherited_writer_cannot_extend_retirement(self) -> None:
        process, kernel, worker, _ = self.start("framing_refill")
        events, reader = self.framing_reader(process)
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"42"}\n')
        process.stdin.flush()
        holder = self.owned_descendant(kernel)
        self.framing_control.sendall(b"1")
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        self.assertEqual(process.wait(timeout=10), 0)
        self.assertEqual(kernel.WaitForSingleObject(holder, 0), 258)
        # The holder also inherits the relay's outer stdout handle, so relay
        # exit alone cannot produce EOF for the test's stdout reader.
        self.assertTrue(kernel.TerminateProcess(holder, 1), ctypes.get_last_error())
        self.assertEqual(kernel.WaitForSingleObject(holder, 10000), 0)
        tail = self.framing_finish(process, events, reader)
        outputs = [event for event in tail if event["kind"] == "console_output"]
        self.assertTrue(outputs)
        self.assertTrue(
            all(
                event == {"kind": "console_output", "data": "inherited writer"}
                for event in outputs
            )
        )
        self.assertEqual(
            tail,
            outputs
            + [
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_framing_malformed_empty_and_partial_eof_diagnostics(self) -> None:
        for scenario, diagnostic in (
            ("framing_malformed", "EOF while parsing a value at line 2 column 0"),
            ("framing_empty", "EOF while parsing a value at line 2 column 0"),
            ("framing_partial", "worker sideband closed midway through a frame"),
        ):
            with self.subTest(scenario=scenario):
                process, _, _, _ = self.start(scenario)
                process.stdin.write(
                    b'{"kind":"evaluate","language":"r","source":"42"}\n'
                )
                process.stdin.flush()
                process.wait(timeout=10)
                events = self.finish(process)
                self.assertIn(
                    {
                        "kind": "fatal",
                        "message": f"worker sideband read failed: {diagnostic}",
                    },
                    events,
                )

    def test_framing_malformed_complete_commands_keep_diagnostics(self) -> None:
        for frame in (b'{"kind":\n', b"\r\n"):
            with self.subTest(frame=frame):
                process, _, _, marker = self.start("commands")
                process.stdin.write(frame)
                process.stdin.flush()
                events = self.finish(process)
                self.assertIn(
                    {
                        "kind": "fatal",
                        "message": "relay stdin frame is invalid: EOF while parsing a value at line 2 column 0",
                    },
                    events,
                )
                self.assertFalse(marker.exists())

    def test_framing_builtin_retains_partial_command_across_interrupt(self) -> None:
        process, kernel, _, _ = self.start("framing_interrupt")
        self.owned_descendant(kernel)
        events, reader = self.framing_reader(process)
        process.stdin.write(
            b'{"kind":"evaluate","language":"r","source":"retained <- 0L"}\n'
        )
        process.stdin.flush()
        self.assertEqual(
            [events.get(timeout=15) for _ in range(3)],
            [
                {"kind": "cell_started"},
                {"kind": "r_initialization", "complete": False},
                {"kind": "r_initialization", "complete": True},
            ],
        )
        self.assertEqual(events.get(timeout=15), {"kind": "completed"})
        command = {
            "kind": "evaluate",
            "language": "r",
            "source": 'retained <- retained + 1L; cat("🦀", retained)',
        }
        process.stdin.write(json.dumps(command, ensure_ascii=False).encode() + b"\r\n")
        process.stdin.flush()
        self.assertEqual(self.framing_stream.readline(), b"prefix consumed\n")
        # This output proves the real worker returned from its incomplete read
        # and handled the idle interrupt before we release the UTF-8 suffix.
        self.assertEqual(
            events.get(timeout=15), {"kind": "console_diagnostic", "data": "\n"}
        )
        self.framing_control.sendall(b"1")
        self.assertEqual(events.get(timeout=15), {"kind": "cell_started"})
        output = []
        while (event := events.get(timeout=15))["kind"] != "completed":
            self.assertEqual(event["kind"], "console_output", event)
            output.append(event["data"])
        self.assertEqual("".join(output), "🦀 1")
        process.stdin.write(b'{"kind":"shutdown","grace_millis":1000}\n')
        process.stdin.flush()
        tail = self.framing_finish(process, events, reader)
        self.assertEqual(
            tail,
            [
                {"kind": "shutdown_started"},
                {"kind": "stdout_closed"},
                {"kind": "stderr_closed"},
                {"kind": "worker_sideband_closed"},
                {"kind": "worker_exited", "code": 0},
            ],
        )

    def test_framing_builtin_services_later_with_partial_command(self) -> None:
        from windows import Session

        # Prepare the real package through MCP, then give the raw worker its
        # library. This fixture has no server to answer resolver requests.
        session = Session()
        try:
            session.initialize()
            self.assertFalse(session.send(requirements={"r": ["later"]})["isError"])
            result = session.send(r='cat(dirname(find.package("later")))')
            self.assertFalse(result["isError"], result)
            library = json.dumps(result["content"][0]["text"])
        finally:
            session.close()

        process, kernel, _, gate = self.start("framing_later")
        self.owned_descendant(kernel)
        events, reader = self.framing_reader(process)
        # fmt: r
        callback = code(r"""
            retained <- 0L
            run_callback <- function() {
              if (!file.exists(callback_gate)) {
                later::later(run_callback, delay = 0.01)
                return(invisible(NULL))
              }
              cat("idle callback\n")
            }
            later::later(run_callback, delay = 0.01)
            """)
        source = (
            f".libPaths(c({library}, .libPaths()))\n"
            f"callback_gate <- {json.dumps(str(gate))}\n" + callback
        )
        process.stdin.write(
            json.dumps({"kind": "evaluate", "language": "r", "source": source}).encode()
            + b"\n"
        )
        process.stdin.flush()
        self.assertEqual(
            [events.get(timeout=15) for _ in range(3)],
            [
                {"kind": "cell_started"},
                {"kind": "r_initialization", "complete": False},
                {"kind": "r_initialization", "complete": True},
            ],
        )
        self.assertEqual(events.get(timeout=15), {"kind": "completed"})
        command = {
            "kind": "evaluate",
            "language": "r",
            "source": 'retained <- retained + 1L; cat("🦀", retained)',
        }
        process.stdin.write(json.dumps(command, ensure_ascii=False).encode() + b"\r\n")
        process.stdin.flush()
        self.assertEqual(self.framing_stream.readline(), b"prefix consumed\n")
        gate.touch()
        # The suffix is still withheld: this event must come from idle message
        # dispatch, including cancellation/retirement of the incomplete read.
        self.assertEqual(
            events.get(timeout=15),
            {"kind": "console_output", "data": "idle callback\n"},
        )
        self.framing_control.sendall(b"1")
        self.assertEqual(events.get(timeout=15), {"kind": "cell_started"})
        output = []
        while (event := events.get(timeout=15))["kind"] != "completed":
            self.assertEqual(event["kind"], "console_output", event)
            output.append(event["data"])
        self.assertEqual("".join(output), "🦀 1")
        process.stdin.write(b'{"kind":"shutdown","grace_millis":1000}\n')
        process.stdin.flush()
        tail = self.framing_finish(process, events, reader)
        self.assertIn({"kind": "worker_exited", "code": 0}, tail)

    @staticmethod
    def stop(process):
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None:
                stream.close()

    def finish(self, process):
        output, errors = process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, errors.decode(errors="replace"))
        return [json.loads(line) for line in output.splitlines()]

    def test_rejects_unterminated_commands(self):
        for command in (
            {"kind": "evaluate", "language": "r", "source": "42"},
            {"kind": "stdin", "data": "x"},
        ):
            with self.subTest(command=command):
                process, _, _, marker = self.start("commands")
                process.stdin.write(json.dumps(command).encode())
                process.stdin.flush()
                events = self.finish(process)
                self.assertTrue(
                    any(
                        event["kind"] == "fatal"
                        and "midway through a frame" in event["message"]
                        for event in events
                    ),
                    events,
                )
                self.assertFalse(marker.exists(), "unterminated command was dispatched")

    def test_forwards_worker_commands_and_keeps_controls_local(self):
        process, _, _, marker = self.start("forwarding")
        events, reader = self.framing_reader(process)
        process.stdin.write(command_batch())
        process.stdin.flush()
        receipts = [events.get(timeout=10) for _ in range(len(WORKER_COMMANDS) + 1)]
        process.stdin.write(json.dumps(SHUTDOWN).encode() + b"\n")
        process.stdin.flush()
        tail = self.framing_finish(process, events, reader)
        assert_forwarding(marker, receipts, tail)

    def test_fatal_precedes_sideband_closure(self):
        process, kernel, worker, _ = self.start("invalid_sideband")
        process.stdin.write(
            json.dumps(LIFECYCLE_COMMANDS["invalid_sideband"]).encode() + b"\n"
        )
        process.stdin.flush()
        # Keep the input open: the malformed sideband must initiate retirement.
        process.wait(timeout=10)
        events = self.finish(process)
        self.assertEqual(kernel.WaitForSingleObject(worker, 0), 0)
        assert_failure_tail(
            events,
            "worker sideband read failed: unknown variant `broken`",
            {"kind": "worker_exited", "code": 1},
        )

    def test_shutdown_ignores_buffered_commands_and_trailing_bytes(self):
        self.shutdown_admission("retirement_batch")

    def test_shutdown_ignores_commands_sent_after_worker_receipt(self):
        self.shutdown_admission("retirement_receipt")

    def test_sideband_eof_closes_admission_without_renewing_deadline(self):
        self.shutdown_admission("retirement_sideband")

    def test_clean_controller_eof_retires_without_shutdown_acceptance(self):
        self.shutdown_admission("retirement_eof")

    def test_retires_after_sideband_forwarding_fails_with_blocked_stdout(self):
        process, kernel, worker, _ = self.start("retirement_backpressure")
        process.stdin.write(b'{"kind":"evaluate","language":"r","source":"output"}\n')
        process.stdin.flush()
        # One byte proves the writer began the 1-MiB frame. Leaving the rest
        # unread keeps its blocking write unfinished throughout the assertion.
        prefix = process.stdout.read1(1)
        self.assertEqual(prefix, b"{")
        for request_id in range(1, 18):
            process.stdin.write(
                json.dumps({"kind": "interrupt", "request_id": request_id}).encode()
                + b"\n"
            )
        process.stdin.flush()
        # Input and both worker outputs stay open: failed forwarding must start
        # retirement without relying on EOF or the blocked writer's callback.
        self.assertEqual(
            kernel.WaitForSingleObject(worker, 5000), 0, "direct worker did not retire"
        )
        self.assertEqual(process.wait(timeout=5), 1)
        output, errors = process.communicate(timeout=5)
        frame = b'{"kind":"console_output","data":"' + b"x" * (1024 * 1024) + b'"}\n'
        captured = prefix + output
        self.assertTrue(captured)
        self.assertLess(len(captured), len(frame), "stdout write was not blocked")
        self.assertEqual(captured, frame[: len(captured)])
        self.assertEqual(
            errors,
            b"relay stdout write failed: relay stdout retirement deadline expired\n",
        )

    def shutdown_admission(self, scenario: str) -> None:
        process, kernel, worker, marker = self.start(scenario)

        def wait_worker(timeout: float) -> None:
            self.assertEqual(
                kernel.WaitForSingleObject(worker, int(timeout * 1000)),
                0,
                "worker budget was renewed",
            )

        exercise_shutdown_admission(
            process,
            self.framing_stream,
            marker,
            scenario,
            wait_worker,
            {"kind": "worker_exited", "code": 1},
            shutdown_on_sideband_eof=True,
        )

    def test_stdin_write_failure_retires_worker(self):
        process, kernel, worker, _ = self.start("closed_stdin")
        process.stdin.write(
            json.dumps(LIFECYCLE_COMMANDS["closed_stdin"]).encode() + b"\n"
        )
        process.stdin.flush()
        process.wait(timeout=10)
        events = self.finish(process)
        self.assertEqual(kernel.WaitForSingleObject(worker, 0), 0)
        assert_failure_tail(
            events,
            "worker stdin write failed:",
            {"kind": "worker_exited", "code": 1},
        )

    def test_drains_sideband_after_exit(self):
        process, kernel, worker, _ = self.start("exit_tail")
        process.stdin.write(
            json.dumps(LIFECYCLE_COMMANDS["exit_tail"]).encode() + b"\n"
        )
        process.stdin.flush()
        # Fill the relay's bounded event queue before allowing stdout to drain.
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        events = self.finish(process)
        assert_exit_tail(events)

    def test_r_home_environment_precedence(self):
        # Exercise R startup directly, independently of third-party resolver
        # home-directory requirements (notably DuckDB's USERPROFILE lookup).
        with tempfile.TemporaryDirectory(prefix="console HOME ") as home:
            for r_user, profile in (
                (None, os.environ.get("USERPROFILE")),
                (None, None),
                ("", None),
            ):
                with self.subTest(r_user=r_user, profile=profile):
                    environment = dict(os.environ, HOME=home, TMPDIR=home)
                    for key, value in (("R_USER", r_user), ("USERPROFILE", profile)):
                        if value is None:
                            environment.pop(key, None)
                        else:
                            environment[key] = value
                    process = subprocess.Popen(
                        [str(BINARY), "worker-relay", str(BINARY), "worker"],
                        env=environment,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    )
                    self.addCleanup(self.stop, process)
                    events = Queue()

                    def read():
                        for line in process.stdout:
                            events.put(json.loads(line))
                        events.put(None)

                    reader = Thread(target=read, daemon=True)
                    reader.start()
                    try:
                        started = events.get(timeout=15)
                        self.assertEqual(started["kind"], "worker_started")
                        self.assertGreater(started["pid"], 0)
                        self.assertEqual(events.get(timeout=15), {"kind": "ready"})
                        # fmt: r
                        source = dedent("""
                            stopifnot(identical(
                              normalizePath(path.expand("~")),
                              normalizePath(Sys.getenv("HOME"))
                            ))
                            cat("HOME selected")
                            """).strip()
                        command = {
                            "kind": "evaluate",
                            "language": "r",
                            "source": source,
                        }
                        process.stdin.write(json.dumps(command).encode() + b"\n")
                        process.stdin.flush()
                        output = []
                        while True:
                            event = events.get(timeout=15)
                            self.assertIsNotNone(event, output)
                            if event["kind"] == "completed":
                                break
                            output.append(event)
                        self.assertIn("HOME selected", json.dumps(output))
                    finally:
                        process.stdin.close()
                        process.wait(timeout=10)
                        reader.join(timeout=5)

    def test_retains_failure_discovered_during_exit_drain(self):
        process, kernel, worker, _ = self.start("exit_invalid")
        process.stdin.write(
            json.dumps(LIFECYCLE_COMMANDS["exit_invalid"]).encode() + b"\n"
        )
        process.stdin.flush()
        self.assertEqual(kernel.WaitForSingleObject(worker, 10000), 0)
        events = self.finish(process)
        assert_exit_tail(events, invalid=True)


if __name__ == "__main__":
    unittest.main()
