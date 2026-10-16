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
from support.public_configuration import (
    CONFIGURATION_EXAMPLES,
    MANAGED_TMPDIR_DESCRIPTION,
)
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


def test_managed_python_resolution_defaults_to_explicit(binary: Path) -> Transcript:
    configurations = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        for managed in ({}, {"packages": []}, {"resolution": "implicit"}):
            config.write_text(json.dumps({"python": {"managed": managed}}))
            result = resolve(
                binary,
                root,
                "--full",
                "--format",
                "json",
            )
            assert result.returncode == 0 and result.stderr == "", result
            settings = json.loads(result.stdout)
            expected = {"resolution": "explicit", **managed}
            assert settings["python"] == {"managed": expected}, settings["python"]
            assert settings["r"]["resolution"] == "implicit", settings["r"]
            configurations.append(settings["python"])
    return configurations


def test_managed_fallback_resolution_is_materialized(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        selected = root / "selected.json"
        for name, existing in (
            ("existing", sys.executable),
            ("managed", str(root / "missing-python")),
        ):
            for managed in (
                {},
                {"packages": [], "version": ">=3.12,<3.14"},
                {"resolution": "implicit"},
                {"resolution": "fixed"},
            ):
                candidates = [{"existing": existing}, {"managed": managed}]
                selected.write_text(
                    json.dumps({"python": {"first_available": candidates}}),
                    encoding="utf-8",
                )
                result = resolve(
                    binary,
                    root,
                    "--config-file",
                    str(selected),
                    "--full",
                    "--format",
                    "json",
                )
                assert result.returncode == 0 and not result.stderr, result
                expected = {"resolution": "explicit", **managed}
                python = json.loads(result.stdout)["python"]
                assert python == {
                    "first_available": [candidates[0], {"managed": expected}]
                }, python
                records.append({"selected": name, "managed": expected})
    return records


def test_exports_regenerate_python_guidance_after_reselection(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        executable = root / Path(sys.executable).name
        selected = root / "selected.json"
        exported = root / "exported.json"
        environment = os.environ | {"PATH": ""}
        environment.pop("RETICULATE_PYTHON", None)
        for name, starts_existing, next_existing, guidance in (
            (
                "environment",
                True,
                False,
                "Declare dependencies with `requirements.python`",
            ),
            ("fallback_missing", True, False, "Import packages normally"),
            ("fallback_present", False, True, "Use installed packages"),
        ):
            for full in (False, True):
                for override, description in (
                    (None, "generated"),
                    ("User-supplied Python guidance.", "custom"),
                    ("", "empty"),
                ):
                    source = {
                        "languages": ["python"],
                        "sandbox": {"profile": "inherit_codex"},
                    }
                    launch_environment = environment.copy()
                    if name == "environment":
                        launch_environment["RETICULATE_PYTHON"] = sys.executable
                    else:
                        executable.unlink(missing_ok=True)
                        if starts_existing:
                            shutil.copy2(sys.executable, executable)
                        source["python"] = {
                            "first_available": [
                                {"existing": str(executable)},
                                {
                                    "managed": {
                                        "resolution": "implicit",
                                        "packages": [],
                                    }
                                },
                            ]
                        }
                    if override is not None:
                        source["tool"] = {
                            "parameters": {"python": {"description": override}}
                        }
                    selected.write_text(json.dumps(source), encoding="utf-8")
                    result = resolve(
                        binary,
                        root,
                        "--config-file",
                        str(selected),
                        "--full" if full else "--prune",
                        "--format",
                        "json",
                        environment=launch_environment,
                    )
                    assert result.returncode == 0 and not result.stderr, result
                    exported.write_text(result.stdout, encoding="utf-8")
                    if name != "environment":
                        executable.unlink(missing_ok=True)
                        if next_existing:
                            shutil.copy2(sys.executable, executable)
                    with McpClient(
                        binary,
                        ("serve", "--config-file", str(exported)),
                        environment | {"MCP_CONSOLE_HOME": str(root / "console-home")},
                        root,
                        record_in_project=False,
                    ) as client:
                        client.initialize_and_list_tools()
                        tool = client.transcript[-1]["result"]["tools"][0]
                        actual = tool["inputSchema"]["properties"]["python"][
                            "description"
                        ]
                        if override is None:
                            assert guidance in actual, (name, full, actual)
                        else:
                            assert actual == override, (name, full, actual)
                        client.finish()
                    records.append(
                        {
                            "selection": name,
                            "full": full,
                            "description": description,
                        }
                    )
    return records


def test_broad_workspace_roots_preserve_selection_policy(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        project = home / "project"
        project.mkdir(parents=True)
        environment = os.environ | {"HOME": str(home)}
        for name, launch in (
            ("root", Path(home.anchor)),
            ("home", home),
            ("home_parent", root),
            ("project", project),
        ):
            for profile in ("workspace", {"inherit_codex": {"fallback": "workspace"}}):
                for full in (False, True):
                    result = resolve(
                        binary,
                        launch,
                        "--no-config",
                        "--format",
                        "json",
                        *("--full",) if full else (),
                        "-c",
                        "sandbox.profile=" + json.dumps(profile),
                        "-c",
                        "sandbox.filesystem.read_write=[.]",
                        environment=environment,
                    )
                    assert result.returncode == 0 and result.stderr == "", result
                    settings = json.loads(result.stdout)
                    assert settings["sandbox"]["profile"] == profile, settings
                    assert settings["sandbox"]["filesystem"]["read_write"] == ["."], (
                        settings
                    )
                    if full:
                        description = settings["tool"]["description"]
                        if profile == "workspace":
                            assert (
                                f"{MANAGED_TMPDIR_DESCRIPTION}, workspace,"
                                in description
                            ), description
                            assert json.dumps(str(launch)) in description, description
                        else:
                            assert "The sandbox base comes from" in description, (
                                description
                            )
                    records.append(
                        {
                            "launch": name,
                            "profile": profile,
                            "full": full,
                            "stderr": result.stderr,
                        }
                    )
    return records


def test_client_sandbox_inheritance_materializes_and_prunes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(binary, root, "--no-config", "--full", "--format", "json")
        assert result.returncode == 0 and result.stderr == "", result
        assert "inherit_from_codex" not in json.loads(result.stdout)["sandbox"]
        selected = root / "config.json"
        profiles = (
            "inherit_codex",
            {"inherit_codex": {}},
            {"inherit_codex": {"fallback": "read_only"}},
            {"inherit_codex": {"fallback": "workspace"}},
        )
        for profile in profiles:
            result = resolve(
                binary,
                root,
                "--no-config",
                "--format",
                "json",
                "-c",
                "sandbox.profile=" + json.dumps(profile),
            )
            assert result.returncode == 0 and result.stderr == "", result
            enabled = json.loads(result.stdout)
            assert enabled == {"sandbox": {"profile": profile}}, enabled
            selected.write_text(json.dumps(enabled), encoding="utf-8")
            result = resolve(
                binary,
                root,
                "--config-file",
                str(selected),
                "--full",
                "--format",
                "json",
            )
            assert result.returncode == 0 and result.stderr == "", result
            full = json.loads(result.stdout)
            assert full["sandbox"]["profile"] == profile, full
            assert "network" not in full["sandbox"], full
            assert full["cache"] is None, full
            assert "Codex" in full["tool"]["description"]
            selected.write_text(result.stdout, encoding="utf-8")
            result = resolve(
                binary, root, "--config-file", str(selected), "--format", "json"
            )
            assert result.returncode == 0 and json.loads(result.stdout) == enabled, (
                result
            )
            result = resolve(
                binary,
                root,
                "--no-config",
                "--format",
                "json",
                "-c",
                "sandbox.profile=" + json.dumps(profile),
                "-c",
                "sandbox.network=restricted",
            )
            expected = {"sandbox": {"profile": profile, "network": "restricted"}}
            assert result.returncode == 0 and json.loads(result.stdout) == expected, (
                result
            )
            result = resolve(
                binary,
                root,
                "--no-config",
                "--format",
                "json",
                "-c",
                "sandbox.profile=" + json.dumps(profile),
                "-c",
                "cache=console",
            )
            assert result.returncode == 0 and json.loads(result.stdout) == {
                "sandbox": {"profile": profile},
                "cache": "console",
            }, result
        for profile in (
            "codex",
            {"codex": {}},
            {"codex": {"fallback": "workspace"}},
            True,
            [],
            {},
            {"first_available": ["inherit_codex", "workspace"]},
            {"first_available": ["inherit_codex", "workspace"], "extra": True},
            {"workspace": {}},
            {"inherit_codex": True},
            {"inherit_codex": []},
            {"inherit_codex": {"fallback": "future"}},
            {"inherit_codex": {"fallback": "inherit_codex"}},
            {
                "inherit_codex": {
                    "fallback": {"inherit_codex": {"fallback": "workspace"}}
                }
            },
            {"inherit_codex": {"fallback": "workspace", "extra": True}},
            {"inherit_codex": {"fallback": "workspace"}, "extra": True},
        ):
            result = resolve(
                binary,
                root,
                "--no-config",
                "-c",
                "sandbox.profile=" + json.dumps(profile),
            )
            assert (
                result.returncode == 1
                and "profile" in result.stderr
                and not result.stdout
            ), result
        result = resolve(
            binary, root, "--no-config", "-c", "sandbox.inherit_from_codex=true"
        )
        assert (
            result.returncode == 1
            and "unknown field `inherit_from_codex`" in result.stderr
        ), result
    return [{"profiles": list(profiles)}]


def test_prunes_builtin_defaults_by_default(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        defaults = {
            "languages": ["r", "python", "sql"],
            "r": {"vanilla": False, "resolution": "implicit"},
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
            json.dumps({"r": {"vanilla": True, "resolution": "fixed"}}),
            encoding="utf-8",
        )
        project = root / ".agents" / "console"
        project.mkdir(parents=True)
        (project / "config.yaml").write_text(
            json.dumps({"r": {"resolution": "implicit"}}), encoding="utf-8"
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
            "python": {"managed": {"packages": [], "resolution": "explicit"}},
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
        assert settings["r"]["resolution"] == "implicit"
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
        managed = {"managed": {"packages": [], "resolution": "fixed"}}
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
                "environment": {
                    "UNCHANGED": "",
                    "LAYER": "worker",
                    "case_sensitive": "lower",
                    "CASE_SENSITIVE": "upper",
                },
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
                "case_sensitive": "lower",
                "CASE_SENSITIVE": "upper",
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
