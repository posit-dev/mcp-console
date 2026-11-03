"""Rewrite generated Markdown links while preserving prose and code verbatim."""

from collections.abc import Callable
import json
import re
import subprocess


def rewrite_links(text: str, rewrite: Callable[[str], str]) -> str:
    document = json.loads(
        subprocess.run(
            ["quarto", "pandoc", "--from=commonmark+sourcepos", "--to=json"],
            input=text,
            encoding="utf-8",
            capture_output=True,
            check=True,
        ).stdout
    )
    lines = text.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    spans = []

    def visit(node: object) -> None:
        if isinstance(node, dict):
            if node.get("t") in ("Link", "Image"):
                positions = dict(node["c"][0][2])["data-pos"].split("-")
                start, end = (
                    offsets[int(line) - 1] + int(column) - 1
                    for line, column in (position.split(":") for position in positions)
                )
                spans.append((start, end))
            for child in node.values():
                visit(child)
        elif isinstance(node, list):
            for child in node:
                visit(child)

    visit(document)
    for start, end in sorted(spans, reverse=True):
        # Generated documentation uses inline links. Work only on their final
        # destination so code inside a link label also stays untouched.
        link = re.sub(
            r"(?<=\]\()([^\s)]+)(?=\)$)",
            lambda match: rewrite(match[0]),
            text[start:end],
        )
        text = text[:start] + link + text[end:]
    return text
