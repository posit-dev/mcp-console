"""Linux-only namespace fixtures; policy assertions stay in the public suites."""

import ctypes
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys


def retain_system_bwrap(directory: Path, search_path: str | None = None) -> None:
    # Runtime-discovery fixtures may isolate PATH while the native launcher
    # still needs the host's AppArmor-approved system bubblewrap executable.
    link = directory / "bwrap"
    if (bwrap := shutil.which("bwrap", path=search_path)) and not link.exists():
        link.symlink_to(bwrap)


def inherited_procfs_command(command: list[str]) -> list[str]:
    helper = shutil.which("bwrap")
    assert helper is not None, "the outer namespace fixture requires bwrap on PATH"
    return [
        sys.executable,
        str(
            Path(__file__).resolve().parents[1]
            / "fixtures/cli/sandbox/inherited_procfs.py"
        ),
        helper,
        "--unshare-user",
        "--unshare-pid",
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--",
        *command,
    ]


def procfs_probe_command(command: list[str], *, inherited: bool) -> list[str]:
    helper = shutil.which("bwrap")
    assert helper is not None, "the procfs probe requires bwrap on PATH"
    # The trusted driver must be able to enter its disposable network namespace
    # before testing the sandbox. Never grant this capability to the target.
    namespace = [
        helper,
        "--unshare-user",
        "--unshare-pid",
        "--unshare-net",
        "--uid",
        "0",
        "--gid",
        "0",
        "--cap-add",
        "ALL",
        "--bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--",
        *command,
    ]
    if inherited:
        return [
            sys.executable,
            str(
                Path(__file__).resolve().parents[1]
                / "fixtures/cli/sandbox/inherited_procfs.py"
            ),
            *namespace,
        ]
    return namespace


def procfs_network_available() -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    command = procfs_probe_command(
        [
            sys.executable,
            str(Path(__file__).resolve().parents[1] / "fixtures/cli/sandbox/procfs.py"),
            "network-baseline",
        ],
        inherited=False,
    )
    return subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def inherited_procfs_available() -> bool:
    if sys.platform != "linux" or shutil.which("bwrap") is None:
        return False
    # The fixture denies fresh procfs mounts without preventing the inner
    # helper from creating the namespaces used by inherited-procfs execution.
    command = inherited_procfs_command(
        [
            shutil.which("bwrap"),
            "--unshare-user",
            "--unshare-pid",
            "--ro-bind",
            "/",
            "/",
            "--",
            "/bin/true",
        ]
    )
    return subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def nested_namespaces_available() -> bool:
    helper = shutil.which("bwrap")
    if sys.platform != "linux" or helper is None:
        return False
    # Root metadata uses ordinary nested namespaces, without the seccomp
    # filter that forces inherited-procfs execution in the separate fixture.
    command = [
        helper,
        "--unshare-user",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--dev",
        "/dev",
        "--proc",
        "/proc",
        "--",
        helper,
        "--unshare-user",
        "--unshare-pid",
        "--ro-bind",
        "/",
        "/",
        "--",
        "/bin/true",
    ]
    return subprocess.run(command, capture_output=True, timeout=10).returncode == 0


def fresh_procfs_available() -> bool:
    helper = shutil.which("bwrap")
    if sys.platform != "linux" or helper is None:
        return False
    return (
        subprocess.run(
            [
                helper,
                "--unshare-user",
                "--unshare-pid",
                "--ro-bind",
                "/",
                "/",
                "--proc",
                "/proc",
                "--",
                "/bin/true",
            ],
            capture_output=True,
            timeout=10,
        ).returncode
        == 0
    )


def process_events_available() -> bool:
    if sys.platform != "linux" or not hasattr(os, "pidfd_open"):
        return False
    try:
        Path("/proc/self/stat").read_text()
        Path(f"/proc/self/task/{os.getpid()}/children").read_text()
        descriptor = os.pidfd_open(os.getpid())
        try:
            signal.pidfd_send_signal(descriptor, 0)
        finally:
            os.close(descriptor)
        library = ctypes.CDLL(None, use_errno=True)
        descriptor = library.inotify_init1(os.O_CLOEXEC)
        if descriptor < 0:
            return False
        os.close(descriptor)
    except OSError:
        return False
    return True


def root_metadata_prefix(directory: Path) -> list[str]:
    # Native root-write policies protect these names even when absent. Supply
    # read-only mount targets in an outer namespace, without creating host /.*
    # directories or changing the policy under test.
    metadata = directory / "metadata"
    metadata.mkdir()
    helper = shutil.which("bwrap")
    assert helper is not None
    # A bind of the host root cannot create absent mount targets as an ordinary
    # user. Reconstruct its top-level view on a disposable namespace root.
    command = [helper, "--unshare-user", "--unshare-pid", "--tmpfs", "/"]
    for entry in sorted(Path("/").iterdir()):
        if entry.name in {".git", ".agents", ".codex", "dev", "proc"}:
            continue
        if entry.is_symlink():
            command.extend(["--symlink", os.readlink(entry), str(entry)])
        else:
            command.extend(["--bind", str(entry), str(entry)])
    command.extend(["--dev", "/dev", "--proc", "/proc"])
    for name in (".git", ".agents", ".codex"):
        command.extend(["--ro-bind", str(metadata), f"/{name}"])
    return [*command, "--"]
