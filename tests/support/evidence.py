"""Lossless snapshot notation for explicitly selected synthetic repetitions."""

import re


def compact_text(text: str, *units: str) -> str | dict:
    """Call after checking the full response; retain every byte in repeat notation."""
    assert units and all(units)
    pattern = re.compile("|".join(f"(?:{re.escape(unit)}){{16,}}" for unit in units))
    parts: list[str | dict] = []
    start = 0
    for match in pattern.finditer(text):
        if match.start() > start:
            parts.append(text[start : match.start()])
        repeated = match[0]
        unit = next(
            unit for unit in units if repeated == unit * (len(repeated) // len(unit))
        )
        parts.append({"repeat": unit, "count": len(repeated) // len(unit)})
        start = match.end()
    if not parts:
        return text
    if start < len(text):
        parts.append(text[start:])
    assert (
        "".join(
            part if isinstance(part, str) else part["repeat"] * part["count"]
            for part in parts
        )
        == text
    )
    return {"text_bytes": len(text.encode()), "concat": parts}
