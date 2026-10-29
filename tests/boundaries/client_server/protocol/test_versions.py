#!/usr/bin/env -S uv run --script

import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.records import Transcript
from support.suites import run_this_suite


def request_lifecycle(
    binary: Path, execution: Execution, method: str, **params: object
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.request(method, **params)
    # Deliberately omit notifications/initialized: rmcp's initialize response
    # already completes SDK startup; discovery remains a stateless exchange.
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_negotiates_legacy_and_discovers_modern_versions(
    binary: Path, execution: Execution
) -> Transcript:
    legacy = request_lifecycle(
        binary,
        execution,
        "initialize",
        protocolVersion="2025-06-18",
        capabilities={},
        clientInfo={
            "name": "protocol-test",
            "version": "1.0.0",
        },
    )
    modern = request_lifecycle(
        binary,
        execution,
        "server/discover",
        _meta={
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientInfo": {
                "name": "protocol-test",
                "version": "1.0.0",
            },
            "io.modelcontextprotocol/clientCapabilities": {},
        },
    )
    version = subprocess.check_output([binary, "--version"], text=True).split()[1]
    assert legacy[0]["result"]["serverInfo"]["version"] == version
    assert (
        modern[0]["result"]["_meta"]["io.modelcontextprotocol/serverInfo"]["version"]
        == version
    )
    return legacy + modern


if __name__ == "__main__":
    run_this_suite(__file__)
