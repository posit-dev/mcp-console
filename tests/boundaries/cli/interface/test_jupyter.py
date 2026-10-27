"""Public launch options select Jupyter without configuring the built-in worker."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli.interface.test_help import record
from support.client import McpClient
from support.execution import DIRECT
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def test_launch_options(binary: Path) -> Transcript:
    help = record(binary, "serve", "--help")
    for flag in (
        "--jupyter-kernel",
        "--jupyter-python",
        "--jupyter-transport",
    ):
        assert flag in help["stdout"], help
    entries = [help]
    for arguments in (
        ("--jupyter-python", "python3"),
        ("--jupyter-transport", "ipc"),
    ):
        entry = record(binary, "serve", *arguments)
        assert entry.get("exit_code") == 2, entry
        assert "required arguments were not provided" in entry["stderr"], entry
        assert "--jupyter-kernel <NAME>" in entry["stderr"], entry
        entries.append(entry)
    for setting in (
        "startup={code: '1', language: python}",
        "python={managed: {}}",
        "r={resolution: disabled}",
        "languages=[r]",
    ):
        entry = record(
            binary,
            "serve",
            "--no-config",
            "--no-sandbox",
            "--jupyter-kernel",
            "unused-kernel",
            "-c",
            setting,
        )
        assert entry.get("exit_code") == 1, entry
        assert "cannot be combined with --jupyter-kernel" in entry["stderr"], entry
        entries.append(entry)
    return entries


@requires(POSIX)
def test_jupyter_tool_configuration(binary: Path) -> Transcript:
    with McpClient(
        binary,
        DIRECT.serve(
            "--no-config",
            "--jupyter-kernel",
            "unused-kernel",
            "-c",
            "tool.parameters.code.description=Evaluate the selected Julia kernel.",
            "-c",
            "tool.parameters.requirements.parameters.action.schema={type: string, enum: [get]}",
        ),
    ) as client:
        client.initialize_and_list_tools()
        (tool,) = client.transcript[-1]["result"]["tools"]
        properties = tool["inputSchema"]["properties"]
        assert (
            properties["code"]["description"] == "Evaluate the selected Julia kernel."
        )
        assert properties["requirements"]["properties"]["action"]["enum"] == ["get"]
        return client.finish()


@requires(POSIX)
def test_jupyter_hides_code(binary: Path) -> Transcript:
    with McpClient(
        binary,
        DIRECT.serve(
            "--no-config",
            "--jupyter-kernel",
            "unused-kernel",
            "-c",
            "tool.parameters.code.enabled=false",
        ),
    ) as client:
        client.initialize_and_list_tools()
        (tool,) = client.transcript[-1]["result"]["tools"]
        assert "code" not in tool["inputSchema"]["properties"]
        result = client.send(code="must not start the unused kernel")
        assert result["isError"], result
        assert result["content"] == [
            {"type": "text", "text": "`code` is hidden by `tool.parameters`"}
        ], result
        return client.finish()


def test_jupyter_rejects_unavailable_tool_parameters(binary: Path) -> Transcript:
    entries = []
    for field in ("r", "sql", "requirements.parameters.python", "cod"):
        entry = record(
            binary,
            "serve",
            "--no-config",
            "--no-sandbox",
            "--jupyter-kernel",
            "unused-kernel",
            "-c",
            f"tool.parameters.{field}.description=Unavailable field.",
        )
        assert entry.get("exit_code") == 1, entry
        assert (
            f"tool.parameters.{field}: unknown or unavailable parameter"
            in entry["stderr"]
        ), entry
        entries.append(entry)
    entry = record(
        binary,
        "config",
        "resolve",
        "--no-config",
        "-c",
        "tool.parameters.code.description=Unavailable field.",
    )
    assert entry.get("exit_code") == 1, entry
    assert (
        "tool.parameters.code: unknown or unavailable parameter" in entry["stderr"]
    ), entry
    entries.append(entry)
    return entries


if __name__ == "__main__":
    run_this_suite(__file__)
