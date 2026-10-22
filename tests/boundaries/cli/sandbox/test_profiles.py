#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, SANDBOX, requires
from support.suites import run_this_suite

CONFIG = ".agents/console/config.yaml"
PROTECTED = (".git", ".agents", ".codex", ".aws", ".claude")


def configure(host: Path, source: str) -> None:
    config = host / CONFIG
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(source, encoding="utf-8")


def run(
    binary: Path,
    host: Path,
    exercise: str,
    *options: str,
    environment: dict[str, str] | None = None,
) -> dict[str, str]:
    result = subprocess.run(
        [binary, "sandbox", *options, "--", sys.executable, "-c", exercise],
        cwd=host,
        env=os.environ | (environment or {}),
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result
    assert result.stderr == "", result
    return {"stdout": result.stdout, "stderr": result.stderr}


@requires(SANDBOX)
def test_workspace_yaml_forms_protect_metadata_and_private_temporary_storage(
    binary: Path,
) -> Transcript:
    # fmt: python
    exercise = code(r'''
        import errno
        import os
        from pathlib import Path
        import subprocess
        import textwrap


        def denied(operation, *args, **kwargs):
            try:
                operation(*args, **kwargs)
            except OSError as error:
                # Native Linux mounts can deny replacement across a mount boundary.
                assert error.errno in (
                    errno.EACCES,
                    errno.EPERM,
                    errno.EROFS,
                    errno.EBUSY,
                    errno.EXDEV,
                ), error
            else:
                raise AssertionError(f"unexpected success: {operation} {args}")


        ordinary = Path("ordinary")
        _ = ordinary.write_text("created")
        _ = ordinary.write_text("edited")
        assert ordinary.read_text() == "edited"
        ordinary.unlink()
        for name in (".git", ".agents", ".codex", ".claude"):
            protected = Path(name)
            keep = protected / "keep"
            assert keep.read_text() == "readable"
            denied(keep.write_text, "changed")
            denied((protected / "new").write_text, "created")
            denied(keep.unlink)
            denied(protected.rename, Path(name + "-moved"))
            replacement = Path("replacement")
            _ = replacement.write_text("replacement")
            denied(os.replace, replacement, keep)
            replacement.unlink()
            empty = protected / "empty"
            denied(empty.rmdir)
            replacement.mkdir()
            denied(os.replace, replacement, empty)
            replacement.rmdir()
            for action in ("write", "delete", "rename"):
                child = subprocess.run(
                    [
                        os.environ["TEST_PYTHON"],
                        "-c",
                        # fmt: python
                        textwrap.dedent(
                            """
                            from pathlib import Path
                            import sys

                            p = Path(sys.argv[1])
                            if sys.argv[2] == "write": p.write_text("child")
                            if sys.argv[2] == "delete": p.unlink()
                            if sys.argv[2] == "rename": p.parent.rename(p.parent.with_name("moved"))
                            """
                        ),
                        str(keep),
                        action,
                    ],
                    capture_output=True,
                    text=True,
                )
                assert child.returncode != 0, (name, action, child)
                assert "PermissionError" in child.stderr or "OSError" in child.stderr, child
            assert keep.read_text() == "readable"
        for root in (os.environ["INHERITED_TEMP"], os.environ["SHARED_TEMP"]):
            denied((Path(root) / "outside").write_text, "blocked")
        private = Path(os.environ["TMPDIR"])
        _ = (private / "private").write_text("writable")
        print(
            "project edits; readable metadata; denied writes, creation, deletion, replacement and child mutations; private-only temp"
        )
        ''')
    transcript = []
    for quoted in (True, False):
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory() as inherited,
            TemporaryDirectory(dir="/tmp") as shared,
        ):
            host = Path(directory).resolve() / "workspace"
            host.mkdir()
            for name in PROTECTED:
                (host / name / "empty").mkdir(parents=True)
                (host / name / "keep").write_text("readable")
            source = "sandbox: {filesystem: {read_write: [.], read_only: [.git, .agents, .codex, .aws, .claude]}}\n"
            # Explicit fixed-workspace guards survive broader writable roots.
            options = ("--writable-root", "..") if quoted else ()
            if not quoted:
                source = "sandbox: {filesystem: {read_write: [..], read_only: [.git, .agents, .codex, .aws, .claude]}}\n"
            configure(host, source)
            result = run(
                binary,
                host,
                exercise,
                *options,
                environment={
                    "TEST_PYTHON": sys.executable,
                    "TMPDIR": inherited,
                    "INHERITED_TEMP": inherited,
                    "SHARED_TEMP": shared,
                },
            )
            transcript.append(
                {"configuration": source, "cli_writable_parent": quoted, **result}
            )
    return transcript


