#!/usr/bin/env python3
"""Include the generated starter configuration from CLI acceptance output."""

from pathlib import Path
from typing import Any

from pandocfilters import CodeBlock, Div, toJSONFilter
from yaml12 import read_yaml

SNAPSHOT = (
    Path(__file__).resolve().parents[2]
    / "tests/snapshots/cli/interface/test_config_init/dry_run_previews_the_created_contents.yaml"
)


def config_init(
    key: str, value: Any, format: str, metadata: dict[str, Any]
) -> dict[str, Any] | None:
    if key != "Div" or "config-init-template" not in value[0][1]:
        return None
    attributes, _ = value
    (created,) = (
        entry["created"]
        for entry in read_yaml(SNAPSHOT, multi=True)
        if "created" in entry
    )
    header, separator, contents = created.partition("\n\n")
    assert header.startswith("Created configuration: ") and separator and contents
    return Div(
        attributes,
        [CodeBlock(["", ["yaml"], [["filename", "config.yaml"]]], contents)],
    )


if __name__ == "__main__":
    toJSONFilter(config_init)
