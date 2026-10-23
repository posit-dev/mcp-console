#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, command, requires
from support.suites import run_this_suite


@requires(SANDBOX, command("uv"))
def test_installs_a_local_wheel_into_private_storage(binary: Path) -> Transcript:
    # Exercise a real uv install inside Seatbelt. Host resolver tests run outside
    # the sandbox and therefore cannot validate these compatibility permissions.
    # fmt: python
    script = code(r"""
        import os
        import subprocess
        import sys
        from pathlib import Path

        destination = Path(os.environ["TMPDIR"]) / "site"
        subprocess.run([
            sys.argv[1], "--offline", "--no-cache", "pip", "install", "--quiet",
            "--no-deps", "--target", str(destination), sys.argv[2],
        ], check=True)
        sys.path.insert(0, str(destination))
        import sandbox_fixture
        print(sandbox_fixture.value)
        """)
    with tempfile.TemporaryDirectory() as directory:
        wheel = Path(directory) / "sandbox_fixture-1.0-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as archive:
            archive.writestr(
                "sandbox_fixture.py", "value = 'installed in private storage'\n"
            )
            metadata = "sandbox_fixture-1.0.dist-info/"
            archive.writestr(
                metadata + "METADATA",
                """Metadata-Version: 2.1
Name: sandbox-fixture
Version: 1.0
""",
            )
            archive.writestr(
                metadata + "WHEEL",
                """Wheel-Version: 1.0
Generator: fixture
Root-Is-Purelib: true
Tag: py3-none-any
""",
            )
            archive.writestr(metadata + "RECORD", "")
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--",
                sys.executable,
                "-c",
                script,
                shutil.which("uv"),
                wheel,
            ],
            env=os.environ.copy(),
            capture_output=True,
            text=True,
            timeout=30,
        )
    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert result.stdout == "installed in private storage\n", result.stdout
    return [
        {
            "scenario": "uv installs a local wheel offline inside the sandbox",
            "python": script,
            "stdout": result.stdout,
            "exit_code": result.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
