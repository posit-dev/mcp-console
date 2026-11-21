"""Accept a Windows release archive, installed wheel, or source installation."""

import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import zipfile
import tomllib

from windows_sandbox import workspace
from windows_distribution import (
    NATIVE_SUFFIXES,
    inspect_artifacts,
    inspect_installation,
    inspect_wheel,
)

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from checkout_workflow import checkout_owner, command_process, wait_process


def run(*command, environment=None):
    with command_process(list(map(str, command)), environment=environment) as process:
        status = wait_process(process)
        if status:
            raise subprocess.CalledProcessError(status, command)


def artifact_path(value: str) -> Path:
    # Resolve user input before the acceptance checks switch to the checkout.
    return Path(value).resolve()


def accept(binary: Path, native: Path, *, preinstalled_r: bool) -> None:
    version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    environment = os.environ | {"PATH": ""}
    actual_version = subprocess.check_output(
        [binary, "--version"], text=True, env=environment
    ).strip()
    version_pattern = re.escape(f"mcp-console {version}")
    if version.endswith("+dev"):
        version_pattern += r"(?:\.g[0-9a-f]{12})?"
    assert re.fullmatch(version_pattern, actual_version), actual_version
    run(binary, "--help", environment=environment)
    assert not (binary.parent / "mcp-console-sandbox.exe").exists()
    from support.windows_mxc import mxc_requirement

    if mxc_requirement(binary).available:
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
    cases = [
        "WindowsMxc.test_proxy_launch",
        "WindowsMxc.test_startup_failures_withhold_code_at_every_public_entry_point",
        "WindowsMxc.test_complete_policy_cannot_disable_mxc",
        "WindowsSandbox.test_modified_companion_is_rejected_before_execution",
        "WindowsSandbox.test_first_python_send_from_short_profile_directory",
        "WindowsSandbox.test_python_state_and_restart_with_private_temporary_storage",
        "WindowsSandbox.test_cwd_alias_does_not_require_parent_enumeration",
        "WindowsSandbox.test_stdio_write_policy_and_exit_status",
        "WindowsConsole.test_python_without_r",
        "WindowsConsole.test_r_without_python",
    ]
    if preinstalled_r:
        cases += [
            "WindowsConsole.test_sandboxed_preinstalled_r_python_bridge",
            "WindowsConsole.test_preinstalled_r_startup_python_bridge",
        ]
    else:
        cases += [
            "WindowsConsole.test_r_initializes_before_python",
            "WindowsConsole.test_python_initializes_before_r",
            "WindowsConsole.test_python_sleep_interrupt",
            "WindowsConsole.test_sql_with_r",
            "WindowsSandbox.test_network_enabled_sql",
        ]
    run(
        sys.executable,
        ROOT / "tests/windows.py",
        "-v",
        *cases,
        environment=os.environ
        | {
            "MCP_CONSOLE_TEST_BINARY": str(binary),
            "MCP_CONSOLE_TEST_NATIVE_BINARY": str(native),
        },
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    artifacts = parser.add_mutually_exclusive_group()
    artifacts.add_argument(
        "--installed-binary",
        type=artifact_path,
        help="accept an already installed release wheel without building from source",
    )
    artifacts.add_argument(
        "--archive", type=artifact_path, help="accept a relocated native release ZIP"
    )
    parser.add_argument(
        "--preinstalled-r",
        action="store_true",
        help="use preinstalled R packages when accepting an archive or installed wheel",
    )
    options = parser.parse_args()
    if (
        options.preinstalled_r
        and options.archive is None
        and options.installed_binary is None
    ):
        parser.error("--preinstalled-r requires --archive or --installed-binary")
    if sys.platform != "win32":
        raise SystemExit("native Windows installation checks require Windows")
    os.chdir(ROOT)
    if options.installed_binary is not None:
        binary = options.installed_binary
        native, _ = inspect_installation(binary)
        accept(binary, native, preinstalled_r=options.preinstalled_r)
        raise SystemExit(0)
    if options.archive is not None:
        with workspace() as directory:
            extracted = directory / "extracted"
            with zipfile.ZipFile(options.archive) as archive:
                archive.extractall(extracted)
            prefix = extracted.rename(directory / "relocated Console")
            binary = prefix / "bin/mcp-console.exe"
            inspect_artifacts(
                (path, path.read_bytes())
                for path in prefix.rglob("*")
                if path.suffix.lower() in NATIVE_SUFFIXES
            )
            accept(binary, binary, preinstalled_r=options.preinstalled_r)
        raise SystemExit(0)
    # Exercise installed commands from a workspace with ordinary inherited ACLs.
    with checkout_owner(ROOT), workspace() as directory:
        wheels = directory / "wheels"
        venv = directory / "venv"
        run("uv", "build", "--wheel", "--out-dir", wheels)
        (wheel,) = wheels.glob("*.whl")
        inspect_wheel(wheel)
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
        inspect_installation(venv / "Scripts/mcp-console.exe")
        environment = os.environ | {
            "MCP_CONSOLE_TEST_BINARY": str(venv / "Scripts/mcp-console.exe"),
            "MCP_CONSOLE_TEST_NATIVE_BINARY": str(venv / "libexec/mcp-console.exe"),
        }
        # Cargo fixture builds do not use the installed executable and already
        # run in native acceptance. Retain every installed runtime/package class.
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsPackaging",
            "WindowsConsole",
            "WindowsRelay",
            "WindowsResolver",
            "WindowsResolverMaterialization",
            "WindowsMxc",
            "WindowsPreparation",
            "WindowsSandbox",
            environment=environment,
        )
        run("uv", "pip", "install", "--reinstall", "--python", python, ROOT)
        inspect_installation(venv / "Scripts/mcp-console.exe")
        run(
            sys.executable,
            ROOT / "tests/windows.py",
            "-v",
            "WindowsConsole.test_python_initializes_before_r",
            "WindowsConsole.test_r_initializes_before_python",
            "WindowsConsole.test_python_sleep_interrupt",
            "WindowsConsole.test_sql_with_r",
            environment=environment,
        )
