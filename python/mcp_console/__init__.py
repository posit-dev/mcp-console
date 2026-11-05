"""Composable Python integrations for the MCP Console stdio server."""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import anthropic, chatlas, codex, openai
from ._client import AsyncMCPConsole, Requirements
from ._sync import MCPConsole

if TYPE_CHECKING:
    from ._result import ConsoleError, ConsoleResult

__all__ = [
    "AsyncMCPConsole",
    "ConsoleError",
    "ConsoleResult",
    "MCPConsole",
    "Requirements",
    "anthropic",
    "chatlas",
    "codex",
    "openai",
]


def __getattr__(name: str) -> type[ConsoleError] | type[ConsoleResult]:
    # The base package and framework configuration helpers work without the SDK.
    if name in {"ConsoleError", "ConsoleResult"}:
        from ._result import ConsoleError, ConsoleResult

        return {"ConsoleError": ConsoleError, "ConsoleResult": ConsoleResult}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(set(globals()) | set(__all__))
