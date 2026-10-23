"""A complete immutable Console installation owned by one case process."""

import shutil
import os
import tempfile
from functools import cache
from pathlib import Path


def native_console(binary: Path) -> Path:
    selected = os.environ.get("MCP_CONSOLE_TEST_NATIVE_BINARY")
    if selected is not None:
        return Path(selected)
    # Installed Windows commands are Python launchers. Fixtures that bind to
    # native PIDs or relocate/tamper with a bundle need its actual executable.
    native = binary.parent.parent / "libexec/mcp-console.exe"
    return native if os.name == "nt" and native.is_file() else binary


@cache
def _installed_console(binary: Path) -> tuple[tempfile.TemporaryDirectory, Path]:
    temporary = tempfile.TemporaryDirectory(prefix="mcp-console-test-installation-")
    prefix = Path(temporary.name)
    try:
        (prefix / "bin").mkdir()
        installed = (
            prefix / "bin" / ("mcp-console.exe" if os.name == "nt" else "mcp-console")
        )
        shutil.copy2(binary, installed)
        # Windows builds keep licenses inside the immutable libexec bundle;
        # installed wheels use the flat license directory on every platform.
        licenses = "share/licenses/mcp-console"
        directories = (
            ("libexec",)
            if os.name == "nt" and not (binary.parent.parent / licenses).is_dir()
            else ("libexec", licenses)
        )
        for relative in directories:
            shutil.copytree(binary.parent.parent / relative, prefix / relative)
    except BaseException:
        temporary.cleanup()
        raise
    return temporary, installed


def installed_console(binary: Path) -> Path:
    # Retain the directory across sequential execution modes, without sharing
    # workspaces, homes, extension caches or activated Python environments.
    return _installed_console(binary)[1]
