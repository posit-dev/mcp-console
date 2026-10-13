"""Build and accept a Windows wheel and source install in a private environment."""

import os
from pathlib import Path
import subprocess
import sys

from windows_sandbox import workspace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from checkout_workflow import checkout_owner, command_process, wait_process


def run(*command, environment=None):
    with command_process(list(map(str, command)), environment=environment) as process:
        status = wait_process(process)
        if status:
            raise subprocess.CalledProcessError(status, command)


if __name__ == "__main__":
    if sys.platform != "win32":
        raise SystemExit("native Windows installation checks require Windows")
    os.chdir(ROOT)
    # Restricted tokens must be able to traverse the installed executable's
    # directory. Private TemporaryDirectory ACLs only admit owner/admin/system.
    with checkout_owner(ROOT), workspace() as directory:
        wheels = directory / "wheels"
        venv = directory / "venv"
        run("uv", "build", "--wheel", "--out-dir", wheels)
        (wheel,) = wheels.glob("*.whl")
        # uv copies Windows commands onto PATH. Exercise that public entry
        # point rather than only the executable inside a virtualenv.
        tool_environment = os.environ | {
            "UV_TOOL_DIR": str(directory / "tools"),
            "UV_TOOL_BIN_DIR": str(directory / "commands"),
            "MCP_CONSOLE_HOME": str(directory / "console-home"),
        }
        run(
            "uv",
            "tool",
            "install",
            "--python",
            sys.executable,
            wheel,
            environment=tool_environment,
        )
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsMxc.test_proxy_launch",
            "WindowsSandbox.test_python_state_and_restart_with_private_temporary_storage",
            "WindowsSandbox.test_stdio_write_policy_and_exit_status",
            environment=tool_environment
            | {"MCP_CONSOLE_TEST_BINARY": str(directory / "commands/mcp-console.exe")},
        )
        run("uv", "venv", "--python", sys.executable, venv)
        python = venv / "Scripts/python.exe"
        run("uv", "pip", "install", "--python", python, wheel)
        environment = os.environ | {
            "MCP_CONSOLE_TEST_BINARY": str(venv / "Scripts/mcp-console.exe"),
            "MCP_CONSOLE_TEST_NATIVE_BINARY": str(venv / "libexec/mcp-console.exe"),
        }
        run(sys.executable, ROOT / "tests/windows.py", "-v", environment=environment)
        run("uv", "pip", "install", "--reinstall", "--python", python, ROOT)
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsConsole.test_python_initializes_before_r",
            "WindowsConsole.test_r_initializes_before_python",
            environment=environment,
        )
