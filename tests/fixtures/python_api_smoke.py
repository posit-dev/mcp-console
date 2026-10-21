"""Exercise the installed wheel and its optional SDK dependencies."""

import asyncio
import json
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path

import mcp_console
from agents import Agent
from agents.tool_context import ToolContext
from chatlas import ChatOpenAI
from openai_codex.generated.v2_all import ThreadStartParams

assert Path(mcp_console.__file__).is_relative_to(sys.prefix), mcp_console.__file__

options = {
    "args": ["serve", "--worker", str(Path(__file__).with_name("zod"))],
    # Run the fixture with the harness interpreter, independently of the client.
    "server_parameters": {"env": {"MCP_CONSOLE_TEST_PYTHON": sys.argv[1]}},
}
arguments = json.dumps({"r": "echo installed wheel"})
context = ToolContext(
    context=None, tool_name="send", tool_call_id="call_1", tool_arguments=arguments
)
expected = "zod: installed wheel\n"


def sync_main() -> None:
    with mcp_console.MCPConsole(**options) as console:
        assert console.send(r="echo installed wheel") == expected
        tool = mcp_console.openai.agents_tool(console)
        assert asyncio.run(tool.on_invoke_tool(context, arguments)) == expected
        assert (
            mcp_console.anthropic.tool(console).call({"r": "echo installed wheel"})
            == expected
        )
        assert mcp_console.openai.responses_tool(console).call(arguments) == expected
        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.set_tools([*chat.get_tools(), mcp_console.chatlas.tool(console)])
        assert chat.get_tools()[0].func(r="echo installed wheel") == expected


async def main() -> None:
    async with mcp_console.AsyncMCPConsole(**options) as console:
        assert await console.send(r="echo installed wheel") == expected
        tool = mcp_console.openai.agents_tool(console)
        assert await tool.on_invoke_tool(context, arguments) == expected
        assert (
            await mcp_console.anthropic.tool(console).call(
                {"r": "echo installed wheel"}
            )
            == expected
        )
        assert (
            await mcp_console.openai.responses_tool(console).call(arguments) == expected
        )
        chat = ChatOpenAI(model="unused", api_key="unused")
        chat.set_tools([*chat.get_tools(), mcp_console.chatlas.tool(console)])
        assert await chat.get_tools()[0].func(r="echo installed wheel") == expected
        assert Agent(name="test", tools=[tool]).tools == [tool]
    server = mcp_console.openai.agents_server()
    assert Agent(name="test", mcp_servers=[server]).mcp_servers == [server]
    ThreadStartParams(config={"mcp_servers": {"console": mcp_console.codex.server()}})


with tempfile.TemporaryDirectory(prefix="mcp-console-sdk-smoke-") as directory:
    options["server_parameters"]["cwd"] = directory
    options["server_parameters"]["env"]["MCP_CONSOLE_HOME"] = str(
        Path(directory) / "console"
    )
    sync_main()
    asyncio.run(main())
    journals = list(
        (Path(directory) / "console/sessions").glob("*/capture/events.jsonl")
    )
    assert len(journals) == 2, journals
print(sys.version)
for package in (
    "mcp-console",
    "mcp",
    "anthropic",
    "chatlas",
    "openai",
    "openai-agents",
    "openai-codex",
):
    print(f"{package}=={version(package)}")
