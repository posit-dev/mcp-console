"""Inject native startup failures in debugged test children, without host setup.

Only the verified companion's private process memory is changed. DLLs on disk,
accounts, credentials, firewall rules, and the parent process remain untouched.
Windows x64 is the supported native acceptance target.
"""

import ctypes
from ctypes import wintypes as w
from pathlib import Path
import struct
import subprocess
from threading import Thread
import time


def run_with_native_fault(
    command, fault, *, cwd, env, input=b"", interaction=None, timeout=40
):
    assert fault in ("mxc_unavailable", "legacy_accounts")
    assert ctypes.sizeof(ctypes.c_void_p) == 8, "native Windows acceptance requires x64"
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    pointer = ctypes.c_void_p
    size = ctypes.c_size_t

    def api(name, result, *arguments):
        function = getattr(kernel, name)
        function.restype = result
        function.argtypes = arguments
        return function

    close = api("CloseHandle", w.BOOL, w.HANDLE)
    filename = api(
        "GetFinalPathNameByHandleW", w.DWORD, w.HANDLE, w.LPWSTR, w.DWORD, w.DWORD
    )
    module_filename = api("GetModuleFileNameW", w.DWORD, w.HMODULE, w.LPWSTR, w.DWORD)
    module_at = api(
        "GetModuleHandleExW", w.BOOL, w.DWORD, pointer, ctypes.POINTER(w.HMODULE)
    )
    export = api("GetProcAddress", pointer, w.HMODULE, ctypes.c_char_p)
    protect = api(
        "VirtualProtectEx",
        w.BOOL,
        w.HANDLE,
        pointer,
        size,
        w.DWORD,
        ctypes.POINTER(w.DWORD),
    )
    write = api(
        "WriteProcessMemory",
        w.BOOL,
        w.HANDLE,
        pointer,
        pointer,
        size,
        ctypes.POINTER(size),
    )
    flush = api("FlushInstructionCache", w.BOOL, w.HANDLE, pointer, size)
    terminate = api("TerminateProcess", w.BOOL, w.HANDLE, w.UINT)

    class CreateProcess(ctypes.Structure):
        _fields_ = [
            ("file", w.HANDLE),
            ("process", w.HANDLE),
            ("thread", w.HANDLE),
            ("base", pointer),
            ("debug_offset", w.DWORD),
            ("debug_size", w.DWORD),
            ("tls", pointer),
            ("start", pointer),
            ("image", pointer),
            ("unicode", w.WORD),
        ]

    class LoadDll(ctypes.Structure):
        _fields_ = [
            ("file", w.HANDLE),
            ("base", pointer),
            ("debug_offset", w.DWORD),
            ("debug_size", w.DWORD),
            ("image", pointer),
            ("unicode", w.WORD),
        ]

    class EventData(ctypes.Union):
        _fields_ = [
            ("create", CreateProcess),
            ("dll", LoadDll),
            ("thread", w.HANDLE),
            ("exception", w.DWORD),
            ("storage", ctypes.c_byte * 160),
        ]

    class Event(ctypes.Structure):
        _fields_ = [
            ("kind", w.DWORD),
            ("pid", w.DWORD),
            ("tid", w.DWORD),
            ("data", EventData),
        ]

    wait = api("WaitForDebugEvent", w.BOOL, ctypes.POINTER(Event), w.DWORD)
    resume = api("ContinueDebugEvent", w.BOOL, w.DWORD, w.DWORD, w.DWORD)

    def checked(result):
        if not result:
            raise ctypes.WinError(ctypes.get_last_error())
        return result

    def path_for(handle):
        buffer = ctypes.create_unicode_buffer(32768)
        count = checked(filename(handle, buffer, len(buffer), 0))
        assert count < len(buffer)
        return buffer.value

    loaded = {}

    def remote_export(modules, library, name):
        # Resolve forwarded exports by their actual containing DLL, then use
        # the debug events' target base. Do not assume matching ASLR addresses.
        if library not in loaded:
            loaded[library] = ctypes.WinDLL(library, use_last_error=True)
        local = loaded[library]
        address = checked(export(local._handle, name.encode()))
        owner = w.HMODULE()
        checked(module_at(6, address, ctypes.byref(owner)))
        path = ctypes.create_unicode_buffer(32768)
        checked(module_filename(owner, path, len(path)))
        name = Path(path.value).name.lower()
        if name not in modules:
            return None
        return modules[name] + address - owner.value

    patched = set()

    def inject(pid, state):
        handle, image, modules = state
        if image != "mcp-console-sandbox.exe" or pid in patched:
            return
        library, function = (
            ("processmodel.dll", "CreateProcessSecurityEnvironment")
            if fault == "mxc_unavailable"
            else ("netapi32.dll", "NetUserGetInfo")
        )
        if library not in modules:
            return
        address = remote_export(modules, library, function)
        if address is None:
            return
        if fault == "mxc_unavailable":
            payload = (
                b"\xb8" + struct.pack("<I", 0x80070032) + b"\xc3"
            )  # HRESULT_FROM_WIN32(ERROR_NOT_SUPPORTED)
        else:
            allocate = remote_export(modules, library, "NetApiBufferAllocate")
            if allocate is None:
                return
            # NetUserGetInfo(level=1): allocate a real NetAPI-owned USER_INFO_1
            # for the native guard, set flags=0, and return success. The guard
            # reads only flags and frees through NetApiBufferFree as usual.
            payload = (
                b"\x53\x48\x83\xec\x20\x4c\x89\xcb\xb9\x38\x00\x00\x00\x4c\x89\xca\x48\xb8"
                + struct.pack("<Q", allocate)
                + b"\xff\xd0\x85\xc0\x75\x0a\x48\x8b\x13\xc7\x42\x28\x00\x00\x00\x00\x48\x83\xc4\x20\x5b\xc3"
            )
        previous = w.DWORD()
        checked(protect(handle, address, len(payload), 0x40, ctypes.byref(previous)))
        try:
            written = size()
            checked(
                write(handle, address, payload, len(payload), ctypes.byref(written))
            )
            assert written.value == len(payload)
            checked(flush(handle, address, len(payload)))
        finally:
            ignored = w.DWORD()
            checked(
                protect(
                    handle, address, len(payload), previous.value, ctypes.byref(ignored)
                )
            )
        patched.add(pid)

    process = subprocess.Popen(
        command,
        cwd=cwd,
        env=env,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=1 | subprocess.CREATE_NO_WINDOW,
    )  # DEBUG_PROCESS: include descendants
    output = []
    failures = []

    def communicate():
        try:
            output.append(
                interaction(process) if interaction else process.communicate(input)
            )
        except BaseException as error:
            failures.append(error)
            process.kill()

    reader = Thread(target=communicate, daemon=True)
    reader.start()
    processes = {}
    deadline = time.monotonic() + timeout

    def event_step(*, injecting):
        event = Event()
        if not wait(ctypes.byref(event), 1000):
            assert ctypes.get_last_error() == 121, ctypes.WinError(
                ctypes.get_last_error()
            )
            return
        continuation = 0x00010002  # DBG_CONTINUE
        try:
            if event.kind == 3:  # CREATE_PROCESS_DEBUG_EVENT
                info = event.data.create
                try:
                    processes[event.pid] = (
                        info.process,
                        Path(path_for(info.file)).name.lower(),
                        {},
                    )
                finally:
                    close(info.file)
            elif event.kind == 6:  # LOAD_DLL_DEBUG_EVENT
                info = event.data.dll
                if info.file:
                    try:
                        processes[event.pid][2][
                            Path(path_for(info.file)).name.lower()
                        ] = info.base
                    finally:
                        close(info.file)
            elif event.kind == 5:  # EXIT_PROCESS_DEBUG_EVENT
                processes.pop(event.pid)
                # ContinueDebugEvent closes the debug-event process/thread
                # handles on exit. Closing them ourselves can close unrelated
                # handles recycled by Python's pipe-reader threads.
            elif event.kind == 1 and event.data.exception != 0x80000003:
                continuation = 0x80010001  # DBG_EXCEPTION_NOT_HANDLED
            if injecting and event.pid in processes:
                inject(event.pid, processes[event.pid])
        finally:
            checked(resume(event.pid, event.tid, continuation))

    try:
        while reader.is_alive() or processes:
            assert time.monotonic() < deadline, "debugged public launch did not retire"
            event_step(injecting=True)
        if failures:
            raise failures[0]
        assert patched, "fault did not reach the verified native runner"
        return subprocess.CompletedProcess(command, process.returncode, *output[0])
    finally:
        if reader.is_alive() or processes:
            process.kill()
            for handle, _, _ in list(processes.values()):
                terminate(handle, 1)
            deadline = time.monotonic() + 10
            while processes and time.monotonic() < deadline:
                event_step(injecting=False)
        reader.join(timeout=10)
        assert not processes and not reader.is_alive(), (
            "debugged launch cleanup was not confirmed"
        )
