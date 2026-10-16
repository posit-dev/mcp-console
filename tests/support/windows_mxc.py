"""Probe native MXC through the public launcher before selecting acceptance cases.

Only the runner's explicit unavailable-runtime diagnostic permits a skip.
Installation, policy, and workload failures remain test failures.
"""

from functools import cache, wraps
import os
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest

from support.requirements import Requirement


@cache
def mxc_requirement(binary: Path) -> Requirement:
    with TemporaryDirectory(prefix="console MXC probe ") as temporary:
        root = Path(temporary)
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--",
                sys.executable,
                "-I",
                "-c",
                "print('MXC probe executed')",
            ],
            cwd=root,
            env=os.environ | {"MCP_CONSOLE_HOME": str(root / "home")},
            capture_output=True,
            text=True,
            timeout=30,
        )
    if result.returncode == 0:
        assert result.stdout.strip() == "MXC probe executed" and not result.stderr, (
            result
        )
        return Requirement("native Windows MXC", True, "")
    unavailable = "MXC is unavailable for this filesystem policy on this executor;"
    assert (
        result.returncode == 1 and not result.stdout and unavailable in result.stderr
    ), result
    return Requirement("native Windows MXC", False, result.stderr.strip())


def requires_mxc(binary: Path):
    """Skip unavailable native execution; allow local runs to require coverage."""

    def decorate(test):
        @wraps(test)
        def run(*args, **kwargs):
            requirement = mxc_requirement(binary)
            if not requirement.available:
                if os.environ.get("MCP_CONSOLE_TEST_REQUIRE_MXC") == "1":
                    raise AssertionError(requirement.reason)
                raise unittest.SkipTest(requirement.reason)
            return test(*args, **kwargs)

        return run

    return decorate
