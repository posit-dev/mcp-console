"""Resolution modes use the existing configuration and CLI validation."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from support.records import Transcript


def test_rejects_removed_automatic_policy(binary: Path) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting in (
            "r.resolution=automatic",
            "python.managed.resolution=automatic",
            "python.first_available=[{existing: unused}, {managed: {resolution: automatic}}]",
        ):
            result = subprocess.run(
                [binary, "config", "resolve", "-c", setting],
                cwd=root,
                env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert (
                result.returncode == 1
                and "unknown resolution policy `automatic`" in result.stderr
                and "expected implicit, explicit, startup_only, or disabled"
                in result.stderr
                and not result.stdout
            ), result
            records.append({"setting": setting, "stderr": result.stderr})
    return records


def test_rejects_invalid_policies(binary: Path) -> Transcript:
    records = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting, diagnostic in (
            ("r.resolution=true", "r: resolution"),
            ("r.resolution=null", "r: resolution"),
            ("r.resolution=unknown", "r: resolution"),
            ("r.resolution={implicit: null}", "r: resolution"),
            ("r.resolution=[]", "r: resolution"),
            ("python.managed.resolution=false", "managed.resolution"),
            ("python.managed.resolution=null", "managed.resolution"),
            ("python.managed.resolution=unknown", "managed.resolution"),
            (
                "python.managed.resolution=disabled",
                "use an existing Python environment or startup_only",
            ),
            ("python.resolution=explicit", "python"),
            ("resolution=implicit", "resolution"),
            (
                "python.first_available=[{existing: unused}, {managed: {resolution: disabled}}]",
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
            assert (
                result.returncode == 1
                and diagnostic in result.stderr
                and not result.stdout
            ), result
            records.append({"setting": setting, "stderr": result.stderr})
        result = subprocess.run(
            [
                binary,
                "serve",
                "-c",
                "r.resolution=disabled",
                "-c",
                "r.packages=[praise]",
            ],
            cwd=root,
            env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert (
            result.returncode == 1
            and "startup_only" in result.stderr
            and not result.stdout
        ), result
        records.append({"nonempty_disabled_r": result.stderr})
    return records
