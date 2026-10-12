#!/usr/bin/env python3
"""Present the CLI acceptance transcripts without running a Console binary."""

from html import escape
from pathlib import Path
import re
from typing import Any

from pandocfilters import Code, Div, Header, RawBlock, toJSONFilter
from yaml12 import read_yaml

SNAPSHOTS = (
    Path(__file__).resolve().parents[2] / "tests/snapshots/cli/interface/test_help"
)


def cli_help(
    key: str, value: Any, format: str, metadata: dict[str, Any]
) -> dict[str, Any] | None:
    if key != "Div" or "cli-help-transcript" not in value[0][1]:
        return None
    assert format == "html", "CLI help presentation requires HTML"
    attributes, _ = value
    platform, _, options = attributes
    entries = read_yaml(SNAPSHOTS / dict(options)["snapshot"], multi=True)
    blocks = []
    for entry in entries:
        command = entry["command"]
        if not command.endswith(" --help"):
            continue
        assert entry.get("exit_code", 0) == 0 and entry["stdout"], command
        assert not entry.get("stderr"), command
        name = command.removesuffix(" --help")
        identifier = platform + "-" + name.replace(" ", "-")
        blocks.append(Header(3, [identifier, [], []], [Code(["", [], []], name)]))
        output = re.sub(
            r"(?m)^([A-Z][\w ]+:)",
            r'<span class="cli-help-heading">\1</span>',
            escape(entry["stdout"]),
        )
        blocks.append(
            RawBlock(
                "html",
                f'<pre class="cli-help" data-command="{escape(command)}">'
                f"<code>{output}</code></pre>",
            )
        )
    assert blocks, "transcript contains no successful --help commands"
    return Div(attributes, blocks)


if __name__ == "__main__":
    toJSONFilter(cli_help)
