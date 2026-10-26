"""Conditional permissions avoid mount targets for absent paths."""

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
        (workspace / "private").mkdir()
        (workspace / "private/keep").write_text("secret")
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
                            "deny_if_exists": [
                                "private",
                                "host-created",
                                "sandbox-created",
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
            secret = Path("private/keep")
            try:
                secret.read_text()
            except OSError as error:
                # Linux masks denied directories, hiding their children.
                assert error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT), error
            else:
                raise AssertionError("private content was readable")
            try:
                secret.write_text("changed")
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
            else:
                raise AssertionError("private content was writable")
            absent = (".codex", ".aws", ".claude")
            assert all(
                not Path(name).exists() for name in (*absent, "host-created", "sandbox-created")
            )
            Path("ordinary").write_text("allowed")
            print(
                "existing metadata read-only; private content denied; missing paths absent",
                flush=True,
            )
            assert sys.stdin.readline() == "continue\n"
            # Skipped rules stay omitted even when the host creates a path later.
            assert Path("host-created/keep").read_text() == "from host"
            Path("host-created/keep").write_text("from sandbox")
            for name in (*absent, "sandbox-created"):
                Path(name).mkdir()
                (Path(name) / "created").write_text("allowed")
            print("paths created after launch remain readable and writable")
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
                assert ready == [
                    "existing metadata read-only; private content denied; missing paths absent"
                ]
                assert sorted(path.name for path in workspace.iterdir()) == [
                    ".agents",
                    ".git",
                    "git-metadata",
                    "ordinary",
                    "private",
                ]
                (workspace / "host-created").mkdir()
                (workspace / "host-created/keep").write_text("from host")
                output, error = process.communicate("continue\n", timeout=15)
                assert process.returncode == 0 and error == "", (output, error)
                assert (
                    output
                    == "paths created after launch remain readable and writable\n"
                )
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.wait(timeout=15)
        assert (metadata / "HEAD").read_text() == "unchanged"
        assert (workspace / ".agents/keep").read_text() == "unchanged"
        assert (workspace / "private/keep").read_text() == "secret"
        assert (workspace / "host-created/keep").read_text() == "from sandbox"
        for name in (".codex", ".aws", ".claude", "sandbox-created"):
            assert (workspace / name / "created").read_text() == "allowed"
    return [
        {
            "stdout": "\n".join([*ready, output.strip()]) + "\n",
            "host_during_execution": "no missing conditional permission mount targets",
        }
    ]


if __name__ == "__main__":
    run_this_suite()
