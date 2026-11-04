"""Register MCP Console on an existing chatlas chat."""

from __future__ import annotations

from collections.abc import Sequence
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from ._common import Command, content_text, result_text, stdio_command

if TYPE_CHECKING:
    from chatlas import Tool
    from mcp.types import CallToolResult
    from mcp.types import Tool as MCPTool

    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def tools(console: MCPConsole | AsyncMCPConsole) -> list[Tool]:
    """Give a chatlas chat access to an existing Console session.

    Pass the returned list to `chat.set_tools()` after entering the Console
    context. Chatlas runs the tool-calling loop; Console keeps cell state between
    calls. Keep both the chat and connection for follow-up messages.

    This uses the connected server's schema, including its configured languages
    and requirements fields. `register_tool(console.send)` infers a schema from
    Python annotations and cannot describe those server settings.

    Text results return text, and tool errors raise. A successful result
    containing an image returns a `chatlas.ContentToolResult` carrying the
    ordered text and image content, so providers receive the image content
    and chat UIs can render it. [register()](chatlas.register.html) uses
    chatlas's native MCP support instead, which also preserves image content.

    Discovery includes every tool, even when the server has no `send` tool.

    Args:
        console: A connected `MCPConsole` or `AsyncMCPConsole`. An async client's
            tool must be used with `chat.chat_async()`.

    Returns:
        A list of `chatlas.Tool` objects with the server's live definitions.

    Examples:
        Install with `pip install 'mcp-console[chatlas]'`. For this OpenAI-backed
        chat, set `OPENAI_API_KEY` and set `OPENAI_MODEL` to your model ID.
        Chatlas prints the response and handles tool calls automatically:

        ```python
        import os
        import mcp_console
        from chatlas import ChatOpenAI
        from mcp_console import MCPConsole

        chat = ChatOpenAI(model=os.environ["OPENAI_MODEL"])
        with MCPConsole() as console:
            chat.set_tools(mcp_console.chatlas.tools(console))
            chat.chat(
                "Use Python in Console to calculate the mean of [12, 15, 18, 20, 25]."
            )
            chat.chat(
                "Use the same values in Console to calculate their sample standard deviation."
            )
        ```

        `set_tools()` replaces the chat's tools. If the chat already has tools,
        use `chat.set_tools([*chat.get_tools(), *mcp_console.chatlas.tools(console)])`.
    """
    return [_tool(console, definition) for definition in console.tools]


def _tool_result(result: CallToolResult) -> Any:
    """Convert a Console tool result into the chatlas tool's return value.

    Text-only results stay plain text and errors raise, matching
    `MCPConsole.send()`. A successful result containing an image becomes a
    `ContentToolResult` whose value holds the ordered content, which chatlas
    expands so providers receive text and image blocks.
    """
    if result.is_error or not any(item.type == "image" for item in result.content):
        return result_text(result)

    from chatlas import ContentToolResult
    from chatlas.types import ContentImageInline, ContentText

    value = [
        ContentImageInline(data=item.data, image_content_type=item.mime_type)
        if item.type == "image"
        else ContentText(text=content_text(item))
        for item in result.content
    ]
    return ContentToolResult(value=value, model_format="as_is")


def _tool(console: MCPConsole | AsyncMCPConsole, definition: MCPTool) -> Tool:
    from chatlas import Tool

    name = definition.name
    if iscoroutinefunction(console.call_tool):

        async def invoke(**arguments: Any) -> Any:
            return _tool_result(await console.call_tool(name, arguments))
    else:

        def invoke(**arguments: Any) -> Any:
            return _tool_result(console.call_tool(name, arguments))

    return Tool(
        func=invoke,
        name=definition.name,
        description=definition.description or "",
        parameters=definition.input_schema,
        strict=False,
    )


async def register(
    chat: Any,
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    **kwargs: Any,
) -> Any:
    """Start Console and register its native MCP tools on a chatlas chat.

    Chatlas owns this MCP connection and preserves text and image results.
    Register once, use `chat.chat_async()` for model requests, and call
    `await chat.cleanup_mcp_tools()` in `finally` to close the server even if
    a request fails. For a connection you also call directly from Python, use
    [tools()](chatlas.tools.html).

    Args:
        chat: An existing chatlas `Chat`.
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        **kwargs: Options passed to `chat.register_mcp_tools_stdio_async()`.
            Supply `cwd` and `env` inside `transport_kwargs`. Use the default
            tool names: a namespace can also change the name used to call
            the server's tools.

    Returns:
        The result of chatlas's MCP registration.

    Examples:
        Install with `pip install 'mcp-console[chatlas]'`. Set `OPENAI_API_KEY`
        and set `OPENAI_MODEL` to your model ID for this OpenAI-backed chat:

        ```python
        import asyncio
        import os
        import mcp_console
        from chatlas import ChatOpenAI


        async def main():
            chat = ChatOpenAI(model=os.environ["OPENAI_MODEL"])
            try:
                await mcp_console.chatlas.register(
                    chat,
                    transport_kwargs={"cwd": os.getcwd()},
                )
                await chat.chat_async(
                    "Use Python in Console to plot monthly sales "
                    "[12, 15, 18, 20, 25] and describe the trend."
                )
            finally:
                await chat.cleanup_mcp_tools()


        asyncio.run(main())
        ```
    """
    command, args = stdio_command(command, args)
    return await chat.register_mcp_tools_stdio_async(
        command=command, args=args, **kwargs
    )
