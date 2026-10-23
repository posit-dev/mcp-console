#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.suites import run_this_suite
from support.requirements import POSIX, SQL, requires

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_routes_send_over_sideband(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.send(r="echo hello")
    assert last_tool_text(client) == "zod: hello\n"
    client.send(python="echo precise 👩🏽‍💻")
    assert last_tool_text(client) == "zod python: precise 👩🏽‍💻\n"
    client.send(sql="echo two  spaces")
    assert last_tool_text(client) == "zod sql: two  spaces\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_projects_console_kinds(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    result = client.send(r="emit console kinds")
    assert result == {
        "content": [
            {
                "type": "text",
                "text": "zod output\nzod diagnostic\n",
            }
        ],
        "isError": False,
    }, result
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_returns_worker_images(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.send(r="emit image")
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {"type": "text", "text": "before image\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {"type": "text", "text": "after image\n"},
        ],
        "isError": False,
    }, result
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
