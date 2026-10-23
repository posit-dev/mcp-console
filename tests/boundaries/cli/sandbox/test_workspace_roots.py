"""Broad write roots require an explicit absolute path."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript
from support.requirements import SANDBOX, case_insensitive_paths, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_broad_roots_require_absolute_write_grants(binary: Path) -> Transcript:
    transcript = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        # This case owns home discovery, preserving the caller's tool environment.
        home = root / "home"
        project = home / "project"
        project.mkdir(parents=True)
        (project / "home-link").symlink_to(home, target_is_directory=True)
        probe = home / "probe"
        config = root / "config.json"
        for name, launch, grant, writable in (
            ("relative_root", Path(home.anchor), ".", False),
            ("relative_home", home, ".", False),
            ("relative_home_parent", home.parent, ".", False),
            ("relative_parent", project, "..", False),
            ("relative_missing_parent", home, "missing/..", False),
            ("relative_symlink", project, "home-link", False),
            ("relative_project", project, ".", True),
            ("absolute_root", Path(home.anchor), home.anchor, True),
            ("absolute_home", home, str(home), True),
            ("absolute_home_parent", home.parent, str(home.parent), True),
        ):
            target = project / "probe" if name == "relative_project" else probe
            for cli in (False, True):
                target.write_text("unchanged")
                config.write_text(
                    json.dumps(
                        {"sandbox": {}}
                        if cli
                        else {"sandbox": {"filesystem": {"read_write": [grant]}}}
                    )
                )
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--config-file",
                        str(config),
                        *(("--writable-root", grant) if cli else ()),
                        "--",
                        sys.executable,
                        "-c",
                        # fmt: python
                        code("""
                            import errno
                            import os
                            from pathlib import Path

                            probe = Path(os.environ["TEST_PROBE"])
                            writable = os.environ["TEST_WRITABLE"] == "True"
                            assert probe.read_text() == "unchanged"
                            try:
                                _ = probe.write_text("changed")
                            except OSError as error:
                                assert not writable and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), (
                                    error
                                )
                            else:
                                assert writable, "relative root granted broad writes"
                            _ = (Path(os.environ["TMPDIR"]) / "private").write_text("allowed")
                            print("writable" if writable else "read-only")
                            """),
                    ],
                    cwd=launch,
                    env=os.environ
                    | {
                        "HOME": str(home),
                        "TEST_PROBE": str(target),
                        "TEST_WRITABLE": str(writable),
                    },
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result
                assert result.stdout == ("writable\n" if writable else "read-only\n"), (
                    result
                )
                assert target.read_text() == ("changed" if writable else "unchanged")
                expected = (
                    ""
                    if writable
                    else f"Warning: refusing relative writable root '{grant}'; use an absolute path to grant writes to the home directory or filesystem root\n"
                )
                assert result.stderr == expected, result
                transcript.append(
                    {
                        "scenario": name,
                        "source": "--writable-root" if cli else "filesystem.read_write",
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                    }
                )
    return transcript


@requires(SANDBOX)
def test_absent_home_write_grants_are_rejected(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "missing" / "home"
        for grant in ("missing", "missing/home"):
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--no-config",
                    "--writable-root",
                    grant,
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import errno
                        import os
                        from pathlib import Path

                        home = Path(os.environ["HOME"])
                        try:
                            home.mkdir(parents=True)
                            _ = (home / "probe").write_text("blocked")
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                        else:
                            raise AssertionError("absent HOME received a write grant")
                        print("absent home remains read-only")
                        """),
                ],
                cwd=root,
                env=os.environ | {"HOME": str(home)},
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result
            assert result.stdout == "absent home remains read-only\n", result
            assert result.stderr == (
                f"Warning: refusing relative writable root '{grant}'; use an absolute path to grant writes to the home directory or filesystem root\n"
            ), result
            assert not home.exists()
            records.append(
                {"grant": grant, "stdout": result.stdout, "stderr": result.stderr}
            )
        safe = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--writable-root",
                "output",
                "--",
                sys.executable,
                "-c",
                "print('unrelated absent root accepted')",
            ],
            cwd=root,
            env=os.environ | {"HOME": str(home)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert safe.returncode == 0 and safe.stderr == "", safe
        assert safe.stdout == "unrelated absent root accepted\n", safe
        records.append(
            {"grant": "output", "stdout": safe.stdout, "stderr": safe.stderr}
        )
    return records


@requires(SANDBOX)
def test_unknown_home_falls_back_to_read_only(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        launch_environment = dict(os.environ)
        launch_environment.pop("HOME", None)
        for profile in (True, False):
            result = subprocess.run(
                [
                    binary,
                    "sandbox",
                    "--no-config",
                    *(
                        ("-c", "sandbox.profile=workspace")
                        if profile
                        else ("--writable-root", ".")
                    ),
                    "--",
                    sys.executable,
                    "-c",
                    # fmt: python
                    code("""
                        import errno
                        import os
                        from pathlib import Path

                        try:
                            _ = Path("probe").write_text("blocked")
                        except OSError as error:
                            assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                        else:
                            raise AssertionError("unknown HOME granted workspace writes")
                        _ = (Path(os.environ["TMPDIR"]) / "allowed").write_text("allowed")
                        print("unknown home uses read-only with private temporary writes")
                        """),
                ],
                cwd=root,
                env=launch_environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert result.returncode == 0, result
            assert (
                result.stdout
                == "unknown home uses read-only with private temporary writes\n"
            ), result
            expected = (
                f"Warning: refusing automatic workspace writes to '{root}'; falling back to read_only\n"
                if profile
                else "Warning: refusing relative writable root '.'; use an absolute path to grant writes to the home directory or filesystem root\n"
            )
            assert result.stderr == expected, result
            records.append(
                {
                    "source": "profile" if profile else "--writable-root",
                    "stdout": result.stdout,
                    "stderr": result.stderr.replace(str(root), "<workspace>"),
                }
            )
    return records


@requires(SANDBOX)
def test_missing_home_preserves_safe_project_writes(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        alias = root / "project-alias"
        alias.symlink_to(project, target_is_directory=True)
        file = root / "file"
        file.write_text("not a directory")
        probe = project / "probe"
        for name, home, writable in (
            ("missing", root / "missing", True),
            ("not_directory", file / "home", True),
            ("missing_child", project / "missing", False),
            ("missing_alias_child", alias / "missing", False),
        ):
            for profile in (True, False):
                probe.write_text("unchanged")
                result = subprocess.run(
                    [
                        binary,
                        "sandbox",
                        "--no-config",
                        *(
                            ("-c", "sandbox.profile=workspace")
                            if profile
                            else ("--writable-root", ".")
                        ),
                        "--",
                        sys.executable,
                        "-c",
                        # fmt: python
                        code("""
                            import errno
                            import os
                            from pathlib import Path

                            writable = os.environ["TEST_WRITABLE"] == "True"
                            try:
                                _ = Path("probe").write_text("changed")
                            except OSError as error:
                                assert not writable and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), (
                                    error
                                )
                            else:
                                assert writable, "home ancestor granted broad writes"
                            print("writable" if writable else "read-only")
                            """),
                    ],
                    cwd=project,
                    env=os.environ
                    | {"HOME": str(home), "TEST_WRITABLE": str(writable)},
                    capture_output=True,
                    text=True,
                    timeout=30,
                )
                assert result.returncode == 0, result
                assert result.stdout == ("writable\n" if writable else "read-only\n"), (
                    result
                )
                assert probe.read_text() == ("changed" if writable else "unchanged")
                expected = (
                    ""
                    if writable
                    else (
                        f"Warning: refusing automatic workspace writes to '{project}'; falling back to read_only\n"
                        if profile
                        else "Warning: refusing relative writable root '.'; use an absolute path to grant writes to the home directory or filesystem root\n"
                    )
                )
                assert result.stderr == expected, result
                records.append(
                    {
                        "scenario": name,
                        "source": "profile" if profile else "--writable-root",
                        "stdout": result.stdout,
                        "stderr": result.stderr.replace(str(project), "<workspace>"),
                    }
                )
    return records


@requires(SANDBOX, case_insensitive_paths())
def test_home_case_alias_does_not_bypass_root_guard(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        home = Path(temporary).resolve() / "home"
        home.mkdir()
        alias = home.with_name("HOME")
        assert home.samefile(alias)
        probe = home / "probe"
        probe.write_text("unchanged")
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "-c",
                "sandbox.profile=workspace",
                "--",
                sys.executable,
                "-c",
                # fmt: python
                code("""
                    import errno
                    from pathlib import Path

                    try:
                        _ = Path("probe").write_text("changed")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        raise AssertionError("home alias granted broad writes")
                    print("home alias remains read-only")
                    """),
            ],
            cwd=home,
            env=os.environ | {"HOME": str(alias)},
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert result.returncode == 0, result
        assert result.stdout == "home alias remains read-only\n", result
        assert probe.read_text() == "unchanged"
        assert result.stderr == (
            f"Warning: refusing automatic workspace writes to '{home}'; falling back to read_only\n"
        ), result
        return [
            {
                "stdout": result.stdout,
                "stderr": result.stderr.replace(str(home), "<home>"),
            }
        ]


if __name__ == "__main__":
    run_this_suite(__file__)
