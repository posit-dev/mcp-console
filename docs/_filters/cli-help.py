#!/usr/bin/env python3
"""Present public CLI acceptance help without running a Console binary."""

from html import escape
from pathlib import Path
import re
from typing import Any

from pandocfilters import Code, Header, RawBlock, toJSONFilter
from yaml12 import read_yaml

SNAPSHOTS = (
    Path(__file__).resolve().parents[2] / "tests/snapshots/cli/interface/test_help"
)

# Acceptance also exercises internal entry points. They do not belong in the
# public command reference; add new public commands here deliberately.
# Display order is independent of transcript recording order.
PUBLIC_HELP_COMMANDS = (
    "mcp-console --help",
    "mcp-console serve --help",
    "mcp-console warmup --help",
    "mcp-console sandbox --help",
    "mcp-console config --help",
    "mcp-console config init --help",
    "mcp-console config resolve --help",
    "mcp-console configure-client --help",
)


def cli_help(
    key: str, value: Any, format: str, metadata: dict[str, Any]
) -> list[dict[str, Any]] | None:
    if key != "Div" or "cli-help-transcript" not in value[0][1]:
        return None
    assert format == "html", "CLI help presentation requires HTML"
    attributes, _ = value
    platform, _, options = attributes
    entries = {
        entry["command"]: entry
        for entry in read_yaml(SNAPSHOTS / dict(options)["snapshot"], multi=True)
    }
    blocks = []
    for command in PUBLIC_HELP_COMMANDS:
        entry = entries[command]
        assert entry.get("exit_code", 0) == 0 and entry["stdout"], command
        assert not entry.get("stderr"), command
        name = command.removesuffix(" --help")
        identifier = platform + "-" + name.replace(" ", "-")
        blocks.append(
            Header(
                3,
                [identifier, [], []],
                [Code(["", [], []], name)],
            )
        )
        output = re.sub(
            r"(?m)^([A-Z][\w ]+:)",
            r'<span class="cli-help-heading">\1</span>',
            escape(entry["stdout"]),
        )
        blocks.append(
            RawBlock(
                "html",
                f'<pre class="cli-help" data-command="{escape(command)}" '
                f'tabindex="0" role="region" aria-label="{escape(name)}">'
                f"<code>{output}</code></pre>",
            )
        )
    assert blocks, "transcript contains no successful public --help commands"
    # Pandoc omits headings grouped inside a Div from its table of contents.
    # Keep the HTML container while exposing its headings at document level.
    return [
        RawBlock(
            "html",
            f'<div id="{escape(platform)}" class="cli-help-transcript">',
        ),
        *blocks,
        RawBlock("html", "</div>"),
    ]


if __name__ == "__main__":
    toJSONFilter(cli_help)
