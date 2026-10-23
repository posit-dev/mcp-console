"""Native Anthropic tools backed by MCP Console."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import asynccontextmanager
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any

from ._common import Command, stdio_command

if TYPE_CHECKING:
    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def tool(console: MCPConsole | AsyncMCPConsole, **kwargs: Any) -> Any:
    """Give Anthropic's tool runner access to an existing Console session.

    The runner calls Console's `send` tool and returns its output to Claude
    before requesting the next response. Keep the connection open for the
    whole conversation so later tool calls can use variables from earlier cells.
    A synchronous client produces a synchronous tool; an async client produces
    an async tool for `AsyncAnthropic`.

    This adapter returns text, with placeholders for images. For plots and other
    image output, use [tools()](anthropic.tools.html), which opens its own
    connection and preserves MCP content blocks.

    Args:
        console: A connected `MCPConsole` or `AsyncMCPConsole`.
        **kwargs: Options passed to Anthropic's `beta_tool` or `beta_async_tool`,
            such as `defer_loading`.

    Returns:
        A tool to include in `client.beta.messages.tool_runner(tools=...)`.

    Examples:
        Install with `pip install 'mcp-console[anthropic]'`. Set
        `ANTHROPIC_API_KEY` and set `ANTHROPIC_MODEL` to the Claude model ID
        you want to use. This script prints the model's final answer:

        ```python
        import os
        import mcp_console
        from anthropic import Anthropic
        from mcp_console import MCPConsole

        with Anthropic() as client, MCPConsole() as console:
            runner = client.beta.messages.tool_runner(
                model=os.environ["ANTHROPIC_MODEL"],
                max_tokens=4096,
                tools=[mcp_console.anthropic.tool(console)],
                messages=[
                    {
                        "role": "user",
                        "content": "Use Python in Console to calculate the mean "
                        "and sample standard deviation of [12, 15, 18, 20, 25].",
                    }
                ],
            )
            message = runner.until_done()
            for block in message.content:
                if block.type == "text":
                    print(block.text)
        ```
    """
    from anthropic import beta_async_tool, beta_tool

    definition = console.send_tool
    if iscoroutinefunction(console.send):

        async def invoke(**arguments: Any) -> str:
            return await console.send(**arguments)

        decorate = beta_async_tool
    else:

        def invoke(**arguments: Any) -> str:
            return console.send(**arguments)

        decorate = beta_tool

    return decorate(
        invoke,
        name=definition.name,
        description=definition.description,
        input_schema=definition.input_schema,
        **kwargs,
    )


@asynccontextmanager
async def tools(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    server_parameters: Mapping[str, Any] | None = None,
    tool_kwargs: Mapping[str, Any] | None = None,
) -> AsyncIterator[list[Any]]:
    """Open a Console session for Anthropic's async tool runner, including images.

    Enter this async context once per conversation and pass the yielded list
    directly to `tool_runner(tools=...)`. The runner executes model tool calls,
    sends their text and image results back to Claude, and continues until the
    model finishes. The same Console process stays alive throughout the context;
    leaving it closes the MCP connection and its subprocess.

    You do not need to create an `AsyncMCPConsole` yourself. To share a connection
    with ordinary Python calls, use [tool()](anthropic.tool.html) instead.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        server_parameters: MCP stdio settings such as `cwd` and `env`. Use `cwd`
            to select the workspace whose files the Console session can access.
        tool_kwargs: Options passed to Anthropic's `async_mcp_tool`, such as
            `defer_loading`.

    Yields:
        Native Anthropic tools discovered from the connected Console server.

    Examples:
        Install with `pip install 'mcp-console[anthropic]'`. Set
        `ANTHROPIC_API_KEY` and set `ANTHROPIC_MODEL` to the Claude model ID
        you want to use. This script gives the model access to the current
        directory and asks it to create and inspect a plot:

        ```python
        import asyncio
        import os
        import mcp_console
        from anthropic import AsyncAnthropic


        async def main():
            async with AsyncAnthropic() as client:
                async with mcp_console.anthropic.tools(
                    server_parameters={"cwd": os.getcwd()},
                ) as tools:
                    runner = client.beta.messages.tool_runner(
                        model=os.environ["ANTHROPIC_MODEL"],
                        max_tokens=4096,
                        tools=tools,
                        messages=[
                            {
                                "role": "user",
                                "content": "Use Python in Console to plot monthly sales "
                                "[12, 15, 18, 20, 25] and describe the trend.",
                            }
                        ],
                    )
                    message = await runner.until_done()
                    for block in message.content:
                        if block.type == "text":
                            print(block.text)


        asyncio.run(main())
        ```

        The plot is returned to Claude as an image tool result. The loop above
        prints Claude's text response; it does not save the plot to disk.
    """
    from anthropic.lib.tools.mcp import async_mcp_tool
    from mcp import Client, StdioServerParameters

    command, args = stdio_command(command, args)
    parameters = dict(server_parameters or {}) | {"command": command, "args": args}
    async with Client(StdioServerParameters(**parameters)) as client:
        tools = await client.list_tools()
        yield [
            async_mcp_tool(tool, client, **dict(tool_kwargs or {}))
            for tool in tools.tools
        ]
