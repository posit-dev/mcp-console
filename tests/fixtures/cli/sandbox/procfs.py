#!/usr/bin/env python3
"""Disposable same-user procfs differential probe. No real host data is used."""

import ctypes
from collections.abc import Callable
import fcntl
import json
import os
from pathlib import Path
import signal
import socket
import struct
import subprocess
import sys
import tempfile

libc = ctypes.CDLL(None, use_errno=True)
CLONE_NEWNET = 0x40000000
NS_GET_NSTYPE = 0xB703


def host(directory: str) -> None:
    # Match an ordinary same-user host process. Retaining the driver's
    # capabilities would hide a missing user-namespace boundary in the target.
    header = (ctypes.c_uint32 * 2)(0x20080522, 0)  # capability ABI version 3
    capabilities = (ctypes.c_uint32 * 6)()
    assert libc.capset(ctypes.byref(header), ctypes.byref(capabilities)) == 0
    os.chdir(directory)
    sentinel = ctypes.create_string_buffer(b"synthetic-memory-sentinel")
    file = open("sentinel", "r+b", buffering=0)
    read, write = os.pipe()
    # Do not let Yama hide a ptrace bypass in this disposable fixture.
    assert libc.prctl(3, 0, 0, 0, 0) == 1  # PR_GET_DUMPABLE
    assert libc.prctl(0x59616D61, ctypes.c_ulong(-1), 0, 0, 0) == 0
    signal.signal(signal.SIGUSR1, lambda *_: Path("signalled").touch())
    print(
        json.dumps(
            dict(
                pid=os.getpid(),
                command=Path("/proc/self/cmdline").read_bytes().hex(),
                address=ctypes.addressof(sentinel),
                file=file.fileno(),
                control=write,
            )
        ),
        flush=True,
    )
    sys.stdin.read()
    assert sentinel.value == b"synthetic-memory-sentinel"
    os.close(write)
    assert os.read(read, 100) == b"", "host control endpoint accessed"


