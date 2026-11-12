"""Storage retention is configurable without creating or cleaning storage."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def resolve(binary: Path, root: Path, *arguments: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, "config", "resolve", "--no-config", "--format", "json", *arguments],
        cwd=root,
        env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_defaults_and_overrides_round_trip_without_storage(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        defaults = resolve(binary, root, "--full")
        assert defaults.returncode == 0 and defaults.stderr == "", defaults
        assert json.loads(defaults.stdout)["retention"] == {
            "cache_days": 14,
            "recordings_days": 14,
        }
        for selection in (
            {},
            {"cache_days": 3},
            {"recordings_days": None},
            {"cache_days": None, "recordings_days": 30},
        ):
            effective = {"cache_days": 14, "recordings_days": 14} | selection
            full = resolve(
                binary, root, "--full", "-c", f"retention={json.dumps(selection)}"
            )
            assert full.returncode == 0 and full.stderr == "", full
            assert json.loads(full.stdout)["retention"] == effective
            pruned = resolve(binary, root, "-c", f"retention={json.dumps(selection)}")
            assert pruned.returncode == 0 and pruned.stderr == "", pruned
            again = resolve(
                binary,
                root,
                "--full",
                "-c",
                f"retention={json.dumps(json.loads(pruned.stdout).get('retention', {}))}",
            )
            assert again.returncode == 0 and again.stderr == "", again
            assert json.loads(again.stdout) == json.loads(full.stdout)
            records.append(
                {"retention": effective, "pruned": json.loads(pruned.stdout)}
            )
        assert not list(root.iterdir()), list(root.iterdir())
    return records


def test_rejects_invalid_retention(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for selection in (
            "null",
            "[]",
            "{cache_days: 0}",
            "{recordings_days: -1}",
            "{cache_days: 1.5}",
            "{recordings_days: true}",
            "{unknown: 14}",
        ):
            result = resolve(binary, root, "-c", f"retention={selection}")
            assert result.returncode == 1 and result.stdout == "", result
            assert "retention" in result.stderr, result
            records.append({"selection": selection, "stderr": result.stderr})
        assert not list(root.iterdir()), list(root.iterdir())
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
