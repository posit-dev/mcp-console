#!/usr/bin/env -S uv run --script

import json
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.linux_sandbox import procfs_probe_command
from support.records import Transcript
from support.requirements import FRESH_PROCFS, NESTED_PROCFS, PROCFS_NETWORK, requires
from support.suites import run_this_suite


def probe(binary: Path, procfs: str) -> Transcript:
    fixture = Path(__file__).resolve().parents[3] / "fixtures/cli/sandbox/procfs.py"
    command = [sys.executable, str(fixture), "run", str(binary), "console", procfs]
    command = procfs_probe_command(command, inherited=procfs == "inherited")
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result
    assert result.stderr == "", result
    return [json.loads(line) for line in result.stdout.splitlines()]


@requires(FRESH_PROCFS, PROCFS_NETWORK)
def test_fresh_procfs_preserves_policy_and_supervisor_isolation(
    binary: Path,
) -> Transcript:
    return probe(binary, "fresh")


@requires(NESTED_PROCFS, PROCFS_NETWORK)
def test_inherited_procfs_preserves_policy_and_supervisor_isolation(
    binary: Path,
) -> Transcript:
    transcript = probe(binary, "inherited")
    for record in transcript:
        assert record["operations"]["network_namespace_link"] == "errno:13", record
        assert record["operations"]["network_setns"] == "errno:1", record
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
