#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.jupyter._sandbox import default_sandbox_session
from support.records import Transcript
from support.requirements import (
    ARK,
    JUPYTER,
    JUPYTER_RETICULATE,
    MACOS_SANDBOX,
    R,
    requires,
)
from support.suites import run_this_suite

NETWORK_OUTPUT = (
    "private Unix: roundtrip\n"
    "host TCP: [Errno 1] Operation not permitted\n"
    "external TCP: [Errno 1] Operation not permitted\n"
    "host Unix: [Errno 1] Operation not permitted\n"
    "host Unix through private symlink: [Errno 1] Operation not permitted\n"
)


@requires(JUPYTER, MACOS_SANDBOX)
def test_ipykernel_private_ipc_preserves_default_sandbox(binary: Path) -> Transcript:
    iostream = subprocess.check_output(
        [
            os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"],
            "-c",
            "import ipykernel.iostream; print(ipykernel.iostream.__file__)",
        ],
        text=True,
    ).strip()
    assert Path(iostream).is_absolute() and Path(iostream).is_file(), iostream
    packages = str(Path(iostream).parent.parent)
    return default_sandbox_session(
        binary,
        "ipykernel",
        network_fixture="jupyter_sandbox_network.py",
        network_output=NETWORK_OUTPUT,
        startup_output=(
            f"{iostream}:73: UserWarning: Couldn't bind IOPub Pipe to 127.0.0.1: "
            "Operation not permitted (addr='tcp://127.0.0.1:*')\n"
            "subprocess output will be unavailable.\n"
            "  self._setup_pipe_in()\n"
        ),
        shutdown_output=(
            "[IPKernelApp] ERROR | Exception during subprocesses termination\n"
            "Traceback (most recent call last):\n"
            f'  File "{packages}/ipykernel/kernelbase.py", line 1645, in _at_shutdown\n'
            "    await self._progressively_terminate_all_children()\n"
            f'  File "{packages}/ipykernel/kernelbase.py", line 1623, in _progressively_terminate_all_children\n'
            "    if not self._process_children():\n"
            "           ~~~~~~~~~~~~~~~~~~~~~~^^\n"
            f'  File "{packages}/ipykernel/kernelbase.py", line 1606, in _process_children\n'
            "    all_children = kernel_process.children(recursive=True)\n"
            f'  File "{packages}/psutil/__init__.py", line 978, in children\n'
            "    ppid_map = _ppid_map()\n"
            f'  File "{packages}/psutil/__init__.py", line 256, in _ppid_map\n'
            "    for pid in pids():\n"
            "               ~~~~^^\n"
            f'  File "{packages}/psutil/__init__.py", line 1485, in pids\n'
            "    ret = sorted(_psplatform.pids())\n"
            "                 ~~~~~~~~~~~~~~~~^^\n"
            f'  File "{packages}/psutil/_psosx.py", line 320, in pids\n'
            "    ls = cext.pids()\n"
            "PermissionError: [Errno 1] Operation not permitted (originated from sysctl() malloc 1/3)\n"
        ),
    )


@requires(JUPYTER, ARK, R, MACOS_SANDBOX)
def test_ark_private_ipc_preserves_default_sandbox(binary: Path) -> Transcript:
    return default_sandbox_session(
        binary,
        "ark",
        network_fixture="jupyter_sandbox_network.py",
        network_output=NETWORK_OUTPUT,
    )


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE, MACOS_SANDBOX)
def test_shared_ipykernel_private_ipc_preserves_default_sandbox(
    binary: Path,
) -> Transcript:
    iostream = subprocess.check_output(
        [
            os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"],
            "-c",
            "import ipykernel.iostream; print(ipykernel.iostream.__file__)",
        ],
        text=True,
    ).strip()
    assert Path(iostream).is_absolute() and Path(iostream).is_file(), iostream
    return default_sandbox_session(
        binary,
        "ark",
        network_fixture="jupyter_sandbox_network.py",
        network_output=NETWORK_OUTPUT,
        reticulate_ipykernel=True,
        startup_output=(
            f"{Path(iostream).resolve()}:73: UserWarning: Couldn't bind IOPub Pipe to 127.0.0.1: "
            "Operation not permitted (addr='tcp://127.0.0.1:*')\n"
            "subprocess output will be unavailable.\n"
            "  self._setup_pipe_in()\n"
        ),
    )


if __name__ == "__main__":
    run_this_suite(__file__)
