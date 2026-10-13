#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript, TranscriptEntry
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
        record(binary, "serve", "--help"),
        record(binary, "worker", "--help"),
        record(binary, "sandbox"),
        record(binary, "sandbox", "--help"),
    ]
    for entry in entries:
        output = entry.get("stdout", "") + entry.get("stderr", "")
        assert all(len(line) <= 80 for line in output.splitlines()), entry
    return entries


def test_version(binary: Path) -> Transcript:
    return [record(binary, "--version")]


if __name__ == "__main__":
    run_this_suite(__file__)
