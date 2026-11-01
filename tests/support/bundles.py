"""Validate and normalize the public session-bundle discovery notice."""

import json
import re
from pathlib import Path
from typing import Any


def split_bundle_notice(text: str) -> tuple[str, str]:
    """Validate flattened SDK discovery text before comparing evaluation output."""
    prefix = "Session bundle on the Console host: "
    if not text.startswith(prefix):
        return "", text
    lines = text.split("\n", 4)
    assert len(lines) == 5, text
    bundle = Path(json.loads(lines[0].removeprefix(prefix)))
    assert bundle.is_absolute(), text
    assert re.fullmatch(r"session-\d{3,}", bundle.name), text
    assert (
        Path(json.loads(lines[1].removeprefix("Source: "))) == bundle / "session.qmd"
    ), text
    assert (
        Path(json.loads(lines[2].removeprefix("Report: "))) == bundle / "session.md"
    ), text
    assert (
        lines[3]
        == "This live bundle updates as results are assembled; the path grants no filesystem access."
    ), text
    return "\n".join(lines[:4]) + "\n", lines[4]


def evaluation_output(text: str) -> str:
    return split_bundle_notice(text)[1]


def normalize_bundle_locations(value: Any) -> Any:
    """Keep discovery visible in SDK snapshots; normalize only its host location."""
    if isinstance(value, str):
        if value.startswith('Session bundle on the Console host: "<'):
            return value  # Already normalized by the recording-specific case.
        notice, remainder = split_bundle_notice(value)
        if notice:
            directory = json.loads(
                notice.splitlines()[0].removeprefix(
                    "Session bundle on the Console host: "
                )
            )
            portable = f"<recording>/sessions/{Path(directory).name}"
            return (
                f'Session bundle on the Console host: "{portable}"\nSource: "{portable}/session.qmd"\nReport: "{portable}/session.md"\n'
                + notice.splitlines()[3]
                + "\n"
                + remainder
            )
        return value
    if isinstance(value, list):
        return [normalize_bundle_locations(item) for item in value]
    if isinstance(value, dict):
        return {key: normalize_bundle_locations(item) for key, item in value.items()}
    return value