def target() -> None:
    # Receive a real namespace descriptor after launch; ordinary inherited
    # descriptors are intentionally closed by the runner. A procfs lookup must
    # not prevent this independent namespace-entry check from reaching setns.
    with socket.socket(
        socket.AF_UNIX, socket.SOCK_SEQPACKET, 0, fileno=os.dup(sys.stdin.fileno())
    ) as channel:
        payload, descriptors, flags, _ = socket.recv_fds(channel, 65536, 1)
    assert len(descriptors) == 1 and flags == 0
    namespace = descriptors[0]
    assert fcntl.ioctl(namespace, NS_GET_NSTYPE) == CLONE_NEWNET
    data = json.loads(payload)
    p = Path(f"/proc/{data['pid']}")
    try:
        visible = (p / "cmdline").read_bytes().hex() == data["command"]
    except FileNotFoundError:
        visible = False
    results = {
        "visible": visible,
        "fresh": int(os.readlink("/proc/self")) == os.getpid(),
    }

    def attempt(name: str, operation: Callable[[], object]) -> None:
        try:
            value = operation()
            results[name] = "allowed" if value is None else str(value)
        except OSError as error:
            results[name] = f"errno:{error.errno}"

    def write(path: str | Path) -> None:
        with open(path, "r+b", buffering=0) as file:
            file.write(b"ESCAPE")

    def mem(mode: str) -> bytes | int:
        with open(p / "mem", mode, buffering=0) as file:
            file.seek(data["address"])
            return file.read(8) if mode == "rb" else file.write(b"ESCAPE")

    def syscall(function: Callable[..., int], *args: object) -> int:
        result = function(*args)
        if result < 0:
            raise OSError(ctypes.get_errno(), "syscall denied")
        return result

    attempt("direct_read", lambda: Path(data["sentinel"]).read_bytes())
    attempt("direct_write", lambda: write(data["sentinel"]))
    attempt("environment", lambda: (p / "environ").read_bytes())
    attempt(
        "root_read", lambda: (p / "root" / data["sentinel"].lstrip("/")).read_bytes()
    )
    attempt("cwd_read", lambda: (p / "cwd/sentinel").read_bytes())
    attempt("fd_read", lambda: (p / f"fd/{data['file']}").read_bytes())
    attempt("root_write", lambda: write(p / "root" / data["sentinel"].lstrip("/")))
    attempt("cwd_write", lambda: write(p / "cwd/sentinel"))
    attempt("fd_write", lambda: write(p / f"fd/{data['file']}"))
    attempt("mem_read", lambda: mem("rb"))
    attempt("mem_write", lambda: mem("r+b"))

    def control() -> None:
        fd = os.open(p / f"fd/{data['control']}", os.O_WRONLY | os.O_NONBLOCK)
        try:
            os.write(fd, b"change-policy")
        finally:
            os.close(fd)

    attempt("control_write", control)
    attempt("signal", lambda: os.kill(data["pid"], signal.SIGUSR1))

    def ptrace() -> None:
        syscall(libc.ptrace, 0x4206, data["pid"], 0, 0)  # PTRACE_SEIZE
        syscall(libc.ptrace, 0x4207, data["pid"], 0, 0)  # PTRACE_INTERRUPT
        os.waitpid(data["pid"], 0)
        syscall(libc.ptrace, 17, data["pid"], 0, 0)

    attempt("ptrace", ptrace)

    class Iovec(ctypes.Structure):
        _fields_ = [("base", ctypes.c_void_p), ("length", ctypes.c_size_t)]

    local = ctypes.create_string_buffer(b"ESCAPE")
    own = Iovec(ctypes.addressof(local), 6)
    other = Iovec(data["address"], 6)
    attempt(
        "vm_read",
        lambda: syscall(
            libc.process_vm_readv,
            data["pid"],
            ctypes.byref(own),
            1,
            ctypes.byref(other),
            1,
            0,
        ),
    )
    attempt(
        "vm_write",
        lambda: syscall(
            libc.process_vm_writev,
            data["pid"],
            ctypes.byref(own),
            1,
            ctypes.byref(other),
            1,
            0,
        ),
    )

    def network() -> None:
        with socket.create_connection(("127.0.0.1", data["port"]), timeout=1) as stream:
            stream.sendall(b"ESCAPE")

    attempt("network", network)
    # readlink checks ptrace access directly, without opening an nsfs handle.
    # Host nsfs/open restrictions cannot replace the procfs permission check.
    attempt("network_namespace_link", lambda: os.readlink(p / "ns/net"))

    def setns() -> None:
        try:
            syscall(libc.setns, namespace, CLONE_NEWNET)
        finally:
            os.close(namespace)
        network()

    attempt("network_setns", setns)
    # Read the accepted runner's initial environment, or acquire its memory or
    # control descriptors. Never scan unrelated processes.
    if "supervisor" in data:
        s = Path(f"/proc/{data['supervisor']}")
        attempt("supervisor_environment", lambda: (s / "environ").read_bytes())
        attempt(
            "supervisor_memory_write", lambda: os.close(os.open(s / "mem", os.O_RDWR))
        )
        attempt("supervisor_fd_metadata", lambda: bool(list((s / "fd").iterdir())))
        attempt(
            "supervisor_fd_link",
            lambda: os.readlink(s / f"fd/{data['supervisor_control']}"),
        )

        def supervisor_control() -> None:
            fd = os.open(
                s / f"fd/{data['supervisor_control']}", os.O_WRONLY | os.O_NONBLOCK
            )
            try:
                os.write(fd, b"change-policy")
            finally:
                os.close(fd)

        attempt("supervisor_control_write", supervisor_control)
        attempt(
            "supervisor_signal", lambda: os.kill(data["supervisor"], signal.SIGUSR1)
        )
    # Changing transport-looking variables after launch cannot change policy.
    os.environ["PROCFS_TEST_CONFIG"] = (
        '{"filesystem":{"kind":"unrestricted"},"network":"enabled"}'
    )
    attempt("policy_replacement_write", lambda: write(data["sentinel"]))
    print(json.dumps(results), flush=True)


def network_baseline() -> None:
    # The outer fixture owns this network namespace and retains CAP_SYS_ADMIN
    # only for the trusted driver. Establish a usable loopback and namespace
    # entry baseline before launching any sandboxed command.
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as control:
        fcntl.ioctl(control, 0x8914, struct.pack("16sH14x", b"lo", 1))  # SIOCSIFFLAGS
    with open("/proc/self/ns/net", "rb") as namespace:
        assert fcntl.ioctl(namespace, NS_GET_NSTYPE) == CLONE_NEWNET
        assert libc.setns(namespace.fileno(), CLONE_NEWNET) == 0, ctypes.get_errno()


