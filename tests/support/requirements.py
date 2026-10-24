"""Capabilities of the implemented runtime and of the host test facilities."""

import os
import platform
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar

from support.linux_sandbox import (
    fresh_procfs_available,
    inherited_procfs_available,
    nested_namespaces_available,
    procfs_network_available,
    process_events_available,
)


@dataclass(frozen=True)
class Requirement:
    name: str
    available: bool
    reason: str


# Keep implementation availability here until the corresponding runtime lands.
WORKER = Requirement(
    "worker",
    sys.platform in {"darwin", "linux", "win32"},
    "requires a supported local worker",
)
# Match runtime selection so invalid R_HOME and broken PATH entries report errors.
R = Requirement(
    "R",
    "R_HOME" in os.environ
    or (
        "PATH" in os.environ
        and any(
            os.path.lexists(Path(directory) / name)
            for directory in os.environ["PATH"].split(os.pathsep)
            for name in (("R.exe", "R.bat", "R.cmd") if os.name == "nt" else ("R",))
        )
    ),
    "requires R_HOME or R on PATH",
)
SANDBOX = Requirement(
    "sandbox",
    sys.platform in {"darwin", "linux"},
    "requires Seatbelt/bubblewrap fixtures; Windows policy uses native acceptance",
)
MACOS_SANDBOX = Requirement(
    "macOS sandbox",
    sys.platform == "darwin",
    "requires Seatbelt policy or kqueue supervision",
)
LINUX_SANDBOX = Requirement(
    "Linux sandbox",
    sys.platform == "linux",
    "requires Linux namespace isolation",
)
NESTED_PROCFS = Requirement(
    "nested procfs fixture",
    inherited_procfs_available(),
    "requires bwrap, libseccomp, and permission for nested user and PID namespaces",
)
NESTED_NAMESPACES = Requirement(
    "nested namespaces fixture",
    nested_namespaces_available(),
    "requires bwrap and permission for nested user and PID namespaces",
)
FRESH_PROCFS = Requirement(
    "fresh procfs fixture",
    fresh_procfs_available(),
    "requires an outer bwrap fixture and permission to mount namespace-local procfs",
)
PROCFS_NETWORK = Requirement(
    "procfs network fixture",
    procfs_network_available(),
    "requires a disposable root user/network namespace with usable loopback and setns",
)
POSIX = Requirement(
    "POSIX", os.name == "posix", "requires POSIX processes and descriptors"
)
PROCESS_EVENTS = Requirement(
    "process events",
    sys.platform == "darwin" or process_events_available(),
    "requires macOS process events or Linux procfs, inotify, and pidfds",
)
NATIVE_FIXTURES = Requirement(
    "native fixtures",
    sys.platform in {"darwin", "linux"},
    "requires macOS or Linux native fixture compilation and interposition",
)
NATIVE_LOADER_INSPECTION = Requirement(
    "native loader inspection",
    sys.platform == "darwin"
    or (sys.platform == "linux" and shutil.which("ldd") is not None),
    "requires macOS system loaders or Linux ldd",
)
PTHREAD_RUNTIME_PARKING = Requirement(
    "pthread runtime parking",
    sys.platform == "darwin",
    "requires macOS pthread condition-variable runtime parking",
)
PTHREAD_MUTEX_INTERPOSITION = Requirement(
    "pthread mutex interposition",
    sys.platform == "darwin",
    "requires macOS pthread mutex recording checkpoints",
)
# XNU's bsd/dev/arm/unix_signal.c reports SEGV_ACCERR for every SIGSEGV,
# including null access. Keep these real kernel diagnostics in separate cases.
NULL_FAULT_ACCERR = Requirement(
    "null fault with SEGV_ACCERR",
    sys.platform == "darwin" and platform.machine() == "arm64",
    "requires ARM macOS null-fault diagnostics",
)
NULL_FAULT_MAPERR = Requirement(
    "null fault with SEGV_MAPERR",
    POSIX.available and WORKER.available and not NULL_FAULT_ACCERR.available,
    "ARM macOS reports SEGV_ACCERR for null faults",
)
NO_WORKER = Requirement(
    "unsupported worker", not WORKER.available, "workers are available on this platform"
)
NO_SANDBOX = Requirement(
    "unsupported sandbox",
    sys.platform not in {"darwin", "linux", "win32"},
    "the sandbox is available on this platform",
)

