"""Resolve and validate built-in R settings before runtime launch."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from boundaries.cli.test_config_materialization import resolve


def test_r_selection_shorthand_preserves_layered_settings(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        console_home = root / "console-home"
        console_home.mkdir()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for home_selection, selection, overrides, vanilla in (
            (None, "chosen R", ("r.vanilla=true",), True),
            (None, {"vanilla": True}, ("r=./chosen R",), True),
            (None, {"vanilla": True}, ("r=null", "r=./chosen R"), False),
            ("./chosen R", {"vanilla": True}, (), True),
        ):
            (console_home / "config.yaml").write_text(
                json.dumps({} if home_selection is None else {"r": home_selection})
            )
            config.write_text(json.dumps({"r": selection}))
            result = resolve(
                binary,
                root,
                "--full",
                "--format",
                "json",
                *(item for override in overrides for item in ("-c", override)),
            )
            assert result.returncode == 0 and result.stderr == "", result
            settings = json.loads(result.stdout)
            assert settings["r"] == {
                "executable": str(root / "chosen R"),
                "vanilla": vanilla,
                "resolution": "implicit",
            }, settings
            transcript.append(
                {
                    "home_selection": home_selection,
                    "project_selection": selection,
                    "overrides": list(overrides),
                    "selected_workspace_launcher": True,
                    "vanilla": vanilla,
                }
            )
    return transcript


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
