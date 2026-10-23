"""Launcher overrides for tool prose and accepted parameters."""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT
from support.presentation import without_descriptions
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.suites import run_this_suite


def _configure(root: Path, tool: dict) -> Path:
    path = root / ".agents/console/config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"languages": ["r", "python"], "tool": tool}), encoding="utf-8"
    )
    return path


def _descriptions(value: object, path: str = "") -> dict[str, str]:
    descriptions = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}" if path else key
            if key == "description":
                descriptions[child] = item
            else:
                descriptions.update(_descriptions(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            descriptions.update(_descriptions(item, f"{path}.{index}"))
    return descriptions


def test_replaces_complete_tool_prose(binary: Path) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        with McpClient(
            binary,
            DIRECT.serve("--worker", "unused-worker", "-c", "languages=[r,python]"),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            (baseline,) = client.transcript[-1]["result"]["tools"]
            assert (
                baseline["inputSchema"]["properties"]["timeout_ms"]["default"] == 60_000
            )
            baseline_transcript = client.finish()

        parameters = {
            field: {"description": f"Replacement for {field}."}
            for field in ("r", "python", "control", "requirements", "timeout_ms")
        }
        parameters["stdin"] = {"description": ""}
        parameters["requirements"]["parameters"] = {
            field: {"description": f"Replacement for requirements.{field}."}
            for field in (
                "action",
                "r",
                "python",
                "duckdb",
                "python_version",
                "exclude_newer",
            )
        }
        description = "Evaluation tool.\n\nUse the supplied parameter descriptions."
        _configure(
            root,
            {
                "description": description,
                "instructions": "Evaluation server.",
                "parameters": parameters,
            },
        )
        with McpClient(
            binary, DIRECT.serve("--worker", "unused-worker"), current_directory=root
        ) as client:
            client.initialize_and_list_tools()
            (tool,) = client.transcript[-1]["result"]["tools"]
            assert (
                client.transcript[0]["result"]["instructions"] == "Evaluation server."
            )
            assert without_descriptions(tool) == without_descriptions(baseline)
            expected = {"description": description}
            for field, settings in parameters.items():
                prefix = f"inputSchema.properties.{field}"
                expected[f"{prefix}.description"] = settings["description"]
                for nested, override in settings.get("parameters", {}).items():
                    expected[f"{prefix}.properties.{nested}.description"] = override[
                        "description"
                    ]
            assert _descriptions(tool) == expected
            assert client.request("tools/list")["result"]["tools"] == [tool]
            return TranscriptWithCompanions(
                client.finish(), {"baseline.yaml": McpTranscript(baseline_transcript)}
            )


def test_hides_parameters_and_keeps_omission_defaults(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        config = _configure(
            root,
            {
                "parameters": {
                    "timeout_ms": {"enabled": False},
                    "requirements": {"parameters": {"python": {"enabled": False}}},
                }
            },
        )
        environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="python")
        with McpClient(
            binary,
            DIRECT.serve("-c", "python=" + json.dumps(sys.executable)),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            (tool,) = client.transcript[-1]["result"]["tools"]
            properties = tool["inputSchema"]["properties"]
            assert "timeout_ms" not in properties
            assert "timeout_ms" not in tool["description"]
            assert "60 seconds" in tool["description"]
            assert "python" not in properties["requirements"]["properties"]
            assert properties["python"]["description"].startswith(
                "Evaluate one complete"
            )
            client.send(python="retained = 42\nprint(retained)")
            assert last_tool_text(client) == "42\n"
            for supplied in (
                {"timeout_ms": 0},
                {"timeout_ms": None},
                {"requirements": {"python": ["must-not-prepare"]}},
                {"requirements": {"python": None}},
            ):
                result = client.send(
                    python="retained = 0", control="restart", **supplied
                )
                assert result.get("isError"), result
                field = (
                    "timeout_ms" if "timeout_ms" in supplied else "requirements.python"
                )
                assert result["content"] == [
                    {
                        "type": "text",
                        "text": f"`{field}` is hidden by `tool.parameters`",
                    }
                ]
            client.send(python="print(retained)")
            assert last_tool_text(client) == "42\n"
            config.write_text("tool: {parameters: {timeout_ms: {enabled: true}}}\n")
            client.send(control="restart")
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            ), last_tool_text(client)
            client.send(python="print(6 * 7)")
            assert last_tool_text(client) == "42\n"
            assert client.request("tools/list")["result"]["tools"] == [tool]
            client.send()
            assert last_tool_text(client) == "\n[idle]", last_tool_text(client)
            return client.finish()


def test_invalid_tool_configuration_fails_launch(
    binary: Path,
) -> TranscriptWithCompanions:
    transcripts = {}
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        for name, tool, diagnostic in (
            (
                "unavailable-parameter",
                {"parameters": {"r": {"description": "Unavailable R cell."}}},
                "tool.parameters.r",
            ),
            (
                "unknown-parameter",
                {"parameters": {"missing": {"enabled": False}}},
                "tool.parameters.missing",
            ),
            (
                "unknown-nested-parameter",
                {
                    "parameters": {
                        "requirements": {
                            "parameters": {"missing": {"description": "unused"}}
                        }
                    }
                },
                "tool.parameters.requirements.parameters.missing",
            ),
            (
                "scalar-children",
                {
                    "parameters": {
                        "timeout_ms": {
                            "parameters": {"missing": {"description": "unused"}}
                        }
                    }
                },
                "tool.parameters.timeout_ms.parameters",
            ),
            ("invalid-description", {"description": 1}, "tool.description"),
            (
                "invalid-enabled",
                {"parameters": {"timeout_ms": {"enabled": "false"}}},
                "tool.parameters.timeout_ms.enabled",
            ),
            (
                "unknown-setting",
                {"parameters": {"timeout_ms": {"type": "string"}}},
                "tool.parameters.timeout_ms",
            ),
        ):
            result = subprocess.run(
                [
                    str(binary),
                    *DIRECT.serve(
                        "--no-config",
                        "--worker",
                        "unused-worker",
                        "-c",
                        "languages=[python]",
                        "-c",
                        "tool=" + json.dumps(tool),
                    ),
                ],
                cwd=root,
                input="",
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            assert result.returncode == 1 and not result.stdout, result
            assert diagnostic in result.stderr, result.stderr
            transcripts[f"{name}.yaml"] = [
                {"configuration": tool, "stderr": result.stderr}
            ]
    return TranscriptWithCompanions(
        transcripts.pop("unknown-parameter.yaml"), transcripts
    )


if __name__ == "__main__":
    run_this_suite(__file__)
