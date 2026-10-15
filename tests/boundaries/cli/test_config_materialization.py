"""Inspect and edit captured configuration through the public CLI."""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.client import McpClient
from support.normalization import code
from support.records import Transcript
from support.requirements import NON_UTF8_FILENAMES, POSIX, requires
from support.public_configuration import CONFIGURATION_EXAMPLES
from support.suites import run_this_suite


def resolve(
    binary: Path,
    directory: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [binary, "config", "resolve", *arguments],
        cwd=directory,
        env=(os.environ if environment is None else environment)
        | {"MCP_CONSOLE_HOME": str(directory / "console-home")},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_prunes_builtin_defaults_by_default(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        defaults = {
            "languages": ["r", "python", "sql"],
            "r": {"vanilla": False, "resolution": "automatic"},
            "python": None,
            "startup": None,
            "inherit_environment": True,
            "environment": {},
            "tool": {"parameters": {"timeout_ms": {"enabled": True}}},
        }
        selected = root / "defaults.json"
        selected.write_text(json.dumps(defaults), encoding="utf-8")
        for mode in ((), ("--no-sandbox",)):
            for selection in (("--no-config",), ("--config-file", str(selected))):
                for output in ((), ("--prune",)):
                    result = resolve(
                        binary, root, *selection, *mode, *output, "--format", "json"
                    )
                    assert result.returncode == 0 and result.stderr == "", result
                    assert json.loads(result.stdout) == {}, result.stdout
            yaml = resolve(binary, root, "--no-config", *mode, "--prune")
            assert yaml.returncode == 0 and yaml.stderr == "", yaml
            selected.write_text(yaml.stdout, encoding="utf-8")
            again = resolve(
                binary, root, "--config-file", str(selected), *mode, "--format", "json"
            )
            assert again.returncode == 0 and json.loads(again.stdout) == {}, again
            selected.write_text(json.dumps(defaults), encoding="utf-8")
    return [{"default_and_prune_match": True, "builtin_defaults": {}}]


def test_prunes_against_builtin_defaults_after_layering(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            json.dumps({"r": {"vanilla": True, "resolution": "startup_only"}}),
            encoding="utf-8",
        )
        project = root / ".agents" / "console"
        project.mkdir(parents=True)
        (project / "config.yaml").write_text(
            json.dumps({"r": {"resolution": "automatic"}}), encoding="utf-8"
        )
        result = resolve(binary, root, "--format", "json", "-c", "r.packages=[]")
        assert result.returncode == 0 and result.stderr == "", result
        settings = json.loads(result.stdout)
        assert settings == {"r": {"vanilla": True, "packages": []}}, settings
        explicit = resolve(
            binary, root, "--prune", "--format", "json", "-c", "r.packages=[]"
        )
        assert explicit.returncode == 0 and json.loads(explicit.stdout) == settings
    return [settings]


def test_prunes_nested_values_without_losing_empty_overrides(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(
            binary,
            root,
            "--no-config",
            "--format",
            "json",
            "-c",
            "environment={EMPTY: ''}",
            "-c",
            "inherit_environment=false",
            "-c",
            "resolver.inherit_environment=true",
            "-c",
            "sandbox.filesystem={}",
            "-c",
            "python.managed.packages=[]",
        )
        assert result.returncode == 0 and result.stderr == "", result
        settings = json.loads(result.stdout)
        assert settings == {
            "environment": {"EMPTY": ""},
            "inherit_environment": False,
            "resolver": {"environment": {"EMPTY": ""}, "inherit_environment": True},
            "sandbox": {"filesystem": {"read_only": [], "read_write": [], "deny": []}},
            "python": {"managed": {"packages": [], "resolution": "automatic"}},
        }, settings
        selected = root / "pruned.json"
        selected.write_text(result.stdout, encoding="utf-8")
        again = resolve(
            binary, root, "--config-file", str(selected), "--format", "json"
        )
        assert again.returncode == 0 and again.stderr == "", again
        assert json.loads(again.stdout) == settings
    return [settings]


def test_pruned_tool_settings_round_trip(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = root / "selected.json"
        selected.write_text(
            json.dumps(
                {
                    "languages": ["python"],
                    "tool": {
                        "instructions": "",
                        "parameters": {
                            "timeout_ms": {"enabled": False},
                            "python": {"description": ""},
                            "requirements": {
                                "parameters": {"action": {"description": "Choose."}}
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )
        full = resolve(
            binary, root, "--config-file", str(selected), "--full", "--format", "json"
        )
        assert full.returncode == 0 and full.stderr == "", full
        result = resolve(binary, root, "--config-file", str(selected), "--prune")
        assert result.returncode == 0 and result.stderr == "", result
        selected.write_text(result.stdout, encoding="utf-8")
        again = resolve(
            binary, root, "--config-file", str(selected), "--full", "--format", "json"
        )
        assert again.returncode == 0 and again.stderr == "", again
        assert json.loads(again.stdout) == json.loads(full.stdout)
        pruned = resolve(
            binary, root, "--config-file", str(selected), "--format", "json"
        )
        assert pruned.returncode == 0 and pruned.stderr == "", pruned
        settings = json.loads(pruned.stdout)
        assert settings["tool"]["instructions"] == ""
        fields = settings["tool"]["parameters"]
        assert fields["timeout_ms"] == {"enabled": False}
        assert fields["python"]["description"] == ""
        assert "cache" not in settings and "r" not in settings
        assert "r" not in fields and "sql" not in fields
    return [{"pruned_tool_round_trip": True, "empty_strings_preserved": True}]


def test_pruning_preserves_explicit_conditional_tool_prose(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = root / "selected.json"
        for mode in ((), ("--no-sandbox",)):
            original = resolve(
                binary, root, "--no-config", *mode, "--full", "--format", "json"
            )
            assert original.returncode == 0 and original.stderr == "", original
            defaults = json.loads(original.stdout)
            edited = json.loads(original.stdout)
            edited["tool"]["parameters"]["timeout_ms"]["enabled"] = False
            languages = {
                "languages": ["python"],
                "tool": {
                    "description": defaults["tool"]["description"],
                    "instructions": defaults["tool"]["instructions"],
                    "parameters": {
                        field: {
                            "description": defaults["tool"]["parameters"][field][
                                "description"
                            ]
                        }
                        for field in ("python", "stdin")
                    },
                },
            }
            cases = [("timeout", edited), ("languages", languages)]
            if not mode:
                cases.append(
                    ("permissions", defaults | {"sandbox": {"network": "enabled"}})
                )
            for name, source in cases:
                for output in ("yaml", "json"):
                    selected.write_text(json.dumps(source), encoding="utf-8")
                    expected = resolve(
                        binary,
                        root,
                        "--config-file",
                        str(selected),
                        *mode,
                        "--full",
                        "--format",
                        "json",
                    )
                    assert expected.returncode == 0 and expected.stderr == "", expected
                    pruned = resolve(
                        binary,
                        root,
                        "--config-file",
                        str(selected),
                        *mode,
                        "--prune",
                        "--format",
                        output,
                    )
                    assert pruned.returncode == 0 and pruned.stderr == "", pruned
                    selected.write_text(pruned.stdout, encoding="utf-8")
                    again = resolve(
                        binary,
                        root,
                        "--config-file",
                        str(selected),
                        *mode,
                        "--full",
                        "--format",
                        "json",
                    )
                    assert again.returncode == 0 and again.stderr == "", again
                    assert json.loads(again.stdout) == json.loads(expected.stdout), name
    return [{"explicit_tool_prose_preserved": ["timeout", "languages", "permissions"]}]


def test_rejects_conflicting_output_modes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        result = resolve(binary, Path(temporary), "--full", "--prune")
        assert result.returncode == 2 and result.stdout == "", result
        assert "cannot be used with" in result.stderr, result
    return [{"conflicting_output_modes_rejected": True}]


def test_materializes_without_starting_runtimes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(
            binary,
            root,
            "--no-config",
            "--full",
            "--format",
            "json",
            "-c",
            "languages=[python]",
            "-c",
            "r.executable=missing-R",
            "-c",
            "r.packages=[must-not-install]",
        )
        assert result.returncode == 0 and result.stderr == "", result
        settings = json.loads(result.stdout)
        assert settings["languages"] == ["python"]
        assert settings["r"]["executable"] == str(root / "missing-R")
        assert settings["r"]["packages"] == ["must-not-install"]
        assert settings["r"]["vanilla"] is False
        assert settings["r"]["resolution"] == "automatic"
        assert settings["cache"] == "console"
        tool = settings["tool"]
        assert tool["description"] and tool["instructions"]
        fields = tool["parameters"]
        assert "r" not in fields and "sql" not in fields
        assert fields["timeout_ms"]["enabled"] is True
        assert fields["timeout_ms"]["description"]
        assert "integer" in fields["timeout_ms"]["schema"]["type"]
        assert fields["requirements"]["parameters"]["action"]["schema"]["enum"]
        assert not list(root.iterdir()), list(root.iterdir())
    return [{"complete_tool": True, "runtime_started": False, "files_created": False}]


def test_exports_round_trip_and_preserve_edits(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        original = resolve(
            binary, root, "--no-config", "--no-sandbox", "--full", "--format", "json"
        )
        assert original.returncode == 0, original
        settings = json.loads(original.stdout)
        description = settings["tool"]["description"]
        settings["tool"]["instructions"] = "Find this workbench."
        parameters = settings["tool"]["parameters"]
        parameters["timeout_ms"]["enabled"] = False
        parameters["python"]["description"] = ""
        parameters["requirements"]["parameters"]["action"]["description"] = (
            "Choose an action."
        )
        selected = root / "selected.json"
        selected.write_text(json.dumps(settings), encoding="utf-8")
        home = root / "console-home"
        home.mkdir()
        (home / "config.yaml").write_text(
            "unknown: must-not-rediscover\n", encoding="utf-8"
        )
        exported = resolve(
            binary, root, "--config-file", str(selected), "--no-sandbox", "--full"
        )
        assert exported.returncode == 0 and exported.stderr == "", exported
        assert "parameters:" in exported.stdout, exported.stdout
        yaml = root / "selected.yaml"
        yaml.write_text(exported.stdout, encoding="utf-8")
        again = resolve(
            binary,
            root,
            "--config-file",
            str(yaml),
            "--no-sandbox",
            "--full",
            "--format",
            "json",
        )
        assert again.returncode == 0 and again.stderr == "", again
        assert json.loads(again.stdout) == settings
        with McpClient(
            binary,
            ("serve", "--no-sandbox", "--config-file", str(yaml)),
            current_directory=root,
            environment=os.environ | {"MCP_CONSOLE_HOME": str(home)},
        ) as client:
            client.initialize_and_list_tools()
            initialized = client.transcript[0]["result"]
            listed = client.transcript[-1]["result"]
            assert initialized["instructions"] == "Find this workbench."
            advertised = listed["tools"][0]
            assert advertised["description"] == description
            properties = advertised["inputSchema"]["properties"]
            assert "timeout_ms" not in properties
            assert properties["python"]["description"] == ""
            assert (
                properties["requirements"]["properties"]["action"]["description"]
                == "Choose an action."
            )
            hidden = client.send(timeout_ms=0)
            assert hidden["isError"], hidden
            transcript = client.finish()
    return transcript


def test_rejects_edits_to_parameter_types(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(binary, root, "--no-config", "--full", "--format", "json")
        assert result.returncode == 0, result
        settings = json.loads(result.stdout)
        settings["tool"]["parameters"]["timeout_ms"]["schema"]["type"] = "string"
        selected = root / "selected.json"
        selected.write_text(json.dumps(settings), encoding="utf-8")
        result = resolve(binary, root, "--config-file", str(selected))
        assert result.returncode == 1 and result.stdout == "", result
        assert "tool.parameters.timeout_ms.schema" in result.stderr
        assert "read-only" in result.stderr
    return [{"schema_edits_rejected": True}]


@requires(NON_UTF8_FILENAMES)
def test_non_utf8_executable_paths_return_errors(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        native = Path(os.fsdecode(os.fsencode(root) + b"/native-\xff"))
        native.mkdir()
        shutil.copy2(sys.executable, native / "relative-python")
        audit = []
        for parameter, path in (
            ("python.existing", "relative-python"),
            ("r.executable", "relative-R"),
        ):
            result = resolve(binary, native, "--no-config", "-c", f"{parameter}={path}")
            assert result.returncode == 1 and result.stdout == "", result
            assert f"cannot export {parameter}" in result.stderr, result
            assert "UTF-8" in result.stderr, result
            audit.append(
                {
                    "parameter": parameter,
                    "exit_code": result.returncode,
                    "stderr": result.stderr,
                }
            )
    return audit


def test_python_fallback_policies_survive_export(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        venv = root / "selected-venv"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", venv],
            check=True,
            capture_output=True,
        )
        fallback = {"existing": sys.executable}
        managed = {"managed": {"packages": [], "resolution": "startup_only"}}
        choices = {
            "existing": {
                "first_available": [{"existing": venv.name}, fallback, managed]
            },
            "active_venv": {"first_available": ["active_venv", fallback, managed]},
        }
        selected = root / "selected.json"
        exported = root / "exported.json"
        audit = []
        for name, policy in choices.items():
            selected.write_text(
                json.dumps(
                    {
                        "python": policy,
                        "languages": ["python"],
                        "r": {"packages": [], "resolution": "disabled"},
                    }
                ),
                encoding="utf-8",
            )
            result = resolve(
                binary,
                root,
                "--config-file",
                str(selected),
                "--no-sandbox",
                "--format",
                "json",
                environment=os.environ | {"VIRTUAL_ENV": str(venv)},
            )
            assert result.returncode == 0 and result.stderr == "", result
            settings = json.loads(result.stdout)
            assert settings["python"] == policy, settings["python"]
            exported.write_text(result.stdout, encoding="utf-8")
            retired = root / "retired-venv"
            venv.rename(retired)
            environment = dict(os.environ)
            environment.pop("VIRTUAL_ENV", None)
            environment["MCP_CONSOLE_HOME"] = str(root / "console-home")
            try:
                with McpClient(
                    binary,
                    ("serve", "--no-sandbox", "--config-file", str(exported)),
                    environment=environment,
                    current_directory=root,
                ) as client:
                    client.initialize_and_list_tools()
                    client.expect(
                        "fallback interpreter selected\n",
                        # fmt: python
                        python=code("""
                            import sys
                            from pathlib import Path

                            assert Path(sys.prefix) != Path.cwd() / "selected-venv"
                            assert Path(sys.prefix) != Path.cwd() / "retired-venv"
                            print("fallback interpreter selected")
                            """),
                    )
                    client.finish()
            finally:
                retired.rename(venv)
            audit.append(
                {"choice": name, "policy_preserved": True, "fallback_launched": True}
            )
    return audit


@requires(POSIX)
def test_permission_and_environment_exports_round_trip(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = root / "selected.json"
        for source in ({}, *CONFIGURATION_EXAMPLES.values()):
            source = source | {
                "environment": {"UNCHANGED": "", "LAYER": "worker"},
                "resolver": {"environment": {"LAYER": "resolver"}},
            }
            selected.write_text(json.dumps(source), encoding="utf-8")
            first = resolve(
                binary,
                root,
                "--config-file",
                str(selected),
                "--full",
                "--format",
                "json",
            )
            assert first.returncode == 0 and first.stderr == "", first
            settings = json.loads(first.stdout)
            assert settings["environment"] == source["environment"]
            assert settings["resolver"]["environment"] == {
                "UNCHANGED": "",
                "LAYER": "resolver",
            }
            if "filesystem" in source.get("sandbox", {}):
                assert (
                    settings["sandbox"]["filesystem"]
                    == {"read_only": [], "read_write": [], "deny": []}
                    | source["sandbox"]["filesystem"]
                )
            if source.get("sandbox", {}).get("profile") == "workspace":
                assert settings["sandbox"]["profile"] == "workspace", settings
                description = settings["tool"]["description"]
                assert "workspace" in description, description
            assert settings["sandbox"]["tempdir"] == source.get("sandbox", {}).get(
                "tempdir", "managed"
            ), settings
            selected.write_text(first.stdout, encoding="utf-8")
            again = resolve(
                binary,
                root,
                "--config-file",
                str(selected),
                "--full",
                "--format",
                "json",
            )
            assert again.returncode == 0 and again.stderr == "", again
            assert json.loads(again.stdout) == settings
    return [
        {
            "permission_round_trips": ["defaults", *CONFIGURATION_EXAMPLES],
            "environment_preserved": True,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
