import ctypes
import errno
import os
import signal
import subprocess
import sys
import time
from typing import Sequence

DarwinProcessIdentity = tuple[int, int, int]


class _DarwinProcessInfo(ctypes.Structure):
    # In Darwin's stable proc_bsdinfo ABI, the two start-time fields follow a
    # 120-byte prefix and complete the 136-byte structure.
    _fields_ = [
        ("prefix", ctypes.c_byte * 120),
        ("pbi_start_tvsec", ctypes.c_uint64),
        ("pbi_start_tvusec", ctypes.c_uint64),
    ]


class _DarwinProcessFdInfo(ctypes.Structure):
    _fields_ = [
        ("fd", ctypes.c_int32),
        ("fdtype", ctypes.c_uint32),
    ]


_LIBPROC = None
if sys.platform == "darwin":
    _LIBPROC = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    _LIBPROC.proc_listchildpids.argtypes = [
        ctypes.c_int,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_listchildpids.restype = ctypes.c_int
    _LIBPROC.proc_pidinfo.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_uint64,
        ctypes.c_void_p,
        ctypes.c_int,
    ]
    _LIBPROC.proc_pidinfo.restype = ctypes.c_int


def current_darwin_process_identity(pid: int) -> DarwinProcessIdentity | None:
    assert _LIBPROC is not None
    proc_pidtbsdinfo = 3
    include_zombies = 1
    info = _DarwinProcessInfo()
    ctypes.set_errno(0)
    size = _LIBPROC.proc_pidinfo(
        pid,
        proc_pidtbsdinfo,
        include_zombies,
        ctypes.byref(info),
        ctypes.sizeof(info),
    )
    if size == ctypes.sizeof(info):
        return (pid, info.pbi_start_tvsec, info.pbi_start_tvusec)
    error = ctypes.get_errno()
    if size == 0 and error == errno.ESRCH:
        return None
    if size == 0 and error != 0:
        raise OSError(error, f"failed to inspect process {pid}")
    raise RuntimeError(
        f"proc_pidinfo returned {size} bytes for process {pid}, "
        f"expected {ctypes.sizeof(info)}"
    )


def capture_darwin_process_identity(pid: int) -> DarwinProcessIdentity:
    identity = current_darwin_process_identity(pid)
    assert identity is not None, f"process {pid} exited before identity capture"
    return identity


def live_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    return [
        identity[0]
        for identity in identities
        if current_darwin_process_identity(identity[0]) == identity
    ]


def darwin_child_process_identities(
    parent: DarwinProcessIdentity,
) -> tuple[DarwinProcessIdentity, ...]:
    assert _LIBPROC is not None
    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process exited before child inspection"
    )
    capacity = 16
    while True:
        child_pids = (ctypes.c_int * capacity)()
        ctypes.set_errno(0)
        count = _LIBPROC.proc_listchildpids(
            parent[0],
            child_pids,
            ctypes.sizeof(child_pids),
        )
        error = ctypes.get_errno()
        if count < 0 or (count == 0 and error != 0):
            raise OSError(error, f"failed to list children of process {parent[0]}")
        if count < capacity:
            break
        capacity *= 2

    assert current_darwin_process_identity(parent[0]) == parent, (
        "parent process changed during child inspection"
    )
    return tuple(capture_darwin_process_identity(pid) for pid in child_pids[:count])


def darwin_process_file_descriptors(
    identity: DarwinProcessIdentity,
) -> set[int]:
    assert _LIBPROC is not None
    assert current_darwin_process_identity(identity[0]) == identity, (
        "process exited before descriptor inspection"
    )
    proc_pidlistfds = 1
    capacity = 16
    while True:
        fd_infos = (_DarwinProcessFdInfo * capacity)()
        ctypes.set_errno(0)
        size = _LIBPROC.proc_pidinfo(
            identity[0],
            proc_pidlistfds,
            0,
            fd_infos,
            ctypes.sizeof(fd_infos),
        )
        error = ctypes.get_errno()
        if size <= 0:
            raise OSError(
                error, f"failed to list descriptors for process {identity[0]}"
            )
        assert size % ctypes.sizeof(_DarwinProcessFdInfo) == 0, size
        count = size // ctypes.sizeof(_DarwinProcessFdInfo)
        if count < capacity:
            break
        capacity *= 2

    assert current_darwin_process_identity(identity[0]) == identity, (
        "process changed during descriptor inspection"
    )
    return {info.fd for info in fd_infos[:count]}


def signal_darwin_process(identity: DarwinProcessIdentity, number: int) -> bool:
    # macOS has no pidfd-like signal API. Recheck the start time immediately
    # before signaling so a reused PID is not treated as the test process.
    if current_darwin_process_identity(identity[0]) != identity:
        return False
    try:
        os.kill(identity[0], number)
    except ProcessLookupError:
        return False
    return True


def kill_darwin_processes(
    identities: Sequence[DarwinProcessIdentity],
) -> list[int]:
    survivors = live_darwin_processes(identities)
    for identity in identities:
        signal_darwin_process(identity, signal.SIGKILL)
    return survivors


def wait_for_darwin_process_state(
    identity: DarwinProcessIdentity,
    prefix: str,
    description: str,
    *,
    timeout: float = 10,
) -> None:
    deadline = time.monotonic() + timeout
    while True:
        assert live_darwin_processes((identity,)) == [identity[0]], (
            f"{description} exited before reaching state {prefix!r}"
        )
        result = subprocess.run(
            ["/bin/ps", "-o", "state=", "-p", str(identity[0])],
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        if result.stdout.strip().startswith(prefix):
            return
        assert time.monotonic() < deadline, (
            f"timed out waiting for {description} state {prefix!r}"
        )
        time.sleep(0.01)
