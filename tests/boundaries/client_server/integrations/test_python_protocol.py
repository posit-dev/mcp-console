#!/usr/bin/env -S uv run --script

import asyncio
import inspect
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "tests"))
sys.path.insert(0, str(ROOT / "python"))

from mcp_console import AsyncMCPConsole, ConsoleResult, MCPConsole
from support.execution import RUNTIME, Execution, executions
from support.records import McpTranscript, TranscriptWithCompanions
from support.suites import run_this_suite


@executions(RUNTIME)
def test_clients_discover_modern_protocol_and_declare_request_metadata(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    async def exercise(console: MCPConsole | AsyncMCPConsole) -> ConsoleResult:
        result = console.send(python="6 * 7")
        if inspect.isawaitable(result):
            result = await result
        assert result.text == "42\n", result
        return result

    async def run() -> TranscriptWithCompanions:
        transcripts = []
        version = subprocess.check_output([binary, "--version"], text=True).split()[1]
        with tempfile.TemporaryDirectory() as directory:
            for client_type in (MCPConsole, AsyncMCPConsole):
                capture = Path(directory) / f"{client_type.__name__}.json"
                console = client_type(
                    command=sys.executable,
                    args=[
                        "-u",
                        ROOT / "tests/fixtures/mcp_recording_proxy.py",
                        capture,
                        binary,
                        *execution.serve(),
                    ],
                )
                if client_type is AsyncMCPConsole:
                    async with console:
                        result = await exercise(console)
                else:
                    with console:
                        result = await exercise(console)
                transcript = json.loads(capture.read_text(encoding="utf-8"))
                assert [
                    "tools/call" if "send" in entry else entry["input"]["method"]
                    for entry in transcript
                ] == [
                    "server/discover",
                    "tools/list",
                    "tools/call",
                ], transcript
                assert "2026-07-28" in transcript[0]["result"]["supportedVersions"]
                for entry in transcript:
                    assert entry["input"]["params"]["_meta"] == {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "mcp",
                            "version": "0.1.0",
                        },
                        "io.modelcontextprotocol/clientCapabilities": {},
                    }, entry
                    identity = entry["result"]["_meta"][
                        "io.modelcontextprotocol/serverInfo"
                    ]
                    assert identity["name"] == "mcp-console", identity
                    assert identity["version"] == version, identity
                assert result.meta == transcript[-1]["result"]["_meta"]
                transcripts.append(transcript)
        synchronous, asynchronous = transcripts
        assert synchronous == asynchronous
        return TranscriptWithCompanions(
            synchronous, {"async.yaml": McpTranscript(asynchronous)}
        )

    return asyncio.run(run())


if __name__ == "__main__":
    run_this_suite(__file__)
