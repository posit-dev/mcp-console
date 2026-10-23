from __future__ import annotations

import json
import os
import shutil
import sysconfig
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.types import CallToolResult, ContentBlock

Command = str | os.PathLike[str]


def stdio_command(
    command: Command | None, args: Sequence[Command] | None
) -> tuple[str, list[str]]:
    if command is None:
        candidate = Path(sysconfig.get_path("scripts")) / "mcp-console"
        command = (
            candidate if os.access(candidate, os.X_OK) else shutil.which("mcp-console")
        )
        if command is None:
            raise FileNotFoundError(
                "Install mcp-console in this Python environment or pass command= explicitly"
            )
    return os.fspath(command), ["serve"] if args is None else [
        os.fspath(arg) for arg in args
    ]


def content_text(content: ContentBlock) -> str:
    if content.type == "text":
        return content.text
    if content.type == "image":
        return f"[{content.mime_type} output]"
    return content.model_dump_json(by_alias=True, exclude_none=True)


def result_text(result: CallToolResult) -> str:
    text = ""
    for item in result.content:
        part = content_text(item)
        if text and part and not text.endswith("\n") and not part.startswith("\n"):
            text += "\n"
        text += part
    if result.is_error:
        raise RuntimeError(text or "MCP Console returned an error")
    if (
        isinstance(result.structured_content, dict)
        and "requirements" in result.structured_content
    ):
        return json.dumps(result.structured_content, indent=2)
    return text
