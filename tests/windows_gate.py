"""Local named-pipe rendezvous for native host fixtures, not sandbox access."""

import ctypes
import math
import subprocess
import time
import uuid
from ctypes import wintypes


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("internal", ctypes.c_size_t),
        ("internal_high", ctypes.c_size_t),
        ("offset", wintypes.DWORD),
        ("offset_high", wintypes.DWORD),
        ("event", wintypes.HANDLE),
    ]


class Gate:
    def __init__(self, timeout: float = 10):
        self.name = rf"\\.\pipe\console-test-gate-{uuid.uuid4().hex}"
        self.timeout = timeout
        self.peer: subprocess.Popen[bytes] | None = None
        self.kernel = kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        handle, dword, pointer = wintypes.HANDLE, wintypes.DWORD, wintypes.LPVOID
        operation = ctypes.POINTER(Overlapped)
        count = ctypes.POINTER(dword)
        for name, arguments, result in (
            ("CreateNamedPipeW", [wintypes.LPCWSTR, *[dword] * 6, pointer], handle),
            (
                "CreateEventW",
                [pointer, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR],
                handle,
            ),
            ("ConnectNamedPipe", [handle, operation], wintypes.BOOL),
            ("ReadFile", [handle, pointer, dword, count, operation], wintypes.BOOL),
            ("WriteFile", [handle, pointer, dword, count, operation], wintypes.BOOL),
            (
                "GetOverlappedResult",
                [handle, operation, count, wintypes.BOOL],
                wintypes.BOOL,
            ),
            ("CancelIoEx", [handle, operation], wintypes.BOOL),
            (
                "WaitForMultipleObjects",
                [dword, ctypes.POINTER(handle), wintypes.BOOL, dword],
                dword,
            ),
            ("CloseHandle", [handle], wintypes.BOOL),
        ):
            function = getattr(kernel, name)
            function.argtypes = arguments
            function.restype = result
        # Duplex, overlapped, first instance; byte mode, local clients only.
        # Default host ACLs and non-inheritable handles. Never grant sandbox
        # accounts access: all users of this gate are trusted host fixtures.
        self.handle = kernel.CreateNamedPipeW(
            self.name, 3 | 0x40000000 | 0x80000, 8, 1, 4096, 4096, 0, None
        )
        if self.handle == ctypes.c_void_p(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle is not None:
            self.kernel.CloseHandle(self.handle)
            self.handle = None

    def _operation(self, function, arguments: tuple, deadline: float) -> int:
        context = f"{function.__name__} on {self.name} (peer PID {getattr(self.peer, 'pid', None)})"
        if time.monotonic() >= deadline:
            raise TimeoutError(f"{context}: deadline expired")
        event = self.kernel.CreateEventW(None, True, False, None)
        if not event:
            raise ctypes.WinError(ctypes.get_last_error())
        overlapped = Overlapped(event=event)
        count = wintypes.DWORD()
        pending = False
        try:
            pending = True
            started = function(self.handle, *arguments, ctypes.byref(overlapped))
            error = ctypes.get_last_error() if not started else 0
            if function is self.kernel.ConnectNamedPipe and error == 535:
                pending = False
                return 0  # The peer connected between creation and accept.
            if error not in (0, 997):
                pending = False
                raise OSError(error, f"{context}: {ctypes.FormatError(error)}")
            handles = [event]
            if self.peer is not None:
                handles.append(int(self.peer._handle))
            handles = (wintypes.HANDLE * len(handles))(*handles)
            result = self.kernel.WaitForMultipleObjects(
                len(handles),
                handles,
                False,
                max(0, math.ceil((deadline - time.monotonic()) * 1000)),
            )
            if result == 258:
                raise TimeoutError(f"{context}: deadline expired")
            if result == 1:
                raise RuntimeError(f"{context}: process exited before rendezvous")
            if result != 0:
                raise ctypes.WinError(ctypes.get_last_error())
            succeeded = self.kernel.GetOverlappedResult(
                self.handle, ctypes.byref(overlapped), ctypes.byref(count), False
            )
            error = ctypes.get_last_error() if not succeeded else 0
            pending = False
            if error:
                raise OSError(error, f"{context}: {ctypes.FormatError(error)}")
            return count.value
        finally:
            if pending:
                # CancelIoEx only submits cancellation. Join completion before
                # releasing the buffer, OVERLAPPED, event, or pipe handle.
                self.kernel.CancelIoEx(self.handle, ctypes.byref(overlapped))
                self.kernel.GetOverlappedResult(
                    self.handle, ctypes.byref(overlapped), ctypes.byref(count), True
                )
            self.kernel.CloseHandle(event)

    def accept(self, peer: subprocess.Popen[bytes]) -> None:
        self.peer = peer
        self._operation(
            self.kernel.ConnectNamedPipe, (), time.monotonic() + self.timeout
        )

    def readline(self) -> bytes:
        deadline = time.monotonic() + self.timeout
        line = bytearray()
        while True:
            buffer = ctypes.create_string_buffer(1)
            count = self._operation(self.kernel.ReadFile, (buffer, 1, None), deadline)
            if count == 0:
                raise EOFError(f"{self.name}: peer closed before a complete line")
            line.extend(buffer.raw)
            if line[-1] == 10:
                return bytes(line)

    def sendall(self, data: bytes) -> None:
        deadline = time.monotonic() + self.timeout
        offset = 0
        while offset < len(data):
            buffer = ctypes.create_string_buffer(data[offset:])
            count = self._operation(
                self.kernel.WriteFile, (buffer, len(data) - offset, None), deadline
            )
            if count == 0:
                raise EOFError(f"{self.name}: peer closed during release")
            offset += count
