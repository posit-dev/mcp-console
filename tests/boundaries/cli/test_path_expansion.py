"""Expand home-relative public paths without interpreting arbitrary strings."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.requirements import MACOS_SANDBOX, POSIX, requires
from support.suites import run_this_suite


def resolve(
    binary: Path, root: Path, environment: dict[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [binary, "config", "resolve", "--format", "json", *arguments],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_materializes_home_paths_and_preserves_literal_values(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home with spaces"
        home.mkdir()
        shutil.copy2(sys.executable, home / "python")
        environment = os.environ | {
            "HOME": str(home),
            "R_USER": str(root / "r-home"),
            "MCP_CONSOLE_HOME": str(root / "console-home"),
        }
        filesystem = {
            "read_only": ["~/data"],
            "read_write": ["~/output", "./~/literal", "~someone/data", "${DATA}/data"],
            "deny": ["~"],
        }
        selected = root / "config.json"
        source = {
            "r": "~/R/bin/R",
            "sandbox": {"filesystem": filesystem},
            "environment": {"HOME": str(root / "child-home"), "LITERAL": "~/${HOME}"},
            "tool": {"instructions": "Keep ~ and ${HOME} literal."},
        }
        for python in (
            "~/python",
            {"existing": "~/python"},
            {"first_available": [{"existing": "~/python"}, {"managed": {}}]},
        ):
            selected.write_text(
                json.dumps(source | {"python": python}), encoding="utf-8"
            )
            result = resolve(binary, root, environment, "--config-file", str(selected))
            assert result.returncode == 0 and result.stderr == "", result
            settings = json.loads(result.stdout)
            assert settings["r"] == {"executable": str(home / "R/bin/R")}, settings
            expected_python = (
                {
                    "first_available": [
                        {"existing": str(home / "python")},
                        {"managed": {"resolution": "explicit"}},
                    ]
                }
                if isinstance(python, dict) and "first_available" in python
                else {"existing": str(home / "python")}
            )
            assert settings["python"] == expected_python, settings
            assert settings["sandbox"]["filesystem"] == {
                "read_only": [str(home / "data")],
                "read_write": [str(home / "output"), *filesystem["read_write"][1:]],
                "deny": [str(home)],
            }, settings
            assert settings["environment"] == source["environment"], settings
            assert settings["tool"]["instructions"] == source["tool"]["instructions"], (
                settings
            )
            selected.write_text(result.stdout, encoding="utf-8")
            again = resolve(binary, root, environment, "--config-file", str(selected))
            assert again.returncode == 0 and json.loads(again.stdout) == settings, again
    return [
        {
            "home_paths_materialized": True,
            "literal_values_preserved": True,
            "round_trip": True,
        }
    ]


def test_expands_selected_file_and_console_home(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        console = home / "console"
        console.mkdir(parents=True)
        (console / "config.yaml").write_text("languages: [r]\n", encoding="utf-8")
        (home / "selected.yaml").write_text("languages: [python]\n", encoding="utf-8")
        environment = os.environ | {"HOME": str(home), "MCP_CONSOLE_HOME": "~/console"}
        discovered = resolve(binary, root, environment)
        assert discovered.returncode == 0 and discovered.stderr == "", discovered
        assert json.loads(discovered.stdout)["languages"] == ["r"], discovered
        selected = resolve(
            binary, root, environment, "--config-file", "~/selected.yaml"
        )
        assert selected.returncode == 0 and selected.stderr == "", selected
        assert json.loads(selected.stdout)["languages"] == ["python"], selected
    return [{"console_home_discovered": True, "explicit_file_selected": True}]


def test_expands_only_effective_paths_after_layering(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        console = root / "console"
        console.mkdir()
        (console / "config.yaml").write_text(
            "r: ~/unused\nsandbox: {filesystem: {deny: [~/unused]}}\n", encoding="utf-8"
        )
        project = root / ".agents/console"
        project.mkdir(parents=True)
        (project / "config.yaml").write_text("r: relative-R\n", encoding="utf-8")
        result = resolve(
            binary,
            root,
            os.environ | {"HOME": "relative-home", "MCP_CONSOLE_HOME": str(console)},
            "-c",
            "sandbox.filesystem.deny=[relative-denial]",
        )
        assert result.returncode == 0 and result.stderr == "", result
        settings = json.loads(result.stdout)
        assert settings["r"] == {"executable": str(root / "relative-R")}, settings
        assert settings["sandbox"]["filesystem"]["deny"] == ["relative-denial"], (
            settings
        )
    return [{"overridden_home_paths_do_not_resolve_home": True}]


def test_home_paths_reject_relative_home(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment = os.environ | {
            "HOME": "relative-home",
            "MCP_CONSOLE_HOME": str(root / "console"),
        }
        for arguments in (
            ("--no-config", "-c", "r.executable=~/R"),
            ("--no-config", "-c", "python=~/python"),
            ("--no-config", "-c", "sandbox.filesystem.deny=[~/private]"),
            ("--config-file", "~/config.yaml"),
        ):
            result = resolve(binary, root, environment, *arguments)
            assert result.returncode == 1 and result.stdout == "", result
            assert "HOME must be an absolute path" in result.stderr, result
            records.append({"arguments": list(arguments), "stderr": result.stderr})
    return records


@requires(POSIX)
def test_materializes_resolver_home_paths(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(
            binary,
            root,
            os.environ | {"HOME": str(root), "MCP_CONSOLE_HOME": str(root / "console")},
            "--no-config",
            "-c",
            "resolver.environment.HOME=/resolver-home",
            "-c",
            "resolver.sandbox.filesystem={read_only: [~/data], read_write: [~/cache], deny: [~/private]}",
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert json.loads(result.stdout)["resolver"]["sandbox"]["filesystem"] == {
            "read_only": [str(root / "data")],
            "read_write": [str(root / "cache")],
            "deny": [str(root / "private")],
        }, result
    return [{"resolver_paths_use_launch_home": True}]


@requires(MACOS_SANDBOX)
def test_materializes_home_socket_paths(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(
            binary,
            root,
            os.environ | {"HOME": str(root), "MCP_CONSOLE_HOME": str(root / "console")},
            "--no-config",
            "-c",
            "sandbox.network={proxy: {}, sockets: {unix_sockets: [~/agent.sock]}}",
        )
        assert result.returncode == 0 and result.stderr == "", result
        assert json.loads(result.stdout)["sandbox"]["network"]["sockets"][
            "unix_sockets"
        ] == [str(root / "agent.sock")], result
    return [{"home_socket_materialized": True}]


@requires(POSIX)
def test_home_expansion_preserves_symlinks(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        home.mkdir()
        (home / "python").symlink_to(sys.executable)
        result = resolve(
            binary,
            root,
            os.environ | {"HOME": str(home), "MCP_CONSOLE_HOME": str(root / "console")},
            "--no-config",
            "-c",
            "python=~/python",
            "-c",
            "sandbox.filesystem.read_only=[~/python]",
        )
        assert result.returncode == 0 and result.stderr == "", result
        settings = json.loads(result.stdout)
        assert settings["python"] == {"existing": str(home / "python")}, settings
        assert settings["sandbox"]["filesystem"]["read_only"] == [
            str(home / "python")
        ], settings
    return [{"symlink_spelling_preserved": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
