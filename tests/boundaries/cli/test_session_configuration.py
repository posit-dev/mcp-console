"""Configure session capacity without starting runtimes."""

import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boundaries.cli.test_config_materialization import resolve
from support.records import Transcript
from support.suites import run_this_suite


def test_materializes_and_overrides_session_limit(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name, arguments, maximum in (
            ("default", (), None),
            ("limited", ("-c", "sessions.max=3"), 3),
            (
                "cleared",
                ("--default-config", "sessions.max=3", "-c", "sessions.max=null"),
                None,
            ),
        ):
            full = resolve(
                binary, root, "--no-config", "--full", "--format", "json", *arguments
            )
            assert full.returncode == 0 and full.stderr == "", full
            value = json.loads(full.stdout)
            assert value["sessions"] == {"max": maximum}, value
            pruned = resolve(
                binary, root, "--no-config", "--prune", "--format", "json", *arguments
            )
            assert pruned.returncode == 0 and pruned.stderr == "", pruned
            expected = {} if maximum is None else {"sessions": {"max": maximum}}
            assert json.loads(pruned.stdout) == expected, pruned
            selected = root / "selected.json"
            selected.write_text(full.stdout, encoding="utf-8")
            again = resolve(
                binary,
                root,
                "--config-file",
                str(selected),
                "--full",
                "--format",
                "json",
            )
            assert again.returncode == 0 and again.stderr == "", again
            assert json.loads(again.stdout) == value, again
            records.append(
                {"case": name, "sessions": value["sessions"], "pruned": expected}
            )
    return records


def test_rejects_invalid_session_limits(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for assignment in (
            "sessions.max=0",
            "sessions.max=-1",
            "sessions.max=1.5",
            "sessions.max=true",
            'sessions.max="2"',
            "sessions.max=[]",
            "sessions=null",
            "sessions=[]",
            "sessions.unknown=1",
        ):
            result = resolve(binary, root, "--no-config", "-c", assignment)
            assert result.returncode == 1 and result.stdout == "", result
            assert "sessions" in result.stderr, result
            records.append({"assignment": assignment, "error": result.stderr})
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
