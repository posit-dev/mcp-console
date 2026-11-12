#!/usr/bin/env -S uv run --script

import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT
from support.previews import live_recording_events, session_directory
from support.records import Transcript
from support.requirements import R, WORKER, requires
from support.suites import run_this_suite


PROTOCOL_VERSION = "2026-07-28"
SERVER_INFO = "io.modelcontextprotocol/serverInfo"
RUNTIME_META = "io.github.t-kalinowski.mcp-console/warmup-runtime"


def request_metadata(version: str = PROTOCOL_VERSION) -> dict[str, Any]:
    return {
        "io.modelcontextprotocol/protocolVersion": version,
        "io.modelcontextprotocol/clientInfo": {
            "name": "mcp",
            "version": "0.1.0",
        },
        "io.modelcontextprotocol/clientCapabilities": {},
    }


def server_identity(binary: Path) -> dict[str, str]:
    version = subprocess.check_output([binary, "--version"], text=True).strip()
    return {"name": "mcp-console", "version": version.removeprefix("mcp-console ")}


@requires(R, WORKER)
def test_modern_discovery_and_tool_results_identify_server(binary: Path) -> Transcript:
    identity = server_identity(binary)
    with McpClient(binary, DIRECT.serve()) as client:
        discovered = client.request("server/discover", _meta=request_metadata())[
            "result"
        ]
        assert PROTOCOL_VERSION in discovered["supportedVersions"], discovered
        assert discovered["_meta"][SERVER_INFO] == identity, discovered
        assert discovered["resultType"] == "complete", discovered

        listed = client.request("tools/list", _meta=request_metadata())["result"]
        assert listed["_meta"][SERVER_INFO] == identity, listed
        assert listed["resultType"] == "complete", listed
        assert listed["ttlMs"] == 0 and listed["cacheScope"] == "public", listed
        assert [tool["name"] for tool in listed["tools"]] == ["send"], listed

        result = client.request(
            "tools/call",
            name="send",
            arguments={"python": "print('modern client')"},
            _meta={**request_metadata(), RUNTIME_META: True},
        )["result"]
        assert result["content"] == [{"type": "text", "text": "modern client\n"}], (
            result
        )
        assert result["resultType"] == "complete" and not result["isError"], result
        assert result["_meta"][SERVER_INFO] == identity, result
        assert isinstance(result["_meta"][RUNTIME_META]["r"], bool), result

        failed = client.request(
            "tools/call",
            name="send",
            arguments={"timeout_ms": -1},
            _meta=request_metadata(),
        )["result"]
        assert failed["isError"] and failed["resultType"] == "complete", failed
        assert failed["_meta"][SERVER_INFO] == identity, failed
        recorded = [
            event["result"]
            for event in live_recording_events(session_directory(client))
            if event["event"] == "tool_result"
        ]
        assert [entry["_meta"] for entry in recorded] == [
            result["_meta"],
            failed["_meta"],
        ], recorded
        return client.finish()


def test_modern_requests_work_without_discovery_and_validate_metadata(
    binary: Path,
) -> Transcript:
    identity = server_identity(binary)
    with McpClient(binary, DIRECT.serve()) as client:
        listed = client.request("tools/list", _meta=request_metadata())["result"]
        assert listed["_meta"][SERVER_INFO] == identity, listed
        assert listed["resultType"] == "complete", listed

        for missing in (
            "io.modelcontextprotocol/protocolVersion",
            "io.modelcontextprotocol/clientCapabilities",
        ):
            metadata = request_metadata()
            del metadata[missing]
            rejected = client.request("tools/list", _meta=metadata)["error"]
            assert rejected["code"] == -32602, rejected
            assert missing in rejected["message"], rejected

        unsupported = client.request(
            "tools/list", _meta=request_metadata("2099-01-01")
        )["error"]
        assert unsupported["code"] == -32022, unsupported
        assert unsupported["data"]["requested"] == "2099-01-01", unsupported
        assert PROTOCOL_VERSION in unsupported["data"]["supported"], unsupported

        recovered = client.request(
            "tools/call",
            name="send",
            arguments={"note": "direct call after version errors"},
            _meta=request_metadata(),
        )["result"]
        assert recovered == {
            "resultType": "complete",
            "content": [{"type": "text", "text": "[done]"}],
            "isError": False,
            "_meta": {SERVER_INFO: identity},
        }, recovered
        return client.finish()


def test_legacy_clients_keep_initialization_and_result_shapes(
    binary: Path,
) -> Transcript:
    with McpClient(binary, DIRECT.serve()) as client:
        client.initialize_and_list_tools()
        initialized = client.transcript[0]["result"]
        assert initialized["protocolVersion"] == "2025-11-25", initialized
        listed = client.transcript[-1]["result"]
        assert set(listed) == {"tools"}, listed
        assert client.request("ping")["result"] == {}
        result = client.send(note="legacy request")
        assert result == {
            "content": [{"type": "text", "text": "[done]"}],
            "isError": False,
        }, result
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
