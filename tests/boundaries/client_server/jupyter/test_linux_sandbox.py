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
    LINUX_SANDBOX,
    R,
    requires,
)
from support.suites import run_this_suite


NETWORK_OUTPUT = (
    "host TCP: [Errno 111] Connection refused\n"
    "external TCP: [Errno 101] Network is unreachable\n"
    "host Unix: [Errno 1] Operation not permitted\n"
    "external HTTP through proxy: denied (403)\n"
    "external CONNECT through proxy: denied (403)\n"
    "host CONNECT through proxy: denied (403)\n"
)
TCP_WARNING = (
    "[IPKernelApp] WARNING | Kernel is running over TCP without encryption. "
    "All communication (including code and outputs) is sent in plain text and "
    "is susceptible to eavesdropping. Use IPC transport or launch with kernel "
    "manager-provisioned CurveZMQ keys to enable transport encryption.\n"
)


def linux_session(
    binary: Path, kernel: str, *, reticulate_ipykernel: bool = False
) -> Transcript:
    localinterfaces = subprocess.check_output(
        [
            os.environ["MCP_CONSOLE_TEST_JUPYTER_PYTHON"],
            "-c",
            "import jupyter_client.localinterfaces; print(jupyter_client.localinterfaces.__file__)",
        ],
        text=True,
    ).strip()
    assert Path(localinterfaces).is_absolute() and Path(localinterfaces).is_file(), (
        localinterfaces
    )
    warning = (
        f"{localinterfaces}:58: UserWarning: Unexpected error discovering local "
        "network interfaces: [Errno 1] Operation not permitted\n"
        "  ret = f(**kwargs)\n"
    )
    startup_output = warning
    if kernel == "ipykernel":
        startup_output += warning + TCP_WARNING
    elif reticulate_ipykernel:
        startup_output += warning + TCP_WARNING.replace("[IPKernelApp]", "[SharedApp]")
    return default_sandbox_session(
        binary,
        kernel,
        network_fixture="jupyter_sandbox_network_linux.py",
        network_output=NETWORK_OUTPUT,
        startup_output=startup_output,
        reticulate_ipykernel=reticulate_ipykernel,
        # The published `python -m ipykernel install` adds this interpreter flag.
        python_arguments=("-Xfrozen_modules=off",) if kernel == "ipykernel" else (),
    )


@requires(JUPYTER, LINUX_SANDBOX)
def test_ipykernel_private_network_preserves_default_sandbox(
    binary: Path,
) -> Transcript:
    return linux_session(binary, "ipykernel")


@requires(JUPYTER, ARK, R, LINUX_SANDBOX)
def test_ark_private_network_preserves_default_sandbox(binary: Path) -> Transcript:
    return linux_session(binary, "ark")


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE, LINUX_SANDBOX)
def test_shared_ipykernel_private_network_preserves_default_sandbox(
    binary: Path,
) -> Transcript:
    return linux_session(binary, "ark", reticulate_ipykernel=True)


if __name__ == "__main__":
    run_this_suite(__file__)
