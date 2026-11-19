"""Accept a standalone Unix release archive after relocation."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from release import smoke_mcp


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    version = tomllib.loads((ROOT / "Cargo.toml").read_text())["package"]["version"]
    expected = re.escape(f"mcp-console {version}")
    if version.endswith("+dev"):
        expected += r"(?:\.g[0-9a-f]{12})?"

    with tempfile.TemporaryDirectory(prefix="mcp-console-archive-") as temporary:
        root = Path(temporary)
        unpacked = root / "unpacked"
        with tarfile.open(args.archive) as archive:
            archive.extractall(unpacked, filter="data")
        prefix = root / "moved installation"
        unpacked.rename(prefix)
        binary = prefix / "bin/mcp-console"
        empty_path = root / "empty"
        empty_path.mkdir()
        environment = os.environ | {
            "PATH": str(empty_path),
            "MCP_CONSOLE_HOME": str(root / "console-home"),
        }
        actual = subprocess.check_output(
            [binary, "--version"], cwd=root, env=environment, text=True
        ).strip()
        assert re.fullmatch(expected, actual), actual
        help_text = subprocess.check_output(
            [binary, "--help"], cwd=root, env=environment, text=True
        )
        assert "sandbox" in help_text
        subprocess.run(
            [binary, "sandbox", "--", "/usr/bin/true"],
            cwd=root,
            env=environment,
            check=True,
        )
        # Language preparation uses the host runtimes after native launch.
        smoke_mcp(
            binary,
            actual.removeprefix("mcp-console "),
            os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
            root,
            1200,
            30,
            r_available=bool(os.environ.get("R_HOME") or shutil.which("R")),
        )
        # The installation suite modifies each helper and notice in a disposable
        # copy to exercise the digests embedded in this archive's public binary.
        subprocess.run(
            [
                sys.executable,
                ROOT / "tests/sandbox_installation.py",
                binary,
                "SandboxInstallationTests.test_rejects_invalid_bundle_files",
            ],
            check=True,
        )


if __name__ == "__main__":
    main()
