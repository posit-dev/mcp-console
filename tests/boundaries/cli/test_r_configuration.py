"""Reject invalid and ineffective built-in R settings before runtime launch."""

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript


def test_rejects_invalid_r_settings(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment = {**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")}
        for setting in (
            "r.vanilla=1",
            "r.vanilla=null",
            'r.vanilla="false"',
            "r=[]",
            "r.executable=null",
            "r.executable=1",
            'r.executable=""',
            "r.profiles=true",
        ):
            result = subprocess.run(
                [binary, "serve", "-c", setting],
                cwd=root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1 and "configuration" in result.stderr, result
            assert not result.stdout, result
            transcript.append({"setting": setting, "stderr": result.stderr})
        return transcript


def test_rejects_explicit_r_settings_with_custom_workers(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting in ("r={}", "r.vanilla=false", "r.vanilla=true"):
            result = subprocess.run(
                [binary, "serve", "--worker", "unused-worker", "-c", setting],
                cwd=root,
                env={**os.environ, "MCP_CONSOLE_HOME": str(root / "console-home")},
                capture_output=True,
                text=True,
                timeout=10,
            )
            assert result.returncode == 1, result
            assert (
                result.stderr == "r settings require the built-in worker and relay\n"
            ), result
            transcript.append({"setting": setting, "stderr": result.stderr})
        return transcript
