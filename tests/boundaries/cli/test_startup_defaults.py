"""Startup package and version validation through CLI configuration."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript


def test_rejects_invalid_startup_defaults(binary: Path) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting, path in (
            ("r.packages=null", "r"),
            ("r.packages=[1]", "r"),
            ('r.packages=[""]', "r.packages"),
            ("python.managed.packages=null", "managed.packages"),
            ("python.managed.packages=[1]", "managed.packages"),
            ('python.managed.packages=[""]', "python.managed.packages"),
            (
                'python.managed.packages=["numpy @ https://example.com/pkg.whl"]',
                "python.managed.packages",
            ),
            ("python.managed.version=3.13", "managed.version"),
            ("python.managed.version=[]", "managed.version"),
            ("python.managed.version=null", "managed.version"),
            ('python.managed.version=""', "python.managed.version"),
            ('python.managed.version="/opt/python"', "python.managed.version"),
            ("python.managed.extra_packages=[]", "managed.extra_packages"),
            ("python.packages=null", "managed.packages"),
            ("python.packages=[1]", "managed.packages"),
            ('python.packages=[""]', "python.managed.packages"),
            ("python.version=3.13", "managed.version"),
            ("python.version=null", "managed.version"),
            ('python.version="/opt/python"', "python.managed.version"),
            ("python={packages: [], extra_packages: []}", "managed.extra_packages"),
            ("python.existing={executable: .venv, packages: []}", "existing"),
            (
                "python.first_available=[{existing: unused}, {managed: {version: 3.13}}]",
                "first_available[1]",
            ),
        ):
            result = subprocess.run(
                [binary, "serve", "-c", setting],
                cwd=root,
                env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and path in result.stderr, result
            assert not result.stdout, result
            records.append({"setting": setting, "stderr": result.stderr})
    return records