SQL = Requirement("SQL", WORKER.available, "requires a supported built-in SQL worker")
JUPYTER = Requirement(
    "Jupyter kernel dependencies",
    POSIX.available and bool(os.environ.get("MCP_CONSOLE_TEST_JUPYTER_PYTHON")),
    "requires POSIX and MCP_CONSOLE_TEST_JUPYTER_PYTHON with jupyter_client and ipykernel",
)
ARK = Requirement(
    "ark kernel",
    bool(os.environ.get("MCP_CONSOLE_TEST_ARK")),
    "requires MCP_CONSOLE_TEST_ARK selecting a published ark executable",
)
R_EVENT_LOOP = Requirement(
    "R event loop",
    WORKER.available,
    "requires a supported built-in worker host",
)
SYSTEM_PYTHON = Path("/usr/bin/python3")
FRAMEWORK_PYTHON = Path(
    "/Library/Frameworks/Python.framework/Versions/Current/bin/python3"
)
PYTHON_FRAMEWORK = Requirement(
    "framework Python",
    FRAMEWORK_PYTHON.is_file(),
    "requires a macOS framework Python installation",
)
OLD_PYTHON_EXECUTABLE = Path(
    os.environ.get("MCP_CONSOLE_TEST_OLD_PYTHON", SYSTEM_PYTHON)
)
OLD_PYTHON = Requirement(
    "Python before 3.10",
    (sys.platform == "darwin" or "MCP_CONSOLE_TEST_OLD_PYTHON" in os.environ)
    and OLD_PYTHON_EXECUTABLE.is_file(),
    "requires macOS system Python or MCP_CONSOLE_TEST_OLD_PYTHON selecting Python 3.9",
)

SYSTEM_FONT_DIRECTORY = Path("/System/Library/Fonts")
SYSTEM_FONTS = Requirement(
    "system font discovery",
    sys.platform == "darwin" and SYSTEM_FONT_DIRECTORY.is_dir(),
    "requires macOS system_profiler font discovery",
)


def command(name: str) -> Requirement:
    return Requirement(
        name, shutil.which(name) is not None, f"{name} is missing from PATH"
    )


def case_insensitive_paths() -> Requirement:
    with TemporaryDirectory() as directory:
        probe = Path(directory) / "lowercase"
        probe.mkdir()
        return Requirement(
            "case-insensitive paths",
            probe.with_name("LOWERCASE").is_dir(),
            "requires a case-insensitive temporary filesystem",
        )


def windows_short_paths() -> Requirement:
    available = False
    if os.name == "nt":
        from support.python import windows_short_path

        with TemporaryDirectory(prefix="console-short-path-") as directory:
            available = windows_short_path(Path(directory)) != Path(directory)
    return Requirement(
        "Windows 8.3 paths",
        available,
        "requires Windows short names on the temporary filesystem",
    )


def gnu_tar() -> Requirement:
    executable = shutil.which("tar")
    version = (
        subprocess.run(
            [executable, "--version"], capture_output=True, text=True, timeout=10
        ).stdout
        if executable
        else ""
    )
    return Requirement("GNU tar", "GNU tar" in version, "requires GNU tar on PATH")


def joblib_processes() -> Requirement:
    from joblib import cpu_count

    return Requirement(
        "joblib process pool",
        cpu_count() >= 2,
        "requires at least two effective joblib CPUs",
    )


Case = TypeVar("Case", bound=Callable)


def requires(*requirements: Requirement) -> Callable[[Case], Case]:
    def decorate(case: Case) -> Case:
        case.requirements = (*getattr(case, "requirements", ()), *requirements)
        return case

    return decorate


def missing_reasons(requirements: tuple[Requirement, ...]) -> str:
    return "; ".join(
        f"{requirement.name}: {requirement.reason}"
        for requirement in dict.fromkeys(requirements)
        if not requirement.available
    )


# ELF loader and seccomp fixtures exercise Linux implementation details.
LINUX_NATIVE = Requirement(
    "Linux native fixtures",
    sys.platform == "linux",
    "requires Linux ELF loading and seccomp",
)

NON_UTF8_FILENAMES = Requirement(
    "non-UTF-8 filenames",
    sys.platform == "linux",
    "requires Linux; macOS rejects non-UTF-8 filenames",
)


UNPRIVILEGED = Requirement(
    "unprivileged filesystem access",
    os.name == "posix" and os.geteuid() != 0,
    "requires POSIX permission fixtures without root bypass",
)

FILE_READ_DENIAL = Requirement(
    "file read denial",
    os.name == "nt" or UNPRIVILEGED.available,
    "requires Windows byte-range locks or POSIX permissions without root bypass",
)
