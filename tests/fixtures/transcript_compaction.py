"""Copied into a temporary CLI suite, with transcripts supplied by adjacent JSON."""

import json
from pathlib import Path

from support.records import McpTranscript, Transcript, TranscriptWithCompanions


def inputs() -> dict:
    return json.loads(Path(__file__).with_suffix(".json").read_text(encoding="utf-8"))


def test_initializes_and_lists_tools(binary: Path) -> TranscriptWithCompanions:
    data = inputs()
    return TranscriptWithCompanions(
        data["initialization"], data["initialization_companions"]
    )


def test_selected(binary: Path) -> Transcript | TranscriptWithCompanions:
    data = inputs()
    transcript = data["transcript"]
    if data["record_companions"]:
        return TranscriptWithCompanions(
            transcript,
            {
                "mcp.yaml": McpTranscript(transcript),
                "wire.yaml": [{"payload": transcript}],
            },
        )
    return transcript
