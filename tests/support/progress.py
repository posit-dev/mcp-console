"""Public running-response presentation and incidental duration normalization."""

import re

from support.records import ToolResult

RUNNING = "\n[running; poll with an empty send]"
ELAPSED = re.compile(
    r"\n\[elapsed: (\d+\.\d)s since admission(; no new output)?"
    r"(?:; phase: (startup|dependency preparation|replacement))?\]"
)
PHASE = re.compile(r"(?:\[|; )phase: (startup|dependency preparation|replacement)\]")


def phase_progress(text: str) -> str:
    matches = list(PHASE.finditer(text))
    assert len(matches) == 1, text
    return matches[0][1]


def elapsed_progress(text: str) -> tuple[float, bool]:
    matches = list(ELAPSED.finditer(text))
    assert len(matches) == 1 and text.endswith(RUNNING), text
    return float(matches[0][1]), matches[0][2] is not None


def without_elapsed(text: str) -> str:
    """Keep existing exact worker-output assertions separate from cell progress."""
    return ELAPSED.sub("", text)


def without_elapsed_result(result: ToolResult) -> ToolResult:
    return {
        **result,
        "content": [
            {**part, "text": without_elapsed(part["text"])}
            if part["type"] == "text"
            else part
            for part in result["content"]
        ],
    }


def normalize_elapsed(text: str) -> str:
    return ELAPSED.sub(
        lambda match: match[0].replace(match[1] + "s", "<elapsed>s", 1), text
    )
