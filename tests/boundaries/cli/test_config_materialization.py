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


def test_materializes_without_starting_runtimes(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        result = resolve(
            binary,
            root,
            "--no-config",
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
            binary, root, "--no-config", "--no-sandbox", "--format", "json"
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
        exported = resolve(binary, root, "--config-file", str(selected), "--no-sandbox")
        assert exported.returncode == 0 and exported.stderr == "", exported
        assert "parameters:" in exported.stdout, exported.stdout
        yaml = root / "selected.yaml"
        yaml.write_text(exported.stdout, encoding="utf-8")
        again = resolve(
            binary, root, "--config-file", str(yaml), "--no-sandbox", "--format", "json"
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
        result = resolve(binary, root, "--no-config", "--format", "json")
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
                binary, root, "--config-file", str(selected), "--format", "json"
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
            selected.write_text(first.stdout, encoding="utf-8")
            again = resolve(
                binary, root, "--config-file", str(selected), "--format", "json"
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
