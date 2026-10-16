#!/usr/bin/env -S uv run --script


import json
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_result_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import MACOS_SANDBOX, requires
from support.suites import run_this_suite


def _inspects_sandbox_child_processes(
    binary: Path,
    environment: dict[str, str],
    workspace: Path | None = None,
) -> Transcript:
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(binary, SANDBOXED.serve(), environment, workspace)
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import sys

        initial_executable = sys.executable
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        import ctypes
        import errno
        import os
        import subprocess
        import sys

        import psutil

        mib = (ctypes.c_int * 3)(1, 14, 0)
        size = ctypes.c_size_t()
        ctypes.set_errno(0)
        result = ctypes.CDLL(None, use_errno=True).sysctl(
            mib,
            len(mib),
            None,
            ctypes.byref(size),
            None,
            0,
        )
        denied = result == -1 and ctypes.get_errno() == errno.EPERM

        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        child = subprocess.Popen(command)
        try:
            visible = psutil.pids()
            descendants = psutil.Process().children(recursive=True)
            observed = [process.pid for process in descendants]
            process_group = os.getpgrp()
            visible_groups = [os.getpgid(pid) for pid in visible]
        finally:
            child.terminate()
            child.wait()

        (
            initial_executable != sys.executable,
            denied,
            1 not in visible,
            visible == sorted(visible),
            all(group == process_group for group in visible_groups),
            child.pid in visible,
            child.pid in observed,
        )
        """)
    client.send(python=python, requirements={"python": ["psutil"]})
    output = last_result_text(client)
    assert output == "(True, True, True, True, True, True, True)\n", repr(output)
    return client.finish()


@requires(MACOS_SANDBOX)
def test_inspects_sandbox_child_processes_with_psutil(binary: Path) -> Transcript:
    return _inspects_sandbox_child_processes(binary, os.environ.copy())


@requires(MACOS_SANDBOX)
def test_inspects_child_processes_without_environment_inheritance(
    binary: Path,
) -> Transcript:
    environment, _ = r_test_environment()
    with TemporaryDirectory() as directory:
        workspace = Path(directory)
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "inherit_environment": False,
                    "environment": {
                        name: environment[name]
                        for name in ("R_HOME", "R_PROFILE_USER", "HOME", "PATH")
                    },
                }
            ),
            encoding="utf-8",
        )
        return _inspects_sandbox_child_processes(binary, environment, workspace)


@requires(MACOS_SANDBOX)
def test_retains_environment_when_optional_psutil_setup_fails(
    binary: Path,
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    client = McpClient(
        binary,
        SANDBOXED.serve("-c", "python.managed.resolution=implicit"),
        environment,
    )
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import importlib.machinery
        import os
        import sys

        live_pid = os.getpid()
        live_sentinel = 42


        class FailingPsutilFinder:
            def __init__(self):
                self.visible_lookups = 0

            def find_spec(self, fullname, path=None, target=None):
                if fullname != "psutil":
                    return None
                specification = importlib.machinery.PathFinder.find_spec(
                    fullname,
                    path,
                )
                if specification is None:
                    return None
                self.visible_lookups += 1
                sys.meta_path.remove(self)
                raise ImportError("synthetic psutil probe failure")


        failing_psutil_finder = FailingPsutilFinder()
        sys.meta_path.insert(0, failing_psutil_finder)
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"

    # fmt: python
    python = code("""
        import os
        import subprocess
        import sys

        import psutil

        command = [sys.executable, "-c", "import time; time.sleep(30)"]
        child = subprocess.Popen(command)
        try:
            visible = psutil.pids()
            descendants = psutil.Process().children(recursive=True)
            observed = [process.pid for process in descendants]
            process_group = os.getpgrp()
            visible_groups = [os.getpgid(pid) for pid in visible]
        finally:
            child.terminate()
            child.wait()

        (
            psutil.__name__,
            failing_psutil_finder.visible_lookups,
            live_sentinel,
            os.getpid() == live_pid,
            1 not in visible,
            visible == sorted(visible),
            all(group == process_group for group in visible_groups),
            child.pid in visible,
            child.pid in observed,
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == ("('psutil', 1, 42, True, True, True, True, True, True)\n"), repr(
        output
    )

    client.send(r='"psutil" %in% reticulate::py_require()$packages')
    assert last_result_text(client) == "[1] TRUE\n"

    client.send(control="restart")
    assert last_result_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    client.send(r='"psutil" %in% reticulate::py_require()$packages')
    assert last_result_text(client) == "[1] TRUE\n"
    client.send(python="import psutil; psutil.__name__")
    assert last_result_text(client) == "'psutil'\n"
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
