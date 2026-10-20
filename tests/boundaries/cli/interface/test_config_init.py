"""Create shared Console configuration through the public CLI."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from yaml12 import read_yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.records import Transcript
from support.suites import run_this_suite


WORKSPACE_CONFIGURATION = {"sandbox": {"profile": "workspace"}}


def invoke(
    binary: Path,
    root: Path,
    directory: Path,
    *arguments: str,
    extra_environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, *arguments],
        cwd=directory,
        env=os.environ
        | {
            "MCP_CONSOLE_HOME": str(root / "console-home"),
            "NO_COLOR": "1",
            "PATH": str(root / "empty-path"),
        }
        | (extra_environment or {}),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=15,
    )


def test_defaults_use_cwd_without_git_or_global_inference(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text("[]\n", encoding="utf-8")
        repository = root / "repository"
        repository.mkdir()
        (repository / ".git").write_text("gitdir: fixture\n", encoding="utf-8")
        for relative in ("outside-git", "repository/nested"):
            directory = root / relative
            directory.mkdir()
            result = invoke(binary, root, directory, "config", "init")
            assert result.returncode == 0 and not result.stderr, result
            path = directory / ".agents/console/config.yaml"
            assert result.stdout.startswith(f"Created configuration: {path}\n\n"), (
                result
            )
            assert read_yaml(path) == WORKSPACE_CONFIGURATION
            assert "## Full reference:" in path.read_text(encoding="utf-8")
            assert not (repository / ".agents").exists()
            records.append(
                {"directory": relative, "configuration": WORKSPACE_CONFIGURATION}
            )
        assert (home / "config.yaml").read_text(encoding="utf-8") == "[]\n"
    return records


def test_directory_and_global_destinations(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        caller = root / "caller"
        caller.mkdir()
        for selection in ("relative", "absolute", "global"):
            project = root / f"{selection} project"
            project.mkdir()
            if selection == "global":
                arguments = ["--global"]
                extra = {}
                path = root / "console-home/config.yaml"
            else:
                arguments = [
                    "--directory",
                    str(project) if selection == "absolute" else f"../{project.name}",
                ]
                # Local initialization must not inspect global configuration.
                extra = {"MCP_CONSOLE_HOME": "relative-home"}
                path = project / ".agents/console/config.yaml"
            result = invoke(
                binary,
                root,
                caller,
                "config",
                "init",
                *arguments,
                extra_environment=extra,
            )
            assert result.returncode == 0 and not result.stderr, result
            assert result.stdout.startswith(f"Created configuration: {path}\n\n"), (
                result
            )
            assert read_yaml(path) == WORKSPACE_CONFIGURATION
            assert not (caller / ".agents").exists()
            records.append(
                {"destination": selection, "configuration": WORKSPACE_CONFIGURATION}
            )
    return records


def test_directory_validates_and_retains_relative_settings(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project = root / "project"
        project.mkdir()
        executable = project / Path(sys.executable).name
        # A real executable is present only in the selected project. Validation
        # may inspect its path, but initialization must not execute Python.
        shutil.copy2(sys.executable, executable)
        result = invoke(
            binary,
            root,
            root,
            "config",
            "init",
            "--directory",
            str(project),
            "-c",
            f"python={executable.name}",
            "-c",
            "cache.directory=host",
        )
        assert result.returncode == 0 and not result.stderr, result
        assert read_yaml(
            project / ".agents/console/config.yaml"
        ) == WORKSPACE_CONFIGURATION | {
            "python": executable.name,
            "cache": {"directory": "host"},
        }
        assert not (root / ".agents").exists()
        assert not (project / "host").exists()
    return [
        {
            "relative_settings": "validated in the selected project and saved without absolute path expansion"
        }
    ]


def test_ordered_settings_are_saved_without_copying_discovered_values(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "environment: {INHERITED: global}\nr: {vanilla: true}\n", encoding="utf-8"
        )
        result = invoke(
            binary,
            root,
            root,
            "-c",
            "environment={KEEP: explicit, VALUE: first}",
            "-c",
            "r.packages=[stats]",
            "config",
            "init",
            "--config",
            "environment.VALUE=last",
            "-c",
            "r.packages=[]",
            "-c",
            "r.vanilla=false",
            "-c",
            "startup=null",
        )
        assert result.returncode == 0 and not result.stderr, result
        path = root / ".agents/console/config.yaml"
        expected = WORKSPACE_CONFIGURATION | {
            "environment": {"KEEP": "explicit", "VALUE": "last"},
            "r": {"packages": [], "vanilla": False},
            "startup": None,
        }
        assert read_yaml(path) == expected
        # The new file remains one layer: normal launch discovery still merges
        # the global defaults rather than freezing them into the project file.
        resolved = invoke(
            binary, root, root, "config", "resolve", "--full", "--format", "json"
        )
        assert resolved.returncode == 0 and not resolved.stderr, resolved
        value = json.loads(resolved.stdout)
        assert value["environment"] == expected["environment"] | {"INHERITED": "global"}
        assert value["r"]["packages"] == [] and value["r"]["vanilla"] is False
    return [
        {"saved": expected, "global_values": "remain a separate configuration layer"}
    ]


def test_dry_run_previews_the_created_contents(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        arguments = ["config", "init"]
        preview = invoke(binary, root, root, *arguments, "--dry-run")
        assert preview.returncode == 0 and not preview.stderr, preview
        assert not (root / ".agents").exists()
        assert not (root / "console-home").exists()
        result = invoke(binary, root, root, *arguments)
        assert result.returncode == 0 and not result.stderr, result
        path = root / ".agents/console/config.yaml"
        contents = path.read_text(encoding="utf-8")
        assert contents in preview.stdout and contents in result.stdout
        assert read_yaml(path) == WORKSPACE_CONFIGURATION
        assert preview.stdout.startswith(f"Would create configuration: {path}\n\n"), (
            preview
        )
        assert result.stdout.startswith(f"Created configuration: {path}\n\n"), result
    return [
        {
            "preview": preview.stdout.replace(
                str(path), "<WORKSPACE>/.agents/console/config.yaml"
            )
        },
        {
            "created": result.stdout.replace(
                str(path), "<WORKSPACE>/.agents/console/config.yaml"
            )
        },
    ]


def test_template_examples_can_be_uncommented_and_loaded(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = invoke(binary, root, root, "config", "init")
        assert result.returncode == 0 and not result.stderr, result
        path = root / ".agents/console/config.yaml"
        contents = path.read_text(encoding="utf-8")
        assert (
            "## The workspace profile allows creating, editing, and deleting project files."
            in contents
        )
        assert (
            "## .git, .agents, .codex, and .aws stay read-only, even when absent."
            in contents
        )
        assert "## Temporary storage options:" in contents
        assert (
            "## R defaults to implicit; managed Python defaults to explicit."
            in contents
        )
        assert "#   allow_local_binding: true" in contents
        assert "#   directory: /absolute/path/to/console-cache" in contents
        output = root / "shared-output"
        output.mkdir()
        (root / ".env").write_text("SECRET=fixture\n", encoding="utf-8")
        (root / "secrets").mkdir()
        lines = []
        for line in contents.splitlines():
            # Choose managed Python; the scalar example is the alternative.
            if line == "# python: .venv":
                continue
            if line.lstrip().startswith("# "):
                line = line.replace("# ", "", 1)
            line = line.replace(
                "/absolute/path/to/shared-output", output.as_posix()
            ).replace("/absolute/path/to/console-cache", (root / "cache").as_posix())
            lines.append(line)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        resolved = invoke(
            binary, root, root, "config", "resolve", "--full", "--format", "json"
        )
        assert resolved.returncode == 0 and not resolved.stderr, resolved
        value = json.loads(resolved.stdout)
        assert value["r"]["resolution"] == "implicit"
        assert value["python"]["managed"]["resolution"] == "explicit"
        assert value["sandbox"]["network"]["allow_local_binding"] is True
        assert value["python"]["managed"]["packages"] == [
            "numpy",
            "pandas",
            "matplotlib",
        ]
        assert value["r"]["packages"] == ["dplyr", "ggplot2"]
        assert value["environment"] == {"OMP_NUM_THREADS": "4"}
        assert set(value["cache"]) == {"directory"}
        assert Path(value["cache"]["directory"]) == root / "cache"
        assert not (root / "cache").exists()
    return [{"template_examples": "uncommented and accepted by config resolve"}]


def test_settings_override_the_starter_permissions(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for scenario, arguments, expected in (
            (
                "clear sandbox",
                ("sandbox=null",),
                {"sandbox": None},
            ),
            (
                "read-only sandbox",
                ("sandbox.profile=read_only",),
                {"sandbox": {"profile": "read_only"}},
            ),
            (
                "inherited client sandbox",
                ("sandbox.profile=inherit_codex",),
                {"sandbox": {"profile": "inherit_codex"}},
            ),
            (
                "empty sandbox mapping",
                ("sandbox=null", "sandbox={}"),
                {"sandbox": {}},
            ),
            (
                "replace write grants",
                ("sandbox.filesystem.read_write=[]",),
                {
                    "sandbox": {
                        "filesystem": {
                            "read_write": [],
                        },
                        "profile": "workspace",
                    }
                },
            ),
            (
                "configured proxy and local binding",
                (
                    "sandbox.network.proxy.domains.allow=[api.example.com]",
                    "sandbox.network.allow_local_binding=true",
                ),
                {
                    "sandbox": WORKSPACE_CONFIGURATION["sandbox"]
                    | {
                        "network": {
                            "proxy": {"domains": {"allow": ["api.example.com"]}},
                            "allow_local_binding": True,
                        }
                    }
                },
            ),
        ):
            directory = root / scenario
            directory.mkdir()
            result = invoke(
                binary,
                root,
                directory,
                "config",
                "init",
                *(argument for value in arguments for argument in ("-c", value)),
            )
            assert result.returncode == 0 and not result.stderr, result
            path = directory / ".agents/console/config.yaml"
            assert read_yaml(path) == expected
            records.append({"scenario": scenario, "configuration": expected})
    return records


def test_launch_profile_overrides_do_not_retain_workspace_grants(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = invoke(binary, root, root, "config", "init")
        assert result.returncode == 0 and not result.stderr, result
        for profile in ("read_only", "inherit_codex"):
            resolved = invoke(
                binary,
                root,
                root,
                "config",
                "resolve",
                "--full",
                "--format",
                "json",
                "-c",
                f"sandbox.profile={profile}",
            )
            assert resolved.returncode == 0 and not resolved.stderr, resolved
            sandbox = json.loads(resolved.stdout)["sandbox"]
            assert sandbox["profile"] == profile, sandbox
            assert not sandbox.get("filesystem", {}).get("read_write"), sandbox
            records.append({"launch_profile": profile, "workspace_grants": []})
    return records


def test_profile_guidance_matches_the_selected_policy(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for name, assignment, guidance in (
            (
                "read_only",
                "read_only",
                "The read_only profile does not grant writes to project files.",
            ),
            (
                "inherit_codex",
                "inherit_codex",
                "The inherit_codex profile uses permissions supplied by the client.",
            ),
            (
                "inherit with fallback",
                "{inherit_codex: {fallback: workspace}}",
                "The inherit_codex profile uses permissions supplied by the client.",
            ),
            (
                "no profile",
                "null",
                "Without a named profile, the base policy grants no project writes.",
            ),
        ):
            directory = root / name
            directory.mkdir()
            result = invoke(
                binary,
                root,
                directory,
                "config",
                "init",
                "-c",
                f"sandbox.profile={assignment}",
            )
            assert result.returncode == 0 and not result.stderr, result
            contents = (directory / ".agents/console/config.yaml").read_text(
                encoding="utf-8"
            )
            assert guidance in contents, contents
            assert (
                "profile allows creating, editing, and deleting project files"
                not in contents
            )
            assert ".git, .agents, .codex, and .aws stay read-only" not in contents
            records.append({"profile": name, "guidance": guidance})
    return records


def test_existing_configuration_is_never_replaced(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        path = root / ".agents/console/config.yaml"
        path.parent.mkdir(parents=True)
        contents = "# Existing file, even when it is not valid configuration.\n[]\n"
        path.write_text(contents, encoding="utf-8")
        identity = path.stat().st_ino
        for extra in ((), ("--dry-run",), ("-c", "r.vanilla=true")):
            result = invoke(binary, root, root, "config", "init", *extra)
            assert result.returncode == 1 and not result.stdout, result
            assert (
                result.stderr
                == f"Configuration already exists at {path}; no files changed\n"
            ), result
            assert path.read_text(encoding="utf-8") == contents
            assert path.stat().st_ino == identity
    return [
        {
            "existing_configuration": "reported and preserved without parsing or replacement"
        }
    ]


def test_invalid_settings_and_destinations_do_not_create_files(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        file = root / "file"
        file.write_text("not a directory", encoding="utf-8")
        for scenario, arguments, status, diagnostic in (
            ("missing assignment", ("-c", "environment"), 1, "KEY=VALUE"),
            ("unknown field", ("-c", "unknown_setting=true"), 1, "unknown_setting"),
            (
                "environment value type",
                ("-c", "environment.COUNT=42"),
                1,
                "environment",
            ),
            ("unknown profile", ("-c", "sandbox.profile=unknown"), 1, "profile"),
            ("directory is a file", ("--directory", str(file)), 1, "directory"),
            (
                "missing directory",
                ("--directory", str(root / "missing")),
                1,
                "directory",
            ),
            (
                "conflicting destinations",
                ("--global", "--directory", str(root)),
                2,
                "--global",
            ),
        ):
            result = invoke(binary, root, root, "config", "init", *arguments)
            assert result.returncode == status and not result.stdout, result
            assert diagnostic in result.stderr, result
            assert not (root / ".agents").exists()
            assert not (root / "console-home").exists()
            records.append({"rejected": scenario, "files_created": False})
        for flag in ("--no-config", "--no-global-config", "--no-project-config"):
            result = invoke(binary, root, root, flag, "config", "init")
            assert result.returncode == 1 and not result.stdout, result
            assert flag in result.stderr, result
            assert not (root / ".agents").exists()
        result = invoke(
            binary, root, root, "--config-file", str(file), "config", "init"
        )
        assert result.returncode == 1 and not result.stdout, result
        assert "--config-file" in result.stderr, result
        assert not (root / ".agents").exists()
        result = invoke(
            binary,
            root,
            root,
            "--default-config",
            "sandbox.profile=inherit_codex",
            "config",
            "init",
        )
        assert result.returncode == 1 and not result.stdout, result
        assert "--default-config" in result.stderr, result
        assert not (root / ".agents").exists()
    return records + [{"configuration_source_flags": "rejected before writing"}]


if __name__ == "__main__":
    run_this_suite(__file__)
