"""MCP Console adapters for OpenAI Responses and Agents."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from functools import partial
from inspect import iscoroutinefunction
from typing import TYPE_CHECKING, Any, overload

from ._common import Command, content_text, result_text, stdio_command

if TYPE_CHECKING:
    from agents import FunctionTool
    from mcp.types import CallToolResult, Tool

    from ._client import AsyncMCPConsole
    from ._sync import MCPConsole


def agents_tool(console: MCPConsole | AsyncMCPConsole) -> FunctionTool:
    """Give an OpenAI agent access to an existing Console session.

    The Agents SDK runs the tool loop and sends Console's output back to the
    model. Keep the connection open throughout `Runner.run_sync()` or
    `await Runner.run()`. Later tool calls in that run share the same cell state.
    Both sync and async clients are supported.

    This function tool returns text, with placeholders for images. Use
    [agents_server()](openai.agents_server.html) for native MCP image results.
    The live server schema is retained with `strict_json_schema=False` so
    optional `send` arguments remain optional.

    Args:
        console: A connected `MCPConsole` or `AsyncMCPConsole`.

    Returns:
        An Agents `FunctionTool` to include in `Agent(tools=...)`.

    Examples:
        Install with `pip install 'mcp-console[openai-agents]'`. Set
        `OPENAI_API_KEY` and set `OPENAI_MODEL` to your model ID. The runner
        executes any Console calls and returns the model's final answer:

        ```python
        import os
        import mcp_console
        from agents import Agent, Runner
        from mcp_console import MCPConsole

        with MCPConsole() as console:
            agent = Agent(
                name="Data analyst",
                model=os.environ["OPENAI_MODEL"],
                tools=[mcp_console.openai.agents_tool(console)],
            )
            result = Runner.run_sync(
                agent,
                "Use Python in Console to calculate the mean and sample "
                "standard deviation of [12, 15, 18, 20, 25].",
            )
            print(result.final_output)
        ```
    """
    from agents import FunctionTool
    from anyio import to_thread

    tool = console.send_tool

    async def invoke(context: Any, arguments: str) -> str:
        parsed = json.loads(arguments)
        if iscoroutinefunction(console.send):
            return await console.send(**parsed)
        return await to_thread.run_sync(partial(console.send, **parsed))

    return FunctionTool(
        name=tool.name,
        description=tool.description or "",
        params_json_schema=tool.input_schema,
        on_invoke_tool=invoke,
        strict_json_schema=False,
    )


@overload
def responses_tool(console: MCPConsole) -> ResponsesTool: ...


@overload
def responses_tool(console: AsyncMCPConsole) -> AsyncResponsesTool: ...


def responses_tool(
    console: MCPConsole | AsyncMCPConsole,
) -> ResponsesTool | AsyncResponsesTool:
    """Connect Console to an application-owned OpenAI Responses tool loop.

    Pass `tool.definition` to `responses.create(tools=...)`. When the model
    returns a `send` function call, `tool(call)` executes it and returns the
    `function_call_output` item for the next API request. Text and images are
    preserved. The application runs this loop and routes any other tools.

    Create the adapter after connecting Console, and keep that connection open
    for the entire loop. With `AsyncMCPConsole`, the factory returns an
    `AsyncResponsesTool`; await both the model requests and `tool(call)`.

    Args:
        console: A connected `MCPConsole` or `AsyncMCPConsole`.

    Returns:
        A `ResponsesTool` or `AsyncResponsesTool` matching the client.

    Examples:
        Install with `pip install 'mcp-console[openai]'`. Set `OPENAI_API_KEY`
        and set `OPENAI_MODEL` to your model ID. This example registers only
        Console, feeds each tool result back into the response chain, and
        prints the final answer:

        ```python
        import os
        import mcp_console
        from mcp_console import MCPConsole
        from openai import OpenAI

        with OpenAI() as client, MCPConsole() as console:
            tool = mcp_console.openai.responses_tool(console)
            response = client.responses.create(
                model=os.environ["OPENAI_MODEL"],
                input="Use Python in Console to calculate the mean and sample "
                "standard deviation of [12, 15, 18, 20, 25].",
                tools=[tool.definition],
                parallel_tool_calls=False,
            )
            while calls := [
                item for item in response.output if item.type == "function_call"
            ]:
                response = client.responses.create(
                    model=os.environ["OPENAI_MODEL"],
                    previous_response_id=response.id,
                    input=[tool(call) for call in calls],
                    tools=[tool.definition],
                    parallel_tool_calls=False,
                )
            print(response.output_text)
        ```

        `previous_response_id` keeps the conversation, including earlier tool
        calls. `parallel_tool_calls=False` requests sequential calls to the
        persistent Console. Keep passing the tool definition on each request.
    """
    from ._sync import MCPConsole

    tool = console.send_tool
    if isinstance(console, MCPConsole):
        return ResponsesTool(console, tool)
    return AsyncResponsesTool(console, tool)


def agents_server(
    *,
    command: Command | None = None,
    args: Sequence[Command] | None = None,
    name: str = "MCP Console",
    params: Mapping[str, Any] | None = None,
    client_session_timeout_seconds: float | None = None,
    **kwargs: Any,
) -> Any:
    """Start Console as a native MCP server for an OpenAI agent.

    Enter the returned async context and pass the server to
    `Agent(mcp_servers=...)`. The Agents SDK discovers Console's tools, runs
    model tool calls, and returns text and image results to the model. Leaving
    the context closes the server. You do not need a separate `AsyncMCPConsole`.
    Use [agents_tool()](openai.agents_tool.html) to share an existing connection.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        name: Display name for this MCP server.
        params: MCP stdio settings such as `cwd` and `env`.
        client_session_timeout_seconds: MCP request deadline. `None` leaves
            requests without a client deadline, including long Console startup.
        **kwargs: Other options passed to `MCPServerStdio`.

    Returns:
        An Agents `MCPServerStdio` async context manager.

    Examples:
        Install with `pip install 'mcp-console[openai-agents]'`. Set
        `OPENAI_API_KEY` and set `OPENAI_MODEL` to your model ID. This example
        gives the agent access to files in the current directory:

        ```python
        import asyncio
        import os
        import mcp_console
        from agents import Agent, Runner


        async def main():
            async with mcp_console.openai.agents_server(
                params={"cwd": os.getcwd()},
            ) as server:
                agent = Agent(
                    name="Data analyst",
                    model=os.environ["OPENAI_MODEL"],
                    mcp_servers=[server],
                )
                result = await Runner.run(
                    agent,
                    "Use Python in Console to plot monthly sales "
                    "[12, 15, 18, 20, 25] and describe the trend.",
                )
                print(result.final_output)


        asyncio.run(main())
        ```
    """
    from agents.mcp import MCPServerStdio

    command, args = stdio_command(command, args)
    return MCPServerStdio(
        name=name,
        params=dict(params or {}) | {"command": command, "args": args},
        client_session_timeout_seconds=client_session_timeout_seconds,
        **kwargs,
    )


def _responses_definition(tool: Tool) -> dict[str, Any]:
    return {
        "type": "function",
        "name": tool.name,
        "description": tool.description,
        "parameters": tool.input_schema,
        "strict": False,
    }


def _responses_arguments(arguments: str | Mapping[str, Any]) -> Mapping[str, Any]:
    parsed = json.loads(arguments) if isinstance(arguments, str) else arguments
    if not isinstance(parsed, Mapping):
        raise TypeError("OpenAI function arguments must decode to a JSON object")
    return parsed


def _responses_content(result: CallToolResult) -> str | list[dict]:
    text = result_text(result)
    if all(item.type == "text" for item in result.content):
        return text
    return [
        {
            "type": "input_image",
            "detail": "auto",
            "image_url": f"data:{item.mime_type};base64,{item.data}",
        }
        if item.type == "image"
        else {"type": "input_text", "text": content_text(item)}
        for item in result.content
    ]


def _responses_call(call: Any) -> tuple[str, str | Mapping[str, Any]]:
    if isinstance(call, Mapping):
        return call["call_id"], call["arguments"]
    return call.call_id, call.arguments


class AsyncResponsesTool:
    """An async Responses function tool that preserves Console text and images.

    Create this through [responses_tool()](openai.responses_tool.html) with an
    `AsyncMCPConsole`. Use `definition` when requesting a model response, then
    `await tool(call)` to build the next request's function-call output. The
    factory page shows the complete model loop.

    Examples:
        Install with `pip install 'mcp-console[openai]'`. You can also invoke
        `call()` directly to inspect the output without a model or API key:

        ```python
        import asyncio
        import mcp_console
        from mcp_console import AsyncMCPConsole


        async def main():
            async with AsyncMCPConsole() as console:
                tool = mcp_console.openai.responses_tool(console)
                output = await tool.call(
                    {"python": "sum([12, 15, 18, 20, 25])"}
                )
                print(output)


        asyncio.run(main())
        ```
    """

    def __init__(self, console: AsyncMCPConsole, tool: Tool) -> None:
        self._console = console
        self._tool = tool

    @property
    def definition(self) -> dict[str, Any]:
        """Function-tool definition to place in ``responses.create(tools=...)``."""
        return _responses_definition(self._tool)

    async def call(self, arguments: str | Mapping[str, Any]) -> str | list[dict]:
        """Execute `send` arguments supplied as a JSON string or mapping.

        Returns text for text-only output, or Responses `input_text` and
        `input_image` blocks for rich output. A model call's `arguments` can be
        passed directly; `output()` also attaches its `call_id`.
        """
        return _responses_content(
            await self._console._call_send(_responses_arguments(arguments))
        )

    async def output(self, call: Any) -> dict[str, Any]:
        """Execute a model function call and return its `function_call_output`.

        Accepts a Responses function-call object or a mapping with `call_id`
        and `arguments`. Pass the returned item in the next request's `input`.
        `await tool(call)` is shorthand for `await tool.output(call)`.
        """
        call_id, arguments = _responses_call(call)
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": await self.call(arguments),
        }

    __call__ = output


class ResponsesTool:
    """A synchronous Responses tool that preserves Console text and images.

    Create this through [responses_tool()](openai.responses_tool.html) with an
    `MCPConsole`. The factory page shows how to register `definition` and return
    `tool(call)` results in a complete model loop.

    Examples:
        Install with `pip install 'mcp-console[openai]'`. You can also invoke
        `call()` directly to inspect the output without a model or API key:

        ```python
        import mcp_console
        from mcp_console import MCPConsole

        with MCPConsole() as console:
            tool = mcp_console.openai.responses_tool(console)
            output = tool.call({"python": "sum([12, 15, 18, 20, 25])"})
            print(output)
        ```
    """

    def __init__(self, console: MCPConsole, tool: Tool) -> None:
        self._console = console
        self._tool = tool

    @property
    def definition(self) -> dict[str, Any]:
        """Function-tool definition to place in ``responses.create(tools=...)``."""
        return _responses_definition(self._tool)

    def call(self, arguments: str | Mapping[str, Any]) -> str | list[dict]:
        """Execute `send` arguments supplied as a JSON string or mapping.

        Returns text for text-only output, or Responses `input_text` and
        `input_image` blocks for rich output. A model call's `arguments` can be
        passed directly; `output()` also attaches its `call_id`.
        """
        return _responses_content(
            self._console._call_send(_responses_arguments(arguments))
        )

    def output(self, call: Any) -> dict[str, Any]:
        """Execute a model function call and return its `function_call_output`.

        Accepts a Responses function-call object or a mapping with `call_id`
        and `arguments`. Pass the returned item in the next request's `input`.
        `tool(call)` is shorthand for `tool.output(call)`.
        """
        call_id, arguments = _responses_call(call)
        return {
            "type": "function_call_output",
            "call_id": call_id,
            "output": self.call(arguments),
        }

    __call__ = output
