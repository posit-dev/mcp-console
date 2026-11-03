"""Capture editable R adapter configuration through the native process boundary."""

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
    payload: dict,
    *,
    environment: dict[str, str] | None = None,
    full: bool = True,
) -> subprocess.CompletedProcess:
    path = directory / "adapter.json"
    path.write_text(json.dumps({"files": [], "values": []} | payload), encoding="utf-8")
    return subprocess.run(
        [
            binary,
            "config",
            "resolve",
            "--no-config",
            "--no-sandbox",
            "--config-overrides-file",
            str(path),
            "--format",
            "json",
            *(["--full"] if full else []),
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
        payload = {
            "files": [
                "missing-default.yaml",
                "~/global.yaml",
                "project.yaml",
                str(home / "global.yaml"),
            ],
            "required_files": ["~/global.yaml", "project.yaml"],
            "values": ['environment.OVERRIDE="present"'],
        }
        result = resolve_adapter(
            binary, root, payload, environment=os.environ | {"HOME": str(home)}
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
    "win32", reason="Missing required adapter sources report native filesystem errors"
)
def test_adapter_requires_only_selected_sources(binary: Path) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        for required in ("global.yaml", "project.yaml"):
            result = resolve_adapter(
                binary,
                root,
                {
                    "files": ["global.yaml", "project.yaml"],
                    "required_files": [required],
                },
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
        for payload, message in (
            (
                {"required_files": ["unselected.yaml"]},
                "required adapter configuration 'unselected.yaml' must be included in files",
            ),
            (
                {"files": ["directory.yaml"]},
                "configuration 'directory.yaml' must be a regular file",
            ),
            (
                {
                    "files": ["directory.yaml"],
                    "required_files": ["directory.yaml"],
                },
                "configuration 'directory.yaml' must be a regular file",
            ),
        ):
            result = resolve_adapter(binary, root, payload)
            assert result.returncode == 1 and result.stdout == "", result
            assert message in result.stderr, result
            records.append(
                {"payload": payload, "exit_code": 1, "stderr": result.stderr}
            )
    return records


def test_adapter_preserves_authored_tool_overrides_through_capture_and_edits(
    binary: Path,
) -> Transcript:
    records = []
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        baseline = resolve_adapter(binary, root, {"values": ['languages=["python"]']})
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
            payload = {
                "files": ["selected.json"],
                "required_files": ["selected.json"],
                "values": values,
                "preserve_tool_overrides": True,
            }
            result = resolve_adapter(binary, root, payload)
            assert result.returncode == 0 and result.stderr == "", result
            captured = json.loads(result.stdout)
            assert "sessions" in captured and "resolver" in captured, captured
            if tool is None:
                assert "tool" not in captured, captured
            else:
                assert captured["tool"] == tool, captured["tool"]
            pruned = resolve_adapter(binary, root, payload, full=False)
            assert pruned.returncode == 0 and pruned.stderr == "", pruned
            assert json.loads(pruned.stdout).get("tool") == tool, pruned
            captured["languages"] = ["r"]
            edited = resolve_adapter(
                binary,
                root,
                {
                    "values": [
                        f"{key}={json.dumps(value)}" for key, value in captured.items()
                    ]
                },
            )
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
            {
                "preserve_tool_overrides": True,
                "values": ['tool.parameters.timeout_ms.schema={"type":"string"}'],
            },
        )
        assert result.returncode == 1 and result.stdout == "", result
        assert "tool.parameters.timeout_ms.schema is read-only" in result.stderr, result
    return [{"exit_code": 1, "stderr": result.stderr}]


if __name__ == "__main__":
    run_this_suite(__file__)
