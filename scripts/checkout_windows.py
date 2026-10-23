"""Native checkout locks and process ownership for Windows development tools."""

import ctypes
from ctypes import wintypes as w
import msvcrt
import os
import select
import signal
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from threading import Thread

kernel = ctypes.WinDLL("kernel32", use_last_error=True)


def api(name, result, *arguments):
    function = getattr(kernel, name)
    function.restype = result
    function.argtypes = arguments
    return function


def checked(result):
    if not result:
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class Overlapped(ctypes.Structure):
    _fields_ = [
        ("Internal", ctypes.c_size_t),
        ("InternalHigh", ctypes.c_size_t),
        ("Offset", w.DWORD),
        ("OffsetHigh", w.DWORD),
        ("hEvent", w.HANDLE),
    ]


def lock_file(descriptor, *, wait=False):
    lock = api(
        "LockFileEx",
        w.BOOL,
        w.HANDLE,
        w.DWORD,
        w.DWORD,
        w.DWORD,
        w.DWORD,
        ctypes.POINTER(Overlapped),
    )
    if not lock(
        msvcrt.get_osfhandle(descriptor),
        2 | (0 if wait else 1),
        0,
        1,
        0,
        ctypes.byref(Overlapped()),
    ):
        error = ctypes.get_last_error()
        if error == 33:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError("checkout is locked")
        raise ctypes.WinError(error)


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("PerProcessUserTimeLimit", ctypes.c_longlong),
        ("PerJobUserTimeLimit", ctypes.c_longlong),
        ("LimitFlags", w.DWORD),
        ("MinimumWorkingSetSize", ctypes.c_size_t),
        ("MaximumWorkingSetSize", ctypes.c_size_t),
        ("ActiveProcessLimit", w.DWORD),
        ("Affinity", ctypes.c_size_t),
        ("PriorityClass", w.DWORD),
        ("SchedulingClass", w.DWORD),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("BasicLimitInformation", BasicLimits),
        ("IoInfo", ctypes.c_ulonglong * 6),
        ("ProcessMemoryLimit", ctypes.c_size_t),
        ("JobMemoryLimit", ctypes.c_size_t),
        ("PeakProcessMemoryUsed", ctypes.c_size_t),
        ("PeakJobMemoryUsed", ctypes.c_size_t),
    ]


class Accounting(ctypes.Structure):
    _fields_ = [
        ("Times", ctypes.c_longlong * 4),
        ("TotalPageFaultCount", w.DWORD),
        ("TotalProcesses", w.DWORD),
        ("ActiveProcesses", w.DWORD),
        ("TotalTerminatedProcesses", w.DWORD),
    ]


class CompletionPort(ctypes.Structure):
    _fields_ = [("CompletionKey", ctypes.c_void_p), ("CompletionPort", w.HANDLE)]


close = api("CloseHandle", w.BOOL, w.HANDLE)
set_information = api(
    "SetInformationJobObject", w.BOOL, w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD
)
query_information = api(
    "QueryInformationJobObject",
    w.BOOL,
    w.HANDLE,
    ctypes.c_int,
    ctypes.c_void_p,
    w.DWORD,
    ctypes.c_void_p,
)


def wait_process(process):
    # A Windows process-handle wait does not dispatch Python signal handlers.
    # Wake a socket wait on either process exit or a console cancellation.
    reader, writer = socket.socketpair()
    writer.setblocking(False)
    previous = signal.set_wakeup_fd(writer.fileno())

    def exited():
        process.wait()
        try:
            writer.send(b"1")
        except OSError:
            pass  # Cancellation already closed the notification socket.

    try:
        Thread(target=exited, daemon=True).start()
        select.select([reader], [], [])
        return process.wait()
    finally:
        signal.set_wakeup_fd(previous)
        reader.close()
        writer.close()


@contextmanager
def command_process(
    command, *, log=None, environment=None, directory=None, interruptible=False
):
    job = checked(
        api("CreateJobObjectW", w.HANDLE, ctypes.c_void_p, w.LPCWSTR)(None, None)
    )
    port = process = None
    try:
        limits = ExtendedLimits()
        limits.BasicLimitInformation.LimitFlags = 0x2000  # KILL_ON_JOB_CLOSE
        checked(set_information(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)))
        port = checked(
            api(
                "CreateIoCompletionPort",
                w.HANDLE,
                w.HANDLE,
                w.HANDLE,
                ctypes.c_size_t,
                w.DWORD,
            )(w.HANDLE(-1), None, 0, 1)
        )
        association = CompletionPort(None, port)
        checked(
            set_information(
                job, 7, ctypes.byref(association), ctypes.sizeof(association)
            )
        )
        os.set_handle_inheritable(job, True)
        startup = subprocess.STARTUPINFO(lpAttributeList={"handle_list": [job]})
        # A virtualenv redirector would retain the inherited Job handle outside
        # the Job. This stdlib helper must use the base interpreter directly.
        # The helper enters the Job and closes its inherited Job handle before
        # running the command. Even owner death during startup cannot let a
        # mutator escape: closing the last handle then kills the helper itself.
        process = subprocess.Popen(
            [
                sys._base_executable,
                str(Path(__file__).resolve()),
                str(job),
                *(["--interruptible"] if interruptible else []),
                *command,
            ],
            stdin=sys.stdin,
            stdout=sys.stdout if log is None else log,
            stderr=sys.stderr if log is None else subprocess.STDOUT,
            env=environment,
            cwd=directory,
            startupinfo=startup,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        os.set_handle_inheritable(job, False)
        try:
            yield process
        finally:
            # First retire the helper, including a helper still entering the
            # Job, so it cannot launch work after TerminateJobObject.
            if process.poll() is None:
                process.kill()
            process.wait()
            checked(api("TerminateJobObject", w.BOOL, w.HANDLE, w.UINT)(job, 1))
            accounting = Accounting()
            message, key, overlapped = w.DWORD(), ctypes.c_size_t(), ctypes.c_void_p()
            receive = api(
                "GetQueuedCompletionStatus",
                w.BOOL,
                w.HANDLE,
                ctypes.POINTER(w.DWORD),
                ctypes.POINTER(ctypes.c_size_t),
                ctypes.POINTER(ctypes.c_void_p),
                w.DWORD,
            )
            while True:
                checked(
                    query_information(
                        job,
                        1,
                        ctypes.byref(accounting),
                        ctypes.sizeof(accounting),
                        None,
                    )
                )
                if accounting.ActiveProcesses == 0:
                    break
                # Completion messages are advisory. Recheck accounting after a
                # bounded event wait in case Windows dropped a notification.
                if not receive(
                    port,
                    ctypes.byref(message),
                    ctypes.byref(key),
                    ctypes.byref(overlapped),
                    5000,
                ):
                    if ctypes.get_last_error() != 258:  # WAIT_TIMEOUT
                        checked(False)
    finally:
        close(job)
        if port is not None:
            close(port)


if __name__ == "__main__":
    handle = int(sys.argv[1])
    checked(
        api("AssignProcessToJobObject", w.BOOL, w.HANDLE, w.HANDLE)(
            handle, api("GetCurrentProcess", w.HANDLE)()
        )
    )
    checked(close(handle))
    command = sys.argv[2:]
    if command[0] == "--interruptible":
        # The case interpreter handles CTRL_BREAK and owns its finally blocks.
        # Keep its Job supervisor alive throughout that graceful cleanup.
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
        command = command[1:]
    raise SystemExit(subprocess.call(command))
