from dataclasses import dataclass
from typing import Any


TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]
ToolResult = dict[str, Any]
YamlStream = list[Any]


def without_meta(result: ToolResult) -> ToolResult:
    """Drop `_meta` where the server's identity stamp is incidental to the
    behavior under test. Identity coverage lives in
    client_server/server/test_display_meta."""
    return {key: value for key, value in result.items() if key != "_meta"}


@dataclass(frozen=True)
class McpTranscript:
    transcript: Transcript


@dataclass(frozen=True)
class TranscriptWithCompanions:
    transcript: Transcript
    companions: dict[str, McpTranscript | YamlStream | str]
