"""Build and accept a Windows wheel and source install in a private environment."""

import argparse
import os
from pathlib import Path
import subprocess
import sys
import tomllib

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
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--installed-binary",
        type=Path,
        help="accept an already installed release wheel without building from source",
    )
    options = parser.parse_args()
    if sys.platform != "win32":
        raise SystemExit("native Windows installation checks require Windows")
    os.chdir(ROOT)
    if options.installed_binary is not None:
        binary = options.installed_binary.resolve()
        version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
        assert subprocess.check_output([binary, "--version"], text=True).strip() == (
            f"mcp-console {version}"
        )
        run(binary, "--help")
        assert not (binary.parent / "mcp-console-sandbox.exe").exists()
        with workspace() as directory:
            run(
                binary,
                "sandbox",
                "--no-config",
                "--",
                Path(os.environ["SystemRoot"]) / "System32/cmd.exe",
                "/d",
                "/c",
                "exit 0",
                environment=os.environ | {"PATH": str(directory)},
            )
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsSandbox.test_setup_status_is_read_only",
            "WindowsSandbox.test_first_python_send_from_short_profile_directory",
            "WindowsSandbox.test_python_state_and_restart_with_private_temporary_storage",
            "WindowsSandbox.test_elevated_cwd_reuses_existing_metadata_access",
            "WindowsSandbox.test_elevated_cwd_rejects_a_blocked_metadata_grant",
            "WindowsSandbox.test_elevated_cwd_rejects_denied_metadata_with_existing_allow",
            "WindowsSandbox.test_elevated_cwd_reuses_null_dacl",
            "WindowsSandbox.test_stdio_write_policy_and_exit_status",
            "WindowsConsole.test_python_without_r",
            "WindowsConsole.test_r_initializes_before_python",
            "WindowsConsole.test_python_initializes_before_r",
            environment=os.environ | {"MCP_CONSOLE_TEST_BINARY": str(binary)},
        )
        raise SystemExit(0)
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
            "WindowsSandbox.test_setup_status_is_read_only",
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
