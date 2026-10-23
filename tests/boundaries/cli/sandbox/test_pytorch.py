#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_matches_unsandboxed_autograd(binary: Path) -> Transcript:
    environment = os.environ.copy()
    for name in ("UV_EXCLUDE_NEWER", "UV_NO_CACHE", "UV_OFFLINE"):
        environment.pop(name, None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for the PyTorch workflow"
    resolved = subprocess.run(
        [
            uv,
            "tool",
            "run",
            "--isolated",
            "--upgrade",
            # The fixture uses CPU tensors; avoid Linux's CUDA dependencies.
            "--torch-backend",
            "cpu",
            "--with",
            "torch",
            "--",
            "python",
            "-c",
            "import sys; print(sys.executable)",
        ],
        capture_output=True,
        text=True,
        env=environment,
        timeout=300,
    )
    assert resolved.returncode == 0, (resolved.stdout, resolved.stderr)
    # Preserve the venv path: resolving its symlink would lose the environment.
    python = resolved.stdout.strip()
    script = Path(__file__).resolve().parents[3] / "fixtures" / "pytorch_autograd.py"
    results = []
    captures = []
    for prefix in ([], [binary, "sandbox", "--"]):
        completed = subprocess.run(
            [*prefix, python, script],
            capture_output=True,
            text=True,
            env=environment,
            timeout=60,
        )
        captures.append((completed.stdout, completed.stderr))
        assert completed.returncode == 0, captures
        # Rolling releases may change warnings and other incidental output.
        # Compare only the fixture-owned result; keep captures for diagnostics.
        records = [
            line.removeprefix("PYTORCH_RESULT=")
            for line in completed.stdout.splitlines()
            if line.startswith("PYTORCH_RESULT=")
        ]
        assert len(records) == 1, captures
        results.append(json.loads(records[0]))
    assert results[1] == results[0], captures
    return [
        {
            "command": [
                "mcp-console",
                "sandbox",
                "--",
                "<resolved Python>",
                "tests/fixtures/pytorch_autograd.py",
            ],
            "result": "<numbers identical to the live unsandboxed run>",
            "exit_code": 0,
            "transcript_normalization": {"target": "result", "warnings": "omitted"},
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
