"""Home-relative permissions apply to the launching user's paths."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_home_paths_control_reads_and_writes(binary: Path) -> Transcript:
    # fmt: python
    program = code("""
        import errno
        from pathlib import Path
        import sys

        home = Path(sys.argv[1])
        assert (home / "data/input").read_text() == "readable"
        try:
            (home / "private/secret").read_text()
        except OSError as error:
            assert error.errno in (errno.EPERM, errno.EACCES)
            print("private read denied")
        else:
            raise AssertionError("home-relative denial did not protect the file")
        for directory in ("output", "cli-output"):
            (home / directory / "result").write_text("allowed")
            print(directory + " write allowed")
        """)
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        for directory in ("data", "private", "output", "cli-output"):
            (home / directory).mkdir(parents=True)
        (home / "data/input").write_text("readable", encoding="utf-8")
        (home / "private/secret").write_text("private", encoding="utf-8")
        selected = root / "config.yaml"
        selected.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "filesystem": {
                            "read_only": ["~/data"],
                            "read_write": ["~/output"],
                            "deny": ["~/private"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--config-file",
                str(selected),
                "--writable-root",
                "~/cli-output",
                "--",
                sys.executable,
                "-c",
                program,
                str(home),
            ],
            cwd=root,
            env=os.environ
            | {"HOME": str(home), "MCP_CONSOLE_HOME": str(root / "console")},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert (
            result.stdout
            == "private read denied\noutput write allowed\ncli-output write allowed\n"
        ), result
        for directory in ("output", "cli-output"):
            assert (home / directory / "result").read_text() == "allowed"
    return [{"stdout": result.stdout}]


if __name__ == "__main__":
    run_this_suite(__file__)
