from dataclasses import dataclass
from typing import Any


TranscriptEntry = dict[str, Any]
Transcript = list[TranscriptEntry]
ToolResult = dict[str, Any]
YamlStream = list[Any]


@dataclass(frozen=True)
class McpTranscript:
    transcript: Transcript


@dataclass(frozen=True)
class TranscriptWithCompanions:
    transcript: Transcript
    companions: dict[str, McpTranscript | YamlStream | str]
