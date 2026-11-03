"""Capture editable configuration through public file selection and exports."""

import json
import os
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.records import Transcript
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


def resolve_adapter(
    binary: Path,
    directory: Path,
    *,
    files: tuple[str, ...] = (),
    optional_files: tuple[str, ...] = (),
    values: tuple[str, ...] = (),
    editable: bool = False,
    environment: dict[str, str] | None = None,
    full: bool = True,
) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            binary,
            "config",
            "resolve",
            "--no-sandbox",
            *(
                [argument for path in files for argument in ("--config-file", path)]
                if files
                else ["--no-config"]
            ),
            *[
                argument
                for path in optional_files
                for argument in ("--optional-config-file", path)
            ],
            *[argument for value in values for argument in ("-c", value)],
            "--format",
            "json",
            *(["--full"] if full else []),
            *(["--editable"] if editable else []),
        ],
        check=False,
        cwd=directory,
        env=(os.environ if environment is None else environment)
        | {"MCP_CONSOLE_HOME": str(directory / "console-home")},
        capture_output=True,
        text=True,
        timeout=15,
    )


def test_adapter_files_preserve_order_expansion_and_deduplication(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        home = root / "home"
        home.mkdir()
        (home / "global.yaml").write_text(
            "environment: {ORDER: global, GLOBAL: present}\n", encoding="utf-8"
        )
        (root / "project.yaml").write_text(
            "environment: {ORDER: project, PROJECT: present}\n", encoding="utf-8"
        )
        result = resolve_adapter(
            binary,
            root,
            files=(
                "missing-default.yaml",
                "~/global.yaml",
                "project.yaml",
                str(home / "global.yaml"),
            ),
            optional_files=("missing-default.yaml",),
            values=('environment.OVERRIDE="present"',),
            environment=os.environ | {"HOME": str(home)},
        )
        assert result.returncode == 0 and result.stderr == "", result
        environment = json.loads(result.stdout)["environment"]
        assert environment == {
            "GLOBAL": "present",
            "ORDER": "project",
            "PROJECT": "present",
            "OVERRIDE": "present",
        }, environment
    return [{"environment": environment, "missing_default_skipped": True}]


@platform_snapshots(
    "win32",
    reason="Missing required configuration sources report native filesystem errors",
)
def test_adapter_requires_only_selected_sources(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for required in ("global.yaml", "project.yaml"):
            result = resolve_adapter(
                binary,
                root,
                files=("global.yaml", "project.yaml"),
                optional_files=tuple(
                    path for path in ("global.yaml", "project.yaml") if path != required
                ),
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert f"cannot inspect '{required}':" in result.stderr, result
            records.append(
                {"required": required, "exit_code": 1, "stderr": result.stderr}
            )
    return records


def test_adapter_rejects_unselected_and_non_regular_required_sources(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "directory.yaml").mkdir()
        for files, optional_files, message in (
            (
                (),
                ("unselected.yaml",),
                "--optional-config-file 'unselected.yaml' must also be selected with --config-file",
            ),
            (
                ("directory.yaml",),
                (),
                "configuration 'directory.yaml' must be a regular file",
            ),
            (
                ("directory.yaml",),
                ("directory.yaml",),
                "configuration 'directory.yaml' must be a regular file",
            ),
        ):
            result = resolve_adapter(
                binary, root, files=files, optional_files=optional_files
            )
            assert result.returncode == 1 and result.stdout == "", result
            assert message in result.stderr, result
            records.append(
                {
                    "files": list(files),
                    "optional_files": list(optional_files),
                    "exit_code": 1,
                    "stderr": result.stderr,
                }
            )
    return records


def test_adapter_preserves_authored_tool_overrides_through_capture_and_edits(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        baseline = resolve_adapter(binary, root, values=('languages=["python"]',))
        assert baseline.returncode == 0 and baseline.stderr == "", baseline
        generated = json.loads(baseline.stdout)["tool"]
        authored = {
            "description": generated["description"],
            "instructions": "",
            "parameters": {
                "stdin": {"description": ""},
                "timeout_ms": {
                    "enabled": True,
                    "schema": generated["parameters"]["timeout_ms"]["schema"],
                },
            },
        }
        for name, tool in (
            ("absent", None),
            ("empty", {}),
            ("deferred", {"description": None, "instructions": None}),
            ("blank", {"description": "", "instructions": ""}),
            ("authored", authored),
        ):
            source = {"languages": ["python"]}
            values = []
            if tool is not None:
                source["tool"] = tool
                if tool:
                    source["tool"] = tool | {"instructions": "From the file."}
                    values.append(
                        f"tool.instructions={json.dumps(tool['instructions'])}"
                    )
            (root / "selected.json").write_text(json.dumps(source), encoding="utf-8")
            result = resolve_adapter(
                binary,
                root,
                files=("selected.json",),
                values=tuple(values),
                editable=True,
            )
            assert result.returncode == 0 and result.stderr == "", result
            captured = json.loads(result.stdout)
            assert "sessions" in captured and "resolver" in captured, captured
            if tool is None:
                assert "tool" not in captured, captured
            else:
                assert captured["tool"] == tool, captured["tool"]
            pruned = resolve_adapter(
                binary,
                root,
                files=("selected.json",),
                values=tuple(values),
                editable=True,
                full=False,
            )
            assert pruned.returncode == 0 and pruned.stderr == "", pruned
            assert json.loads(pruned.stdout).get("tool") == tool, pruned
            captured["languages"] = ["r"]
            (root / "edited.json").write_text(json.dumps(captured), encoding="utf-8")
            edited = resolve_adapter(binary, root, files=("edited.json",))
            assert edited.returncode == 0 and edited.stderr == "", edited
            materialized = json.loads(edited.stdout)["tool"]
            if name in ("authored", "blank"):
                assert materialized["description"] == tool["description"], materialized
                assert materialized["instructions"] == "", materialized
            else:
                assert materialized["description"] != generated["description"], (
                    materialized
                )
            if name == "authored":
                assert materialized["parameters"]["stdin"]["description"] == "", (
                    materialized
                )
                assert materialized["parameters"]["timeout_ms"]["enabled"], materialized
            assert "python" not in materialized["parameters"], materialized
            assert "r" in materialized["parameters"], materialized
            records.append(
                {"tool": name, "authorship_preserved": True, "language_edit": "r"}
            )
    return records


def test_adapter_preserved_tool_metadata_is_still_validated(binary: Path) -> Transcript:
    with TemporaryDirectory() as temporary:
        result = resolve_adapter(
            binary,
            Path(temporary).resolve(),
            editable=True,
            values=('tool.parameters.timeout_ms.schema={"type":"string"}',),
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert "tool.parameters.timeout_ms.schema is read-only" in result.stderr, result
    return [{"exit_code": 1, "stderr": result.stderr}]


def test_selected_files_preserve_null_resets_and_cli_precedence(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        (root / "inherited.json").write_text(
            json.dumps(
                {
                    "r": {"executable": "/inherited/R", "packages": ["stats"]},
                    "python": {"existing": ".venv"},
                    "sandbox": {"filesystem": {"deny": ["private"]}},
                    "resolver": {"sandbox": {"filesystem": {"deny": ["secret"]}}},
                    "tool": {"description": "Inherited description"},
                    "environment": {"ORDER": "inherited", "KEEP": "inherited"},
                }
            ),
            encoding="utf-8",
        )
        (root / "resets.json").write_text(
            json.dumps(
                {
                    "r": None,
                    "python": None,
                    "tool": None,
                    "sandbox": None,
                    "resolver": {"sandbox": None},
                }
            ),
            encoding="utf-8",
        )
        (root / "selected.json").write_text(
            json.dumps(
                {
                    "r": {"packages": []},
                    "python": {"packages": []},
                    "tool": {"description": ""},
                    "environment": {"ORDER": "selected"},
                }
            ),
            encoding="utf-8",
        )
        result = subprocess.run(
            [
                binary,
                "--config-file",
                "inherited.json",
                "--default-config",
                'r.executable="/default/R"',
                "-c",
                'environment.ORDER="first CLI override"',
                "config",
                "resolve",
                "--config-file",
                "resets.json",
                "--config-file",
                "selected.json",
                "-c",
                'environment.ORDER="last CLI override"',
                "--no-sandbox",
                "--full",
                "--editable",
                "--format",
                "json",
            ],
            cwd=root,
            env=os.environ | {"MCP_CONSOLE_HOME": str(root / "console-home")},
            capture_output=True,
            text=True,
            timeout=15,
        )
        assert result.returncode == 0 and result.stderr == "", result
        config = json.loads(result.stdout)
        assert config["r"].get("executable") is None, config["r"]
        assert config["r"]["packages"] == [], config["r"]
        assert "existing" not in config["python"], config["python"]
        assert config["python"]["managed"]["packages"] == [], config["python"]
        assert config["tool"] == {"description": ""}, config["tool"]
        assert config["environment"] == {
            "ORDER": "last CLI override",
            "KEEP": "inherited",
        }, config["environment"]
    return [{"ordered_files": True, "null_resets": True, "cli_precedence": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
