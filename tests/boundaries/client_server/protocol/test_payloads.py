#!/usr/bin/env -S uv run --script

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_accepts_long_multibyte_source_lines(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    long_value = "é" * 100_000
    assert len(long_value) == 100_000
    assert len(long_value.encode()) == 200_000
    source = f'long_line_value <- "{long_value}"\nEncoding(long_line_value) <- "UTF-8"\nnchar(long_line_value)'
    client.send(r=source)
    assert last_tool_text(client) == "[1] 100000\n", last_tool_text(client)
    wire_message = client._last_serialized_message
    assert wire_message is not None
    assert long_value in wire_message
    assert "\\u00e9" not in wire_message

    recorded_source = client.transcript[-1]["send"]
    assert isinstance(recorded_source, dict)
    recorded_source["r"] = "<100000 multibyte characters on one source line>"
    client.transcript[-1]["transcript_normalization"] = {
        "target": "send.r",
        "repeated_character": "é",
        "repeated_character_count": 100_000,
        "source_utf8_bytes": len(source.encode()),
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
    }
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
