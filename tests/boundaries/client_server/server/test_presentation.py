#!/usr/bin/env -S uv run --script
"""Configured presentation stays independent of interpreter discovery."""

import difflib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED
from support.presentation import assert_tool_guidance, without_descriptions
from support.records import Transcript
from support.requirements import SQL
from support.suites import run_this_suite


LANGUAGES = ("r,python,sql", "r", "python", "sql", "r,python", "r,sql", "python,sql")
MISSING_UV = (
    "Python sessions without R require `uv` on PATH; "
    "set python in .agents/console/config.yaml to use an existing environment"
)
R_SCRIPT_GUIDANCE = (
    "For a reusable R script, include imports, data inputs, and "
    "`#| packages:`/`#| r-version:` metadata for `ir run script.R`, "
    "which starts without live Console objects."
)


def test_python_dependency_guidance_matches_selection(binary: Path) -> Transcript:
    records: Transcript = []
    cases = (
        ("default", None, None, "explicit"),
        ("launch managed", None, "managed", "explicit"),
        ("launch empty", None, "", "explicit"),
        ("launch existing", None, sys.executable, "existing"),
        ("configured existing", {"existing": sys.executable}, None, "existing"),
        ("managed explicit", {"managed": {}}, sys.executable, "explicit"),
        (
            "managed implicit",
            {"managed": {"resolution": "implicit"}},
            sys.executable,
            "implicit",
        ),
        (
            "managed fixed",
            {"managed": {"resolution": "fixed"}},
            None,
            "fixed",
        ),
        (
            "existing fallback",
            {
                "first_available": [
                    {"existing": sys.executable},
                    {"managed": {"resolution": "implicit"}},
                ]
            },
            None,
            "existing",
        ),
        (
            "managed fallback",
            {
                "first_available": [
                    {"existing": "missing-python"},
                    {"managed": {"resolution": "implicit"}},
                ]
            },
            sys.executable,
            "implicit",
        ),
    )
    for name, selection, launch_python, policy in cases:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            # Defer startup until send so discovery never depends on runtimes.
            config.write_text(
                json.dumps(
                    {
                        "languages": ["python"],
                        "python": selection,
                        "sandbox": {"profile": "inherit_codex"},
                    }
                )
            )
            environment = dict(os.environ, PATH="")
            for variable in ("R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV"):
                environment.pop(variable, None)
            if launch_python is not None:
                environment["RETICULATE_PYTHON"] = launch_python
            with McpClient(
                binary, SANDBOXED.serve(), environment, root, record_in_project=False
            ) as client:
                client.initialize_and_list_tools()
                tool = client.transcript[-1]["result"]["tools"][0]
                guidance = tool["inputSchema"]["properties"]["python"]["description"]
                assert (
                    "Declare dependencies with `requirements.python`" in guidance
                ) == (policy == "explicit"), (name, guidance)
                assert (
                    "prepare missing PyPI distributions on first import" in guidance
                ) == (policy == "implicit"), (name, guidance)
                assert ("Only startup dependencies are prepared" in guidance) == (
                    policy == "fixed"
                ), (name, guidance)
                assert (
                    "Use installed packages in the selected environment" in guidance
                ) == (policy == "existing"), (name, guidance)
                assert ("`requirements.python` is unavailable" in guidance) == (
                    policy == "existing"
                ), (name, guidance)
                if name == "default":
                    baseline = without_descriptions(tool)
                assert without_descriptions(tool) == baseline, name
                result = subprocess.run(
                    [binary, "config", "resolve", "--full", "--format", "json"],
                    cwd=root,
                    env=environment | {"MCP_CONSOLE_HOME": str(root / "console-home")},
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                assert result.returncode == 0 and result.stderr == "", result
                materialized = json.loads(result.stdout)
                assert (
                    materialized["tool"]["parameters"]["python"]["description"] is None
                )
                config.write_text(json.dumps(materialized))
                with McpClient(
                    binary,
                    SANDBOXED.serve(),
                    environment,
                    root,
                    record_in_project=False,
                ) as reloaded:
                    reloaded.initialize_and_list_tools()
                    assert reloaded.transcript[-1]["result"]["tools"] == [tool], name
                    reloaded.finish()
                # An edit affects the next launch, not this captured presentation.
                config.write_text(json.dumps({"python": {"managed": {}}}))
                assert client.request("tools/list")["result"]["tools"] == [tool]
                client.finish()
                records.append({"selection": name, "python_guidance": guidance})
    return records


def test_python_guidance_respects_requirement_visibility(binary: Path) -> Transcript:
    records: Transcript = []
    for visibility in ("visible", "python hidden", "requirements hidden"):
        for description in (None, "Custom Python guidance.", ""):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                config = root / ".agents/console/config.yaml"
                config.parent.mkdir(parents=True)
                parameters = {}
                if visibility == "python hidden":
                    parameters["requirements"] = {
                        "parameters": {"python": {"enabled": False}}
                    }
                elif visibility == "requirements hidden":
                    parameters["requirements"] = {"enabled": False}
                if description is not None:
                    parameters["python"] = {"description": description}
                config.write_text(
                    json.dumps(
                        {
                            "languages": ["python"],
                            "python": {"managed": {}},
                            "sandbox": {"profile": "inherit_codex"},
                            "tool": {"parameters": parameters},
                        }
                    )
                )
                environment = dict(os.environ, PATH="")
                with McpClient(
                    binary,
                    SANDBOXED.serve(),
                    environment,
                    root,
                    record_in_project=False,
                ) as client:
                    client.initialize_and_list_tools()
                    (tool,) = client.transcript[-1]["result"]["tools"]
                    properties = tool["inputSchema"]["properties"]
                    requirements = properties.get("requirements", {}).get(
                        "properties", {}
                    )
                    visible = visibility == "visible"
                    assert ("python" in requirements) == visible, tool
                    guidance = properties["python"]["description"]
                    if description is None:
                        assert (
                            "Declare dependencies with `requirements.python`"
                            in guidance
                        ) == visible, (visibility, guidance)
                        assert (
                            "Use installed packages in the selected environment"
                            in guidance
                        ) == (not visible), (visibility, guidance)
                    else:
                        assert guidance == description, (visibility, guidance)
                    client.finish()
                    records.append(
                        {
                            "visibility": visibility,
                            "description": description,
                            "python_guidance": guidance,
                        }
                    )
    return records


def test_builtin_configured_language_matrix(binary: Path) -> Transcript:
    return _configured_language_matrix(binary)


def _configured_language_matrix(binary: Path) -> Transcript:
    records: Transcript = []
    languages = LANGUAGES if SQL.available else ("r,python", "r", "python")
    for enabled in languages:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            environment = dict(os.environ, PATH="", MCP_CONSOLE_LANGUAGES=enabled)
            for name in ("R_HOME", "RETICULATE_PYTHON", "RETICULATE_UV"):
                environment.pop(name, None)
            with McpClient(
                binary, DIRECT.serve(), environment, root, record_in_project=False
            ) as client:
                client.initialize_and_list_tools()
                tool = client.transcript[-1]["result"]["tools"][0]
                assert client.request("ping")["result"] == {}
                properties = tool["inputSchema"]["properties"]
                fields = set(properties) & {"r", "python", "sql"}
                assert fields == set(enabled.split(",")), fields
                assert_tool_guidance(tool, fields, custom=False)
                description = tool["description"]
                instructions = client.transcript[0]["result"]["instructions"]
                paragraphs = description.split("\n\n")
                assert instructions == paragraphs[0]
                assert "\n" not in instructions and len(instructions) <= 250
                names = {
                    name for name in ("R", "Python", "SQL") if name.lower() in fields
                }
                assert set(re.findall(r"\b(?:R|Python|SQL)\b", instructions)) == names
                assert set(re.findall(r"\b(?:R|Python|SQL)\b", description)) == names
                assert (
                    set(re.findall(r"\b(?:R|Python|SQL)\b", json.dumps(properties)))
                    == names
                )
                cell_guidance = paragraphs[1].split(" cell per call", 1)[0]
                assert set(re.findall(r"`(r|python|sql)`", cell_guidance)) == fields
                assert "on Windows" not in description
                assert (
                    R_SCRIPT_GUIDANCE in properties.get("r", {}).get("description", "")
                ) == ("r" in fields)
                assert ("Choose whichever language makes each step" in description) == (
                    len(fields) > 1
                )
                for field, guidance in (
                    ("r", "Use R for vectorized data"),
                    ("python", "Use Python when its libraries"),
                    ("sql", "consider DuckDB SQL first"),
                ):
                    assert (guidance in description) == (
                        field in fields and not {"r", "python"} <= fields
                    )
                if enabled == languages[0]:
                    baseline = tool
                else:
                    expected = {
                        field: schema
                        for field, schema in baseline["inputSchema"][
                            "properties"
                        ].items()
                        if field not in {"r", "python", "sql"} or field in fields
                    }
                    expected_tool = {
                        **baseline,
                        "description": description,
                        "inputSchema": {
                            **baseline["inputSchema"],
                            "properties": expected,
                        },
                    }
                    expected_metadata = without_descriptions(expected_tool)
                    metadata = without_descriptions(tool)
                    assert metadata == expected_metadata, "\n".join(
                        difflib.unified_diff(
                            json.dumps(expected_metadata, indent=2).splitlines(),
                            json.dumps(metadata, indent=2).splitlines(),
                        )
                    )
                # Pin selected guidance; compare shared paragraphs and schema
                # metadata in full while descriptions follow visible fields.
                baseline_paragraphs = baseline["description"].split("\n\n")
                if "sql" in fields:
                    assert paragraphs[3:] == baseline_paragraphs[3:]
                else:
                    assert paragraphs[3:] == baseline_paragraphs[4:]
                guidance = paragraphs[2]
                records.append({"languages": enabled, "language_guidance": guidance})
                # Observe completed preparation before closing. Discovery stays
                # responsive even when eager startup cannot prepare a runtime.
                result = client.send(session="default", timeout_ms=10_000)
                assert result["isError"] is True, result
                assert client.request("tools/list")["result"]["tools"] == [tool]
                _, stderr = client.finish_with_standard_error(expected_exit_status=1)
                assert result == {
                    "content": [{"type": "text", "text": MISSING_UV}],
                    "isError": True,
                }, result
                assert stderr == MISSING_UV + "\n", stderr
                if enabled == languages[0]:
                    records[-1].update(preparation_error=result, stderr=stderr)
    return records


if __name__ == "__main__":
    run_this_suite(__file__)
