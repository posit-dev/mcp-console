#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript, TranscriptEntry
from support.installation import native_console
from support.suites import run_this_suite


def record(binary: Path, *arguments: str) -> TranscriptEntry:
    result = subprocess.run(
        [binary, *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env={**os.environ, "NO_COLOR": "1", "COLUMNS": "80"},
    )
    entry: TranscriptEntry = {"command": " ".join(("mcp-console", *arguments))}
    if result.returncode != 0:
        entry["exit_code"] = result.returncode
    entry["stdout"] = result.stdout
    if result.stderr:
        entry["stderr"] = result.stderr
    return entry


def test_help(binary: Path) -> Transcript:
    entries = [
        record(binary),
        record(binary, "--help"),
        record(binary, "config", "--help"),
        record(binary, "config", "resolve", "--help"),
        record(binary, "configure-client", "--help"),
        record(binary, "serve", "--help"),
        record(binary, "worker", "--help"),
        record(binary, "sandbox"),
        record(binary, "sandbox", "--help"),
    ]
    for entry in entries:
        output = entry.get("stdout", "") + entry.get("stderr", "")
        assert all(len(line) <= 80 for line in output.splitlines()), entry
        if entry["command"] in ("mcp-console", "mcp-console --help"):
            positions = [
                output.index(f"\n  {command} ")
                for command in (
                    "serve",
                    "sandbox",
                    "config",
                    "configure-client",
                    "help",
                )
            ]
            assert positions == sorted(positions), entry
        # Executable suffixes are presentation, not a different CLI contract.
        # Assert wrapping above before normalizing the shared transcript.
        for stream in ("stdout", "stderr"):
            if stream in entry:
                entry[stream] = entry[stream].replace("mcp-console.exe", "mcp-console")
    return entries


def test_version(binary: Path) -> Transcript:
    return [record(binary, "--version")]


def test_executable_suffix_keeps_help_within_80_columns(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        executable = Path(temporary) / "mcp-console.exe"
        shutil.copy2(native_console(binary), executable)
        entry = record(executable, "configure-client", "--help")
        assert "exit_code" not in entry and "stderr" not in entry, entry
        output = entry["stdout"]
        assert all(len(line) <= 80 for line in output.splitlines()), entry
        assert "Usage: mcp-console configure-client " in output, entry
    return [
        {
            "executable": "mcp-console.exe",
            "help": "canonical command name within 80 columns",
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
