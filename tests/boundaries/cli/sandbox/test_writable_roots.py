#!/usr/bin/env -S uv run --script

import os
import json
import socket
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
def test_writable_roots_cover_complete_subtrees(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import sys
        from pathlib import Path

        root = Path("writable")
        names = (".git", ".agents", ".codex", ".aws")
        launch = int(sys.argv[1])
        if launch == 1:
            assert sorted(path.name for path in root.iterdir()) == ["read-only", "secret"]
        for name in names:
            for nested in ("ordinary", *names):
                directory = root / name / nested
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / "value"
                if launch == 2:
                    assert path.read_text() == "1"
                path.write_text(str(launch))
                assert path.read_text() == str(launch)
                disposable = directory / "disposable"
                disposable.write_text("remove")
                disposable.unlink()
        assert (root / "read-only").read_text() == "unchanged"
        for path in (root / "read-only", root / "secret", Path("outside")):
            try:
                path.write_text("escaped")
            except OSError as error:
                assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS)
            else:
                raise AssertionError(f"unexpected write: {path}")
        try:
            (root / "secret").read_text()
        except OSError as error:
            assert error.errno in (errno.EACCES, errno.EPERM)
        else:
            raise AssertionError("unexpected read of denied file")
        print("complete subtree writable; explicit exclusions retained")
        """)
    transcript = []
    for source in ("--writable-root", "filesystem.read_write"):
        with TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            writable = host / "writable"
            writable.mkdir()
            (writable / "read-only").write_text("unchanged")
            (writable / "secret").write_text("private")
            filesystem = {
                "read_only": [str(writable / "read-only")],
                "deny": [str(writable / "secret")],
            }
            options = []
            if source == "--writable-root":
                options = ["--writable-root", str(writable)]
            else:
                filesystem["read_write"] = [str(writable)]
            config = host / "policy.json"
            config.write_text(json.dumps({"sandbox": {"filesystem": filesystem}}))
            for launch in (1, 2):
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--config-file",
                        config,
                        *options,
                        "--",
                        sys.executable,
                        "-c",
                        script,
                        str(launch),
                    ],
                    cwd=host,
                    env=dict(os.environ, MCP_CONSOLE_HOME=str(host / "home")),
                    capture_output=True,
                    text=True,
                )
                assert result.returncode == 0, (result.stdout, result.stderr)
                assert result.stderr == ""
                assert (
                    result.stdout
                    == "complete subtree writable; explicit exclusions retained\n"
                )
                assert (writable / "read-only").read_text() == "unchanged"
                assert (writable / "secret").read_text() == "private"
                assert not (host / "outside").exists()
                transcript.append(
                    {"grant": source, "launch": launch, "stdout": result.stdout}
                )
    return transcript


@requires(SANDBOX)
def test_writable_roots_augment_default_permissions(binary: Path) -> Transcript:
    return _augment_default_permissions(binary)


@requires(SANDBOX)
def test_project_grants_combine_with_cli_roots(binary: Path) -> Transcript:
    return _augment_default_permissions(binary, configured=True)


def _augment_default_permissions(binary: Path, configured: bool = False) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import os
        from pathlib import Path
        import socket
        import subprocess
        import sys

        host = Path.cwd()
        temporary = Path(os.environ["TMPDIR"])
        assert not host.is_relative_to(temporary)
        assert not temporary.is_relative_to(host)
        (temporary / "private").write_text("still writable")
        print("private storage remains writable")


        def denied(path: Path) -> None:
            try:
                path.write_text("must not be written")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                print(f"write denied: {path.relative_to(host)}")
            else:
                raise AssertionError(f"unexpected write: {path}")


        for name in ("output café 雪", "cache"):
            root = host / name
            if sys.argv[1] == "denied":
                denied(root / "created")
                continue
            created = root / "created"
            created.write_text("created")
            assert created.read_text() == "created"
            existing = root / "existing"
            assert existing.read_text() == "original"
            existing.write_text("modified")
            assert existing.read_text() == "modified"
            created.unlink()
            assert not created.exists()
            subprocess.run(["touch", str(root / "child")], check=True)
            assert (root / "child").is_file()
            print(f"create, modify, delete, subprocess write allowed: {name}")

        denied(host / "parent-write")
        denied(host / "neighbor" / "created")
        denied(host / "output café 雪" / "escape" / "created")
        try:
            socket.create_connection(("127.0.0.1", int(sys.argv[2])), timeout=2)
        except OSError:
            print("network remains denied")
        else:
            raise AssertionError("network was allowed")
        """)
    transcript = []
    with TemporaryDirectory() as directory, socket.socket() as listener:
        host = Path(directory).resolve()
        roots = [host / "output café 雪", host / "cache"]
        for root in [*roots, host / "neighbor"]:
            root.mkdir()
            (root / "existing").write_text("original")
        (roots[0] / "escape").symlink_to(host / "neighbor", target_is_directory=True)
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        for allowed in (False, True):
            # Exercise one relative and one absolute argument, each kept whole.
            options = (
                ["--writable-root", roots[0].name, "--writable-root", str(roots[1])]
                if allowed
                else []
            )
            if configured and allowed:
                config = host / ".agents/console/config.yaml"
                config.parent.mkdir(parents=True)
                config.write_text(
                    code("""
                    sandbox:
                      filesystem:
                        read_write: [./output café 雪]
                    """),
                    encoding="utf-8",
                )
                options = ["--writable-root", str(roots[1])]
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    *options,
                    "--",
                    sys.executable,
                    "-c",
                    script,
                    "allowed" if allowed else "denied",
                    str(listener.getsockname()[1]),
                ],
                cwd=host,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 0, result.stderr
            assert result.stderr == ""
            for root in roots:
                assert root.is_dir(), "retirement removed a user directory"
                assert (root / "existing").read_text() == (
                    "modified" if allowed else "original"
                )
                assert (root / "child").exists() == allowed
            assert (host / "neighbor" / "existing").read_text() == "original"
            assert not (host / "neighbor" / "created").exists()
            assert not (host / "parent-write").exists()
            transcript.append(
                {
                    "writable_roots": [root.name for root in roots] if allowed else [],
                    "stdout": result.stdout,
                }
            )
    return transcript


