"""Force the native inherited-procfs path without relying on kernel mount masks."""

import ctypes
import ctypes.util
import errno
from pathlib import Path
import subprocess
import sys
import tempfile


class Comparison(ctypes.Structure):
    _fields_ = [
        ("argument", ctypes.c_uint),
        ("operator", ctypes.c_int),
        ("value", ctypes.c_uint64),
        ("unused", ctypes.c_uint64),
    ]


def main() -> int:
    library = ctypes.CDLL(ctypes.util.find_library("seccomp") or "libseccomp.so.2")
    library.seccomp_init.argtypes = [ctypes.c_uint32]
    library.seccomp_init.restype = ctypes.c_void_p
    library.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    library.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        Comparison,
    ]
    library.seccomp_export_bpf.argtypes = [ctypes.c_void_p, ctypes.c_int]
    library.seccomp_release.argtypes = [ctypes.c_void_p]
    context = library.seccomp_init(0x7FFF0000)  # SCMP_ACT_ALLOW
    assert context
    try:
        mount = library.seccomp_syscall_resolve_name(b"mount")
        assert mount >= 0
        # Bubblewrap mounts procfs with MS_NOSUID | MS_NODEV | MS_NOEXEC (14).
        # Bind/remount and private tmpfs setup remain available. The outer
        # helper installs this filter only after mounting its own procfs.
        assert (
            library.seccomp_rule_add(
                context, 0x50000 | errno.EPERM, mount, 1, Comparison(3, 4, 14, 0)
            )
            == 0
        )  # SCMP_CMP_EQ
        with tempfile.TemporaryFile() as compiled:
            assert library.seccomp_export_bpf(context, compiled.fileno()) == 0
            compiled.seek(0)
            command = sys.argv[1:]
            separator = command.index("--")
            # This case owns generic Linux procfs isolation. Exclude the WSL
            # bridges, whose absence-of-procfs policy is a separate contract.
            masks = ["--tmpfs", "/run"]
            if Path("/mnt/wslg").is_dir():
                masks += ["--tmpfs", "/mnt/wslg"]
            command[separator:separator] = [*masks, "--seccomp", str(compiled.fileno())]
            return subprocess.run(command, pass_fds=(compiled.fileno(),)).returncode
    finally:
        library.seccomp_release(context)


if __name__ == "__main__":
    raise SystemExit(main())
