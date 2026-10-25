"""Conditional read-only permissions avoid mount targets for absent paths."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_conditional_paths_protect_existing_metadata_without_creating_missing_paths(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        workspace = root / "workspace"
        workspace.mkdir()
        metadata = workspace / "git-metadata"
        metadata.mkdir()
        (metadata / "HEAD").write_text("unchanged")
        (workspace / ".git").write_text("gitdir: git-metadata\n")
        (workspace / ".agents").mkdir()
        (workspace / ".agents/keep").write_text("unchanged")
        config = root / "policy.json"
        config.write_text(
            json.dumps(
                {
                    "sandbox": {
                        "profile": "read_only",
                        "filesystem": {
                            "read_write": ["."],
                            "read_only_if_exists": [
                                ".git",
                                ".agents",
                                ".codex",
                                ".aws",
                                ".claude",
                            ],
                        },
                    }
                }
            )
        )
        # fmt: python
        exercise = code(r"""
            import errno
            from pathlib import Path
            import sys

            for name in (".git", "git-metadata/HEAD", ".agents/keep"):
                path = Path(name)
                assert path.read_text()
                try:
                    path.write_text("changed")
                except OSError as error:
                    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                else:
                    raise AssertionError(name)
            absent = (".codex", ".aws", ".claude")
            assert all(not Path(name).exists() for name in absent)
            Path("ordinary").write_text("allowed")
            print("existing metadata protected; missing paths absent", flush=True)
            assert sys.stdin.readline() == "continue\n"
            # Skipped rules allow new metadata after policy capture.
            for name in absent:
                Path(name).mkdir()
                (Path(name) / "created").write_text("allowed")
            print("missing paths can be created")
            """)
        with subprocess.Popen(
            [
                binary,
                "sandbox",
                "--config-file",
                config,
                "--",
                sys.executable,
                "-c",
                exercise,
            ],
            cwd=workspace,
            env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        ) as process:
            try:
                ready = read_lines(process.stdout, 1, "sandbox permission checks")
                assert ready == ["existing metadata protected; missing paths absent"]
                assert sorted(path.name for path in workspace.iterdir()) == [
                    ".agents",
                    ".git",
                    "git-metadata",
                    "ordinary",
                ]
                output, error = process.communicate("continue\n", timeout=15)
                assert process.returncode == 0 and error == "", (output, error)
                assert output == "missing paths can be created\n"
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.wait(timeout=15)
        assert (metadata / "HEAD").read_text() == "unchanged"
        assert (workspace / ".agents/keep").read_text() == "unchanged"
        for name in (".codex", ".aws", ".claude"):
            assert (workspace / name / "created").read_text() == "allowed"
    return [
        {
            "stdout": "\n".join([*ready, output.strip()]) + "\n",
            "host_during_execution": "no missing metadata mount targets",
        }
    ]


if __name__ == "__main__":
    run_this_suite()
