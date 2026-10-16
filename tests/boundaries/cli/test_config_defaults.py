"""Supply client defaults below configuration files through the public CLI."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def test_default_config_layers_below_files_and_overrides(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        project = root / ".agents/console"
        project.mkdir(parents=True)
        selected = root / "selected.json"
        selected.write_text(
            json.dumps({"sandbox": {"profile": None}, "r": {"packages": []}}),
            encoding="utf-8",
        )
        defaults = [
            "--default-config",
            "sandbox.profile=inherit_codex",
            "--default-config",
            "environment={KEEP: default, CHANGE: first}",
            "--default-config",
            "r.packages=[stats]",
        ]
        for name, global_value, project_value, arguments, profile, change, packages in (
            ("defaults", {}, {}, (), "inherit_codex", "last", ["stats"]),
            (
                "global",
                {
                    "sandbox": {"profile": "workspace"},
                    "environment": {"CHANGE": "global"},
                },
                {},
                (),
                "workspace",
                "global",
                ["stats"],
            ),
            (
                "project",
                {"sandbox": {"profile": "workspace"}},
                {
                    "sandbox": {"profile": "read_only"},
                    "environment": {"CHANGE": "project"},
                    "r": {"packages": []},
                },
                (),
                "read_only",
                "project",
                [],
            ),
            (
                "override",
                {},
                {"sandbox": {"profile": "read_only"}},
                ("-c", "sandbox.profile=workspace"),
                "workspace",
                "last",
                ["stats"],
            ),
            (
                "no config",
                {"sandbox": {"profile": "workspace"}},
                {"sandbox": {"profile": "workspace"}},
                ("--no-config",),
                "inherit_codex",
                "last",
                ["stats"],
            ),
            (
                "selected file",
                {"sandbox": {"profile": "workspace"}},
                {"sandbox": {"profile": "workspace"}},
                ("--config-file", str(selected)),
                None,
                "last",
                [],
            ),
        ):
            (home / "config.yaml").write_text(
                json.dumps(global_value), encoding="utf-8"
            )
            (project / "config.yaml").write_text(
                json.dumps(project_value), encoding="utf-8"
            )
            result = subprocess.run(
                [
                    binary,
                    *defaults,
                    "config",
                    "resolve",
                    "--full",
                    "--format",
                    "json",
                    *arguments,
                    "--default-config",
                    "environment.CHANGE=last",
                    "--default-config",
                    "sandbox.profile=inherit_codex",
                ],
                cwd=root,
                env=os.environ | {"MCP_CONSOLE_HOME": str(home)},
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 0 and not result.stderr, result
            value = json.loads(result.stdout)
            assert value["sandbox"].get("profile") == profile, value["sandbox"]
            assert value["environment"] == {"KEEP": "default", "CHANGE": change}, value
            assert value["r"]["packages"] == packages, value["r"]
            records.append(
                {
                    "source": name,
                    "profile": profile,
                    "environment": value["environment"],
                    "packages": packages,
                }
            )
    return records


def test_invalid_defaults_fail_before_launch(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for assignment, diagnostic in (
            ("environment", "KEY=VALUE"),
            (".environment=value", "dotted key"),
            ("unknown_setting=true", "unknown_setting"),
        ):
            result = subprocess.run(
                [
                    binary,
                    "config",
                    "resolve",
                    "--no-config",
                    "--default-config",
                    assignment,
                ],
                cwd=root,
                env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert result.returncode == 1 and not result.stdout, result
            assert diagnostic in result.stderr, result
            records.append({"default": assignment, "rejected": True})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
