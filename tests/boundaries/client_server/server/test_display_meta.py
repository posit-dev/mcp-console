#!/usr/bin/env -S uv run --script
"""Every send result carries the standard server identity in _meta."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite

INFO = "io.modelcontextprotocol/serverInfo"
VENDOR = "posit.mcp-console/send"


def assert_identity(result) -> None:
    info = result["_meta"][INFO]
    assert info["name"] == "mcp-console", info
    assert isinstance(info["version"], str) and info["version"], info
    assert VENDOR not in result["_meta"], result["_meta"]


@requires(POSIX, R)
@executions(RUNTIME)
def test_send_results_carry_server_identity(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()

            noted = client.send(
                # fmt: r
                r=code("""
                    cat("hi\n")
                    """),
                note="Greeting from R\n\nsecond line stays intact",
            )
            assert_identity(noted)

            plain = client.send(r="6 * 7")
            assert_identity(plain)

            note_only = client.send(note="Standalone observation")
            assert note_only["content"] == [{"type": "text", "text": "[done]"}], (
                note_only
            )
            assert_identity(note_only)

            inspection = client.send(requirements={"action": "get"})
            assert_identity(inspection)

            poll = client.send()
            assert_identity(poll)

            # Language errors surface as text content, not protocol errors.
            failed = client.send(r='stop("boom")')
            assert any(
                "Error: boom" in part.get("text", "") for part in failed["content"]
            ), failed
            assert_identity(failed)

            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
