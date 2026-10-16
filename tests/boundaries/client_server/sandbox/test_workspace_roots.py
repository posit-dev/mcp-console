"""Launch directories must not implicitly grant writes to the home or host."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.requirements import NATIVE_FIXTURES, SANDBOX, requires
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import environment


def test_workspace_guard_uses_console_home(binary: Path) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        home.mkdir()
        profile = root / "profile"
        profile.mkdir()
        launch_environment = os.environ | {
            "HOME": str(home),
            "USERPROFILE": str(profile),
            "MCP_CONSOLE_HOME": str(root / "console"),
        }
        arguments = (
            "serve",
            "--no-config",
            # Keep runtime startup lazy: this case only inspects launch policy.
            "--worker",
            str(binary),
            "-c",
            "sandbox.profile=workspace",
            "-c",
            "cache=host",
            "-c",
            "languages=[r,python,sql]",
        )
        with McpClient(
            binary, arguments, launch_environment, home, record_in_project=False
        ) as client:
            client.initialize_and_list_tools()
            description = client.transcript[-1]["result"]["tools"][0]["description"]
            assert "Writable locations: private `TMPDIR`;" in description, description
            transcript, stderr = client.finish_with_standard_error()
        assert stderr == (
            f"Warning: refusing automatic workspace writes to '{home}'; falling back to read_only\n"
        ), stderr
        transcript.append({"stderr": stderr.replace(str(home), "<home>")})
        relative = subprocess.run(
            [binary, *arguments],
            input="",
            cwd=home,
            env=launch_environment | {"HOME": "."},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert relative.returncode == 1 and relative.stdout == "", relative
        assert relative.stderr == "HOME must be an absolute path\n", relative
        return TranscriptWithCompanions(
            transcript, {"relative-home.yaml": [{"stderr": relative.stderr}]}
        )


@requires(SANDBOX)
def test_broad_workspace_roots_fall_back_to_read_only(
    binary: Path,
) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        # This case owns home discovery; keep writes and recordings in the fixture.
        home = root / "home"
        project = home / "project"
        project.mkdir(parents=True)
        output = root / "output"
        output.mkdir()
        probe = home / "probe"
        probe.write_text("unchanged")
        config = root / "config.json"
        transcripts = {}
        for profile in ("workspace", {"inherit_codex": {"fallback": "workspace"}}):
            fallback = isinstance(profile, dict)
            config.write_text(
                json.dumps(
                    {
                        "languages": ["python"],
                        "python": {"existing": sys.executable},
                        "cache": "host",
                        "sandbox": {
                            "profile": profile,
                            "filesystem": {"read_write": [str(output)]},
                        },
                    }
                )
            )
            for name, launch in (
                ("root", Path(home.anchor)),
                ("home", home),
                ("home_parent", home.parent),
                ("project", project),
            ):
                writable = name == "project"
                with McpClient(
                    binary,
                    ("serve", "--config-file", str(config)),
                    environment(root)
                    | {
                        "HOME": str(home),
                        "TEST_PROBE": str(project / "probe" if writable else probe),
                        "TEST_OUTPUT": str(output),
                        "TEST_WRITABLE": str(writable),
                    },
                    launch,
                    record_in_project=False,
                ) as client:
                    client.initialize_and_list_tools()
                    description = client.transcript[-1]["result"]["tools"][0][
                        "description"
                    ]
                    for generation in range(2):
                        client.expect(
                            "project writes allowed; explicit output and private temp writable\n"
                            if writable
                            else "broad workspace read-only; explicit output and private temp writable\n",
                            # fmt: python
                            python=code("""
                                import errno
                                import os
                                from pathlib import Path

                                writable = os.environ["TEST_WRITABLE"] == "True"
                                probe = Path(os.environ["TEST_PROBE"])
                                try:
                                    _ = probe.write_text("changed")
                                except OSError as error:
                                    assert not writable and error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), (
                                        error
                                    )
                                    assert probe.read_text() == "unchanged"
                                else:
                                    assert writable, "launch directory granted broad writes"
                                _ = (Path(os.environ["TEST_OUTPUT"]) / "explicit").write_text("allowed")
                                _ = (Path(os.environ["TMPDIR"]) / "private").write_text("allowed")
                                print(
                                    "project writes allowed; explicit output and private temp writable"
                                    if writable
                                    else "broad workspace read-only; explicit output and private temp writable"
                                )
                                """),
                        )
                        if generation == 0:
                            client.send(control="restart")
                    if not fallback:
                        assert (
                            "private `TMPDIR`, workspace," in description
                        ) is writable
                        assert json.dumps(str(output)) in description, description
                    transcript, stderr = client.finish_with_standard_error()
                expected = (
                    ""
                    if writable
                    else f"Warning: refusing automatic workspace writes to '{launch}'; falling back to read_only\n"
                )
                assert stderr == expected, stderr
                if stderr:
                    transcript.append(
                        {"stderr": stderr.replace(str(launch), "<launch>")}
                    )
                transcript.insert(
                    0,
                    {
                        "scenario": f"Launch from {name.replace('_', ' ')} with a workspace {'fallback' if fallback else 'profile'}.",
                        "fixture": "HOME is isolated. The worker edits a probe under the launch directory, writes to explicitly granted output and private TMPDIR, then repeats after restart.",
                    },
                )
                transcript = json.loads(
                    json.dumps(transcript)
                    .replace(str(output), "<writable-root>")
                    .replace(str(root), "<fixture>")
                )
                transcripts[f"{name}{'_fallback' if fallback else ''}"] = transcript
        return TranscriptWithCompanions(
            transcripts.pop("root"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


@requires(SANDBOX, NATIVE_FIXTURES)
def test_resolver_requires_absolute_broad_write_grants(
    binary: Path,
) -> TranscriptWithCompanions:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        home.mkdir()
        library = build_interposer(root, "runner_configuration")
        config = root / "config.json"
        transcripts = {}
        for name, policy in (
            ("profile", {"profile": "workspace"}),
            ("relative_cache", {}),
            ("relative_extension_cache", {}),
            (
                "relative",
                {"filesystem": {"read_only": [home.anchor], "read_write": ["."]}},
            ),
            (
                "absolute",
                {"filesystem": {"read_only": [home.anchor], "read_write": [str(home)]}},
            ),
        ):
            capture = root / f"{name}.jsonl"
            config.write_text(
                json.dumps(
                    {
                        "languages": ["python"],
                        "python": {"existing": sys.executable},
                        "cache": "host",
                        "resolver": {
                            "sandbox": policy,
                            "environment": {"TEST_POLICY_OWNER": "resolver"}
                            | {
                                "relative_cache": {"UV_CACHE_DIR": "."},
                                "relative_extension_cache": {
                                    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY": "."
                                },
                            }.get(name, {}),
                        },
                    }
                )
            )
            with McpClient(
                binary,
                ("serve", "--config-file", str(config)),
                environment(root)
                | {
                    "HOME": str(home),
                    LOADER_VARIABLE: str(library),
                    "MCP_CONSOLE_TEST_RESOLVER_CONFIGURATION": str(capture),
                    "MCP_CONSOLE_TEST_RUNNER_CONFIGURATION": str(
                        root / "workers.jsonl"
                    ),
                },
                home,
                record_in_project=False,
            ) as client:
                client.initialize_and_list_tools()
                client.expect("ready\n", python="print('ready')")
                transcript, stderr = client.finish_with_standard_error()
            launches = [json.loads(line) for line in capture.read_text().splitlines()]
            assert launches, launches
            resolver_launches = 0
            for launch in launches:
                assert "extends" not in launch, launch
                broad = {
                    "path": {"type": "path", "path": str(home)},
                    "access": "write",
                }
                resolver = (
                    launch.get("environment", {}).get("TEST_POLICY_OWNER") == "resolver"
                )
                resolver_launches += resolver
                assert (broad in launch["filesystem"]["entries"]) is (
                    resolver and name == "absolute"
                ), (name, launch["filesystem"])
                assert "private_tmp" in launch["lifecycle"], launch
            assert resolver_launches > 0, launches
            expected = {
                "profile": f"Warning: refusing automatic workspace writes to '{home}'; falling back to read_only\n",
                "relative": "Warning: refusing relative writable root '.'; use an absolute path to grant writes to the home directory or filesystem root\n",
                "relative_cache": "Warning: refusing relative writable root '.'; use an absolute path to grant writes to the home directory or filesystem root\n",
                "relative_extension_cache": "Warning: refusing relative writable root '.'; use an absolute path to grant writes to the home directory or filesystem root\n",
                "absolute": "",
            }[name]
            assert stderr == expected, stderr
            transcript.insert(
                0,
                {
                    "scenario": f"Resolver {name} configuration launched from HOME.",
                    "fixture": "HOME is isolated. A trusted interposer records the policies passed to the real native resolver runner; the worker uses its default read-only base.",
                },
            )
            if stderr:
                transcript.append({"stderr": stderr.replace(str(home), "<home>")})
            transcripts[name] = transcript
        return TranscriptWithCompanions(
            transcripts.pop("profile"),
            {
                f"{name}.yaml": McpTranscript(transcript)
                for name, transcript in transcripts.items()
            },
        )


@requires(SANDBOX)
def test_retargeted_relative_grant_stays_read_only_after_restart(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        project = home / "project"
        grant = project / "grant"
        grant.mkdir(parents=True)
        (home / "probe").write_text("unchanged")
        config = root / "config.json"
        config.write_text(
            json.dumps(
                {
                    "languages": ["python"],
                    "python": {"existing": sys.executable},
                    "cache": "host",
                    "sandbox": {
                        "profile": "workspace",
                        "filesystem": {"read_write": ["grant"]},
                    },
                }
            )
        )
        with McpClient(
            binary,
            ("serve", "--config-file", str(config)),
            environment(root) | {"HOME": str(home)},
            project,
            record_in_project=False,
        ) as client:
            client.initialize_and_list_tools()
            description = client.transcript[-1]["result"]["tools"][0]["description"]
            assert json.dumps(str(grant)) in description, description
            client.expect(
                "relative grant used\n",
                # fmt: python
                python=code("""
                    from pathlib import Path

                    grant = Path("grant")
                    initial = grant / "initial"
                    _ = initial.write_text("allowed")
                    initial.unlink()
                    print("relative grant used")
                    """),
            )
            # The host changes the path while the worker is idle. macOS forbids
            # deleting a granted root from inside the worker; Linux permits the
            # original symlink form. Both must inspect the next launch's target.
            grant.rmdir()
            grant.symlink_to(home, target_is_directory=True)
            client.send(control="restart")
            result = client.send(
                # fmt: python
                python=code("""
                    import errno
                    import os
                    from pathlib import Path

                    probe = Path(os.environ["HOME"]) / "probe"
                    try:
                        _ = probe.write_text("blocked")
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
                    else:
                        raise AssertionError("retargeted relative grant made home writable")
                    assert probe.read_text() == "unchanged"
                    print("home remains read-only after restart")
                    """),
            )
            assert not result.get("isError"), result
            assert any(
                "home remains read-only after restart\n" in part.get("text", "")
                for part in result["content"]
            ), result
            transcript, stderr = client.finish_with_standard_error()
        assert (home / "probe").read_text() == "unchanged"
        if stderr:
            transcript.append({"stderr": stderr})
        return json.loads(
            json.dumps(transcript)
            .replace(str(grant), "<writable-root>")
            .replace(str(root), "<fixture>")
        )


if __name__ == "__main__":
    run_this_suite(__file__)