def main(binary: str, interface: str, expected_proc: str) -> None:
    network_baseline()
    if os.getpid() == 1:
        # Keep the host fixture above PID 2, which belongs to the target in a
        # fresh inner namespace. Numerical reuse must not probe the target itself.
        subprocess.run(["/bin/true"], check=True)
    with (
        tempfile.TemporaryDirectory() as root,
        socket.socket() as listener,
        open("/proc/self/ns/net", "rb") as namespace,
    ):
        assert fcntl.ioctl(namespace, NS_GET_NSTYPE) == CLONE_NEWNET
        path = Path(root)
        sentinel = path / "sentinel"
        sentinel.write_bytes(b"synthetic-file-sentinel")
        listener.bind(("127.0.0.1", 0))
        listener.listen(16)
        with subprocess.Popen(
            [sys.executable, __file__, "host", root],
            env={"SYNTHETIC_SECRET": "synthetic-env-sentinel"},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
        ) as fixture:
            data = json.loads(fixture.stdout.readline())
            status = Path(f"/proc/{data['pid']}/status").read_text().splitlines()
            for line in status:
                if line.startswith(("CapEff:", "CapPrm:", "CapInh:")):
                    assert int(line.split()[1], 16) == 0, line
            data.update(sentinel=str(sentinel), port=listener.getsockname()[1])
            for read_policy in ("all", "deny-sentinel"):
                entries = [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }
                ]
                if read_policy == "deny-sentinel":
                    entries.append(
                        {"path": {"type": "path", "path": root}, "access": "none"}
                    )
                for network in ("restricted", "enabled"):
                    config = {
                        "filesystem": {"kind": "restricted", "entries": entries},
                        "network": network,
                        "lifecycle": {"private_tmp": {"environment": ["TMPDIR"]}},
                    }
                    command = [binary] + (["sandbox"] if interface == "console" else [])
                    command += [
                        "--runner-config-env"
                        if interface == "console"
                        else "--config-env",
                        "PROCFS_TEST_CONFIG",
                        "--",
                        sys.executable,
                        __file__,
                        "target",
                    ]
                    control_read, control_write = os.pipe()
                    channel, target_input = socket.socketpair(
                        socket.AF_UNIX, socket.SOCK_SEQPACKET
                    )
                    with (
                        channel,
                        target_input,
                        subprocess.Popen(
                            command,
                            pass_fds=(control_write,),
                            stdin=target_input,
                            stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE,
                            text=True,
                            cwd="/",
                            env={
                                "PATH": os.environ.get("PATH", ""),
                                "SYNTHETIC_SECRET": "synthetic-supervisor-sentinel",
                                "PROCFS_TEST_CONFIG": json.dumps(config),
                            },
                        ) as sandbox,
                    ):
                        payload = data | {
                            "supervisor": sandbox.pid,
                            "supervisor_control": control_write,
                        }
                        target_input.close()
                        message = json.dumps(payload).encode()
                        assert socket.send_fds(
                            channel, [message], [namespace.fileno()]
                        ) == len(message)
                        stdout, stderr = sandbox.communicate(timeout=20)
                        assert sandbox.returncode == 0 and not stderr, (
                            sandbox.returncode,
                            stdout,
                            stderr,
                        )
                    os.close(control_write)
                    assert os.read(control_read, 100) == b"", (
                        "supervisor control endpoint accessed"
                    )
                    os.close(control_read)
                    observed = json.loads(stdout)
                    assert observed["fresh"] == (expected_proc == "fresh"), observed
                    assert observed["visible"] == (expected_proc == "inherited"), (
                        observed
                    )
                    for operation, outcome in observed.items():
                        if operation in ("visible", "fresh", "supervisor_fd_metadata"):
                            continue
                        allowed = (
                            operation == "direct_read"
                            and read_policy == "all"
                            or operation == "network"
                            and network == "enabled"
                        )
                        assert outcome.startswith("errno:") != allowed, (
                            operation,
                            outcome,
                            observed,
                        )
                    print(
                        json.dumps(
                            dict(
                                read=read_policy,
                                network=network,
                                proc=expected_proc,
                                operations=observed,
                            )
                        ),
                        flush=True,
                    )
            fixture.stdin.close()
            assert fixture.wait(timeout=5) == 0
        assert sentinel.read_bytes() == b"synthetic-file-sentinel", "host file modified"
        assert not (path / "signalled").exists(), "host process signalled"


if __name__ == "__main__":
    {
        "host": host,
        "target": target,
        "run": main,
        "network-baseline": network_baseline,
    }[sys.argv[1]](*sys.argv[2:])