@requires(SANDBOX)
def test_accepts_file_and_missing_writable_roots(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        from pathlib import Path

        assert not Path("future output").exists()
        file = Path("output café 雪.txt")
        assert file.read_text() == "original"
        _ = file.write_text("updated")
        assert file.read_text() == "updated"
        print("single file updated; missing root was not created")
        for path in (Path("neighbor"), Path("unlisted")):
            try:
                _ = path.write_text("must not be written")
            except OSError as error:
                assert error.errno in (errno.EPERM, errno.EACCES, errno.EROFS)
                print(f"write denied: {path}")
            else:
                raise AssertionError(f"unexpected write: {path}")
        """)
    arguments = [
        "sandbox",
        "--writable-root",
        "output café 雪.txt",
        "--writable-root",
        "future output",
    ]
    with TemporaryDirectory() as directory:
        host = Path(directory)
        for name in ("output café 雪.txt", "neighbor"):
            (host / name).write_text("original")
        result = subprocess.run(
            [binary, *arguments, "--", sys.executable, "-c", script],
            cwd=host,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert result.stderr == ""
        assert (host / "output café 雪.txt").read_text() == "updated"
        assert (host / "neighbor").read_text() == "original"
        assert not (host / "future output").exists()
        assert not (host / "unlisted").exists()
    return [
        {
            "arguments": arguments,
            "python": script,
            "exit_code": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
    ]


def test_rejects_invalid_writable_roots_before_starting(binary: Path) -> Transcript:
    transcript = []
    for command in ("serve", "sandbox"):
        arguments = [command, "--writable-root", ""]
        if command == "sandbox":
            arguments += ["--", "/bin/echo", "workload started"]
        result = subprocess.run(
            [binary, *arguments],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert "a value is required for '--writable-root" in result.stderr
        transcript.append(
            {
                "arguments": arguments,
                "exit_code": result.returncode,
                "stderr": result.stderr,
            }
        )
    return transcript


@requires(SANDBOX)
def test_rejects_non_utf8_writable_roots_before_starting(binary: Path) -> Transcript:
    transcript = []
    for command in (b"serve", b"sandbox"):
        arguments = [command, b"--writable-root", b"non-utf8-\xff"]
        if command == b"sandbox":
            arguments += [b"--", b"/bin/echo", b"workload started"]
        result = subprocess.run(
            [os.fsencode(binary), *arguments],
            input="",
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1, result.stderr
        assert result.stdout == ""
        assert result.stderr == "writable root 'non-utf8-�' is not valid UTF-8\n", (
            result.stderr
        )
        transcript.append(
            {
                "arguments_bytes": repr(arguments),
                "exit_code": result.returncode,
                "stderr": result.stderr,
            }
        )
    return transcript


def test_rejects_conflicting_writable_root_options(binary: Path) -> Transcript:
    transcript = []
    for arguments in (
        ["serve", "--no-sandbox", "--writable-root", "."],
        [
            "sandbox",
            "--runner-config-env",
            "POLICY",
            "--writable-root",
            ".",
            "--",
            "/bin/echo",
            "workload started",
        ],
    ):
        result = subprocess.run(
            [binary, *arguments],
            input="",
            capture_output=True,
            text=True,
            env={**os.environ, "NO_COLOR": "1"},
        )
        assert result.returncode == 2, result.stderr
        assert result.stdout == ""
        assert "cannot be used with" in result.stderr
        assert "--writable-root" in result.stderr
        transcript.append(
            {
                "arguments": arguments,
                "exit_code": result.returncode,
                "stderr": result.stderr.replace(
                    "Usage: mcp-console.exe ", "Usage: mcp-console "
                ),
            }
        )
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
