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


def test_r_enablement_is_materialized_independently(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for overrides, enabled in (
            ((), True),
            (("r.enabled=false",), False),
            (("r.enabled=false", "r.enabled=true"), True),
            (("r.enabled=false", "r=null"), True),
        ):
            for full in (False, True):
                result = resolve(
                    binary,
                    root,
                    "--no-config",
                    "--format",
                    "json",
                    *(("--full",) if full else ()),
                    *(item for override in overrides for item in ("-c", override)),
                )
                assert result.returncode == 0 and result.stderr == "", result
                settings = json.loads(result.stdout)
                if full:
                    assert settings["r"] == {
                        "enabled": enabled,
                        "vanilla": False,
                        "resolution": "implicit",
                    }, settings
                    assert settings["languages"] == ["r", "python", "sql"], settings
                else:
                    assert settings == ({} if enabled else {"r": {"enabled": False}}), (
                        settings
                    )
                transcript.append(
                    {
                        "overrides": list(overrides),
                        "full": full,
                        "r": settings.get("r"),
                        "languages": settings.get("languages"),
                    }
                )
    return transcript


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
                "enabled": True,
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
            "r.enabled=1",
            "r.enabled=null",
            'r.enabled="false"',
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


def test_rejects_disabled_r_startup_configuration(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting, diagnostic in (
            (
                "r.executable=./unused-R",
                "disabled R cannot select an executable or startup packages",
            ),
            (
                'r.packages=["stats"]',
                "disabled R cannot select an executable or startup packages",
            ),
            (
                'startup={language: r, code: "1 + 1"}',
                "R startup source requires r.enabled=true",
            ),
        ):
            result = resolve(
                binary,
                root,
                "--no-config",
                "-c",
                "r.enabled=false",
                "-c",
                setting,
            )
            assert result.returncode == 1 and diagnostic in result.stderr, result
            assert not result.stdout, result
            transcript.append({"setting": setting, "stderr": result.stderr})
    return transcript


def test_rejects_explicit_r_settings_with_custom_workers(binary: Path) -> Transcript:
    transcript = []
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        for setting in ("r={}", "r.enabled=false", "r.vanilla=false", "r.vanilla=true"):
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
