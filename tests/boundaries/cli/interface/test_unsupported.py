#!/usr/bin/env -S uv run --script

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import NO_SANDBOX, requires
from support.suites import run_this_suite


@requires(NO_SANDBOX)
def test_reports_that_the_sandbox_is_unsupported(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        print("not run")
        """)
    arguments = ("sandbox", "--", "python", "-c", script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode != 0
    assert result.stdout == ""
    assert (
        result.stderr
        == "`mcp-console sandbox` is not supported on this operating system\n"
    )
    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