@requires(SANDBOX)
def test_absent_metadata_stays_absent_and_git_worktree_indirection_is_native(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(
            host,
            "sandbox: {filesystem: {read_write: [.], read_only: [.git, .codex, .claude]}}\n",
        )
        absent = run(
            binary,
            host,
            # fmt: python
            code(r"""
                import errno
                from pathlib import Path

                for name in (".git", ".codex", ".claude"):
                    try:
                        (Path(name) / "new").mkdir(parents=True)
                    except OSError as error:
                        # A native Linux placeholder can represent an absent path as a file.
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS, errno.ENOTDIR), (
                            error
                        )
                    else:
                        raise AssertionError(name)
                print("absent metadata cannot be created")
                """),
        )
        assert all(not (host / name).exists() for name in (".git", ".codex", ".claude"))
        metadata = host / "worktree-metadata"
        metadata.mkdir()
        (metadata / "HEAD").write_text("ref: refs/heads/main\n")
        (host / ".git").write_text("gitdir: worktree-metadata\n")
        (host / "alias").symlink_to(metadata, target_is_directory=True)
        indirect = run(
            binary,
            host,
            # fmt: python
            code(r"""
                import errno
                from pathlib import Path

                for path in (Path(".git"), Path("worktree-metadata/HEAD"), Path("alias/HEAD")):
                    assert path.read_text()
                    try:
                        _ = path.write_text("changed")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        raise AssertionError(path)
                print(
                    "git pointer and referenced metadata remain readable and protected through aliases"
                )
                """),
        )
        return [{"absent": absent, "worktree": indirect}]


@requires(SANDBOX)
def test_profile_adjustments_keep_native_write_exceptions_and_read_denials(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        for name in PROTECTED:
            (host / name / "allowed").mkdir(parents=True)
            (host / name / "keep").write_text("readable")
        source = "sandbox: {filesystem: {read_write: [., .claude, .agents/allowed], deny: [.git/keep]}}"
        configure(host, source)
        result = run(
            binary,
            host,
            # fmt: python
            code(r"""
                import errno
                from pathlib import Path

                for name in (".claude/new", ".agents/allowed/new", ".codex/new"):
                    _ = Path(name).write_text("deliberate exception")
                try:
                    _ = Path(".git/keep").read_text()
                except OSError as error:
                    assert error.errno in (errno.EACCES, errno.EPERM), error
                else:
                    raise AssertionError("read denial ignored")
                assert Path(".agents/keep").read_text() == "readable"
                print(
                    "equal-path and descendant write exceptions, group translation and read denial verified"
                )
                """),
            "--writable-root",
            ".codex",
        )
        return [{"configuration": source, **result}]


@requires(SANDBOX)
def test_read_only_and_explicit_filesystem_kinds_preserve_their_meaning(
    binary: Path,
) -> Transcript:
    transcript = []
    for selector in ('":read-only"', ":read-only"):
        with TemporaryDirectory() as directory:
            host = Path(directory).resolve()
            configure(host, "sandbox: {}\n")
            transcript.append(
                run(
                    binary,
                    host,
                    # fmt: python
                    code(r"""
                        import errno
                        import os
                        from pathlib import Path

                        try:
                            _ = Path("blocked").write_text("no")
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                        else:
                            raise AssertionError("read-only allowed project write")
                        _ = (Path(os.environ["TMPDIR"]) / "allowed").write_text("private")
                        print("read-only workspace and writable private temp")
                        """),
                )
            )
    return transcript


@requires(SANDBOX)
def test_unsupported_profile_names_use_native_diagnostics(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        for name in (":unknown", "workspace", "workspace_write", "workspace-write"):
            configure(host, f"extends: {json.dumps(name)}\n")
            result = subprocess.run(
                [binary, "sandbox", "--", "/bin/echo", "must not run"],
                cwd=host,
                capture_output=True,
                text=True,
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert "unknown field `extends`" in result.stderr, result
            transcript.append({"selector": name, "stderr": result.stderr})
    return transcript


@requires(SANDBOX)
def test_denied_workspace_keeps_metadata_unreadable(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        host = root / "workspace"
        host.mkdir()
        cache = root / "cache"
        cache.mkdir()
        for name in PROTECTED:
            (host / name).mkdir()
            (host / name / "keep").write_text("protected metadata")
            assert (host / name / "keep").read_text() == "protected metadata"
        configure(
            host,
            json.dumps(
                {
                    "sandbox": {
                        "filesystem": {
                            "read_write": [str(cache)],
                            "deny": ["."],
                        }
                    }
                }
            ),
        )
        # Verify the host fixtures above before observing denied workload reads.
        # Linux masks denied directories, so their children report ENOENT.
        exercise = (
            # fmt: python
            code(f"""
                import errno
                from pathlib import Path

                for name in {PROTECTED!r}:
                    try:
                        Path({str(host)!r}, name, "keep").read_text()
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.ENOENT), error
                        print(name + ": read denied")
                    else:
                        raise AssertionError(name + " was readable")
                """)
        )
        result = subprocess.run(
            [binary, "sandbox", "--", sys.executable, "-I", "-c", exercise],
            cwd=host,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert result.stdout == "".join(
            name + ": read denied\n" for name in PROTECTED
        ), result
        return [{"stdout": result.stdout, "stderr": result.stderr}]


@requires(SANDBOX)
def test_empty_protected_directories_cannot_be_removed_or_replaced(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(
            host,
            "sandbox: {filesystem: {read_write: [.], read_only: [.git, .codex, .claude]}}\n",
        )
        for name in (".git", ".codex", ".claude"):
            (host / name).mkdir()
        return [
            run(
                binary,
                host,
                # fmt: python
                code(r"""
                    import errno
                    import os
                    from pathlib import Path

                    for name in (".git", ".codex", ".claude"):
                        protected = Path(name)
                        replacement = Path("replacement")
                        replacement.mkdir()
                        for operation, args in (
                            (os.rmdir, (protected,)),
                            (os.replace, (replacement, protected)),
                            (os.rename, (protected, Path("moved"))),
                        ):
                            try:
                                operation(*args)
                            except OSError as error:
                                assert error.errno in (
                                    errno.EACCES,
                                    errno.EPERM,
                                    errno.EROFS,
                                    errno.EBUSY,
                                    errno.EXDEV,
                                ), error
                            else:
                                raise AssertionError((operation, name))
                        replacement.rmdir()
                        assert protected.is_dir()
                    print("empty protected directories resist removal, replacement and rename")
                    """),
            )
        ]


@requires(POSIX, SANDBOX)
def test_tempdir_modes_are_independent_of_workspace_profile(binary: Path) -> Transcript:
    transcript = []
    for profile in (None, "workspace"):
        for mode in ("managed", "read_write", "read_only"):
            with (
                # Keep the workspace outside the automatic /tmp write grant.
                TemporaryDirectory(dir="/var/tmp") as directory,
                TemporaryDirectory(dir="/var/tmp") as inherited,
                TemporaryDirectory(dir="/var/tmp") as configured,
                TemporaryDirectory(dir="/tmp") as shared,
            ):
                host = Path(directory).resolve()
                (host / ".git").mkdir()
                (host / ".git/keep").write_text("metadata")
                policy = {"tempdir": mode}
                if profile:
                    policy["profile"] = profile
                configure(
                    host,
                    json.dumps(
                        {
                            "sandbox": policy,
                            "environment": {
                                name: configured for name in ("TMPDIR", "TEMP", "TMP")
                            },
                        }
                    ),
                )
                result = run(
                    binary,
                    host,
                    # fmt: python
                    code(r"""
                        import errno
                        import os
                        from pathlib import Path

                        mode = os.environ["EXPECTED_MODE"]
                        temporary = Path(os.environ["TMPDIR"])
                        configured = Path(os.environ["CONFIGURED_TEMP"])
                        assert (temporary == configured) is (mode != "managed")
                        for path, allowed in (
                            (temporary, mode != "read_only"),
                            (configured, mode == "read_write"),
                            (Path(os.environ["INHERITED_TEMP"]), False),
                            (Path(os.environ["SHARED_TEMP"]), mode == "read_write"),
                            (Path.cwd(), os.environ["WORKSPACE_WRITE"] == "1"),
                            (Path(".git"), False),
                        ):
                            try:
                                _ = (path / "probe").write_text("temporary write")
                            except OSError as error:
                                assert not allowed and error.errno in (
                                    errno.EACCES,
                                    errno.EPERM,
                                    errno.EROFS,
                                ), (path, allowed, error)
                            else:
                                assert allowed, path
                        assert Path(".git/keep").read_text() == "metadata"
                        print("temp selection, host grants and workspace policy match configuration")
                        """),
                    environment={
                        "TMPDIR": inherited,
                        "INHERITED_TEMP": inherited,
                        "CONFIGURED_TEMP": configured,
                        "SHARED_TEMP": shared,
                        "EXPECTED_MODE": mode,
                        "WORKSPACE_WRITE": "1" if profile else "0",
                    },
                )
                assert not list(Path(configured).glob("sandbox-*"))
                transcript.append({"profile": profile, "tempdir": mode, **result})
    return transcript


@requires(SANDBOX)
def test_read_only_temp_retains_explicit_write_grants(binary: Path) -> Transcript:
    with TemporaryDirectory() as directory, TemporaryDirectory() as temporary:
        host = Path(directory).resolve()
        configure(
            host,
            json.dumps(
                {
                    "sandbox": {
                        "tempdir": "read_only",
                        "filesystem": {"read_write": [temporary]},
                    },
                    "environment": {"TMPDIR": temporary},
                }
            ),
        )
        return [
            run(
                binary,
                host,
                "import os; from pathlib import Path; Path(os.environ['TMPDIR'], 'probe').write_text('explicit'); print('explicit temp grant retained')",
            )
        ]


@requires(SANDBOX)
def test_complete_policy_selector_bypasses_project_and_console_adjustments(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        host = Path(directory).resolve()
        configure(host, "invalid: [")
        (host / ".claude").mkdir()
        policy = {
            "extends": ":workspace",
            "workspace_options": {
                "exclude_tmpdir_env_var": True,
                "exclude_slash_tmp": True,
            },
        }
        return [
            run(
                binary,
                host,
                # fmt: python
                code(r"""
                    import errno
                    from pathlib import Path

                    _ = Path("ordinary").write_text("native workspace")
                    # The native workspace profile owns its metadata defaults.
                    try:
                        _ = Path(".claude/native").write_text("native policy")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        raise AssertionError("native workspace metadata was writable")
                    print("complete native selector bypasses project discovery and application adjustments")
                    """),
                "--runner-config-env",
                "TEST_POLICY",
                environment={"TEST_POLICY": json.dumps(policy)},
            )
        ]


@requires(SANDBOX)
def test_workspace_options_preserve_explicit_native_values(binary: Path) -> Transcript:
    transcript = []
    for options, inherited_write, shared_write in (
        ({"exclude_tmpdir_env_var": False, "exclude_slash_tmp": True}, True, False),
        ({"exclude_tmpdir_env_var": True, "exclude_slash_tmp": False}, False, True),
        (None, True, True),
    ):
        with (
            TemporaryDirectory() as directory,
            TemporaryDirectory(dir="/var/tmp") as inherited,
            TemporaryDirectory(dir="/tmp") as shared,
        ):
            host = Path(directory).resolve()
            configuration = {
                "extends": ":workspace",
                "workspace_options": options,
            }
            configure(host, "{}")
            result = run(
                binary,
                host,
                # fmt: python
                code(r"""
                    import errno
                    import json
                    import os
                    from pathlib import Path

                    for key, allowed in json.loads(os.environ["EXPECTED_WRITES"]).items():
                        try:
                            _ = (Path(os.environ[key]) / "explicit").write_text("native option")
                        except OSError as error:
                            assert not allowed and error.errno in (
                                errno.EACCES,
                                errno.EPERM,
                                errno.EROFS,
                            ), error
                        else:
                            assert allowed, key
                    print("explicit native temporary-directory options preserved")
                    """),
                "--runner-config-env",
                "TEST_POLICY",
                environment={
                    "TEST_POLICY": json.dumps(
                        {"extends": ":workspace", "workspace_options": options}
                    ),
                    "TMPDIR": inherited,
                    "INHERITED_TEMP": inherited,
                    "SHARED_TEMP": shared,
                    "EXPECTED_WRITES": json.dumps(
                        {"INHERITED_TEMP": inherited_write, "SHARED_TEMP": shared_write}
                    ),
                },
            )
            transcript.append({"configuration": configuration, **result})
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
