#!/usr/bin/env -S uv run --script

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.suites import run_this_suite


def test_removed_provider_commands_are_unavailable(binary: Path) -> Transcript:
    commands = (
        "ssh-launch",
        "ssh-prepare",
        "docker-owner",
        "docker-launch",
        "docker-probe",
        "docker-sandbox-owner",
        "docker-sandbox-launch",
        "docker-sandbox-probe",
        "image-runtime-probe",
    )
    for command in commands:
        result = subprocess.run(
            [binary, command], input="", capture_output=True, text=True, timeout=10
        )
        assert result.returncode == 2 and result.stdout == "", result
        assert f"unrecognized subcommand '{command}'" in result.stderr, result.stderr
    return [{"unavailable_commands": list(commands)}]


if __name__ == "__main__":
    run_this_suite(__file__)
