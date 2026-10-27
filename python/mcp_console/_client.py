from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal, Self, TypedDict

from ._common import Command, result_text, stdio_command

if TYPE_CHECKING:
    from mcp import Client
    from mcp.types import CallToolResult, Tool


class Requirements(TypedDict, total=False):
    """Dependency declarations accepted by the clients' `send(requirements=...)`.

    `add` merges packages into the session declaration. `get` inspects it, `set`
    replaces it (emptying omitted fields), and `reset` restores startup defaults.
    Changed replacements of a live worker require `control="restart"`, which
    also discards its variables. Python packages run in Console's worker
    environment; installing a framework extra prepares the calling application.

    Inspection normally returns JSON text, but can return a startup notice
    before the runtime is ready; check the response before parsing it.
    See the [send reference](../../API.html#requirements) for accepted package
    syntax, preparation policies, and complete replacement rules.

    Attributes:
        action: `get`, `add` (the default), `set`, or `reset`.
        python_version: Python version constraints, such as `[">=3.11"]`.
        exclude_newer: Optional cutoff date for dependency resolution.
        r: R package requirements.
        python: Python package requirements.
        duckdb: DuckDB extensions.

    Examples:
        Install with `pip install 'mcp-console[client]'`. Add NumPy to Console's
        managed Python environment and use it in the same call:

        ```python
        from mcp_console import MCPConsole, Requirements

        requirements: Requirements = {"python": ["numpy"]}
        with MCPConsole() as console:
            print(
                console.send(
                    requirements=requirements,
                    python="import numpy as np; np.mean([12, 15, 18, 20, 25])",
                )
            )
        ```
    """

    action: Literal["get", "add", "set", "reset"]
    python_version: list[str]
    exclude_newer: str | None
    r: list[str]
    python: list[str]
    duckdb: list[str]


class AsyncMCPConsole:
    """A persistent, callable connection to ``mcp-console serve``.

    Enter and close the connection in the same async task. The application owns
    its model clients, agents, and tool loops. The context starts the server and
    discovers its tools; leaving the context closes its connection and process.
    Variables persist across `send()` calls on this connection. A new connection
    starts a new session.
    By default the client prefers Console in the current Python environment,
    then PATH, and launches `serve`. Use `command` and `args` to select another
    executable or launch configuration.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        server_parameters: MCP stdio settings such as `cwd` and `env`.

    Examples:
        Install with `pip install 'mcp-console[client]'`. This script defines
        values in one cell and uses them in a second cell:

        ```python
        import asyncio
        from mcp_console import AsyncMCPConsole


        async def main():
            async with AsyncMCPConsole() as console:
                print(
                    await console.send(
                        python="values = [12, 15, 18, 20, 25]; values"
                    )
                )
                print(
                    await console.send(
                        python="import statistics; statistics.mean(values)"
                    )
                )


        asyncio.run(main())
        ```

        Framework adapters are created inside this context, after discovery.
        See [Anthropic](anthropic.tool.html), [chatlas](chatlas.tool.html),
        [Responses](openai.responses_tool.html), or [Agents](openai.agents_tool.html).
    """

    def __init__(
        self,
        *,
        command: Command | None = None,
        args: Sequence[Command] | None = None,
        server_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._command = command
        self._args = args
        self._server_parameters = dict(server_parameters or {})
        self._stack: AsyncExitStack | None = None
        self._client: Client | None = None
        self._send_tool: Tool | None = None

    async def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._client is not None:
            return self

        from mcp import Client, StdioServerParameters

        command, args = stdio_command(self._command, self._args)
        parameters = self._server_parameters | {"command": command, "args": args}
        async with AsyncExitStack() as stack:
            # Automatic discovery can time out while remote startup is pending
            # and switch lifecycles on the same connection. Initialize once.
            client = await stack.enter_async_context(
                Client(StdioServerParameters(**parameters), mode="legacy")
            )
            tools = await client.list_tools()
            send_tool = next(
                (tool for tool in tools.tools if tool.name == "send"), None
            )
            if send_tool is None:
                raise RuntimeError("MCP Console did not expose its send tool")
            self._stack = stack.pop_all()
            self._client = client
            self._send_tool = send_tool
        return self

    async def close(self) -> None:
        """Close the MCP client session and terminate its subprocess."""
        stack = self._stack
        self._stack = self._client = self._send_tool = None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> "Self":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def _call_send(self, arguments: Mapping[str, Any]) -> "CallToolResult":
        if self._client is None:
            raise RuntimeError(
                "AsyncMCPConsole is not connected; use `async with AsyncMCPConsole() as console` "
                "or call `await console.connect()` first"
            )
        return await self._client.call_tool("send", dict(arguments))

    async def send(
        self,
        *,
        code: str | None = None,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Run or control the persistent console.

        Send at most one of code, r, python, or sql per call.
        The connected server's schema determines which source fields are available.
        If output ends in ``[running; poll with an empty send]``, call again
        without code or stdin until completion before submitting another cell.
        `console(...)` is an alias for `send(...)`.
        If tool configuration hides `timeout_ms`, poll without that argument.

        Args:
            code: One complete cell in the selected Jupyter kernel's language.
                Requires a server launched with `--jupyter-kernel`.
            r: One complete R cell.
            python: One complete Python cell.
            sql: One complete SQL cell on the selected connection.
            control: Interrupt the active operation or restart the runtime.
            requirements: Declaration keyed by r, python, or duckdb, with optional
                python_version and exclude_newer constraints. action defaults to
                add; get normally returns inspection JSON, but can return a
                startup notice before the runtime is ready; set replaces omitted
                fields with empty values; reset restores startup defaults.
                Changed replacements of a live worker require control="restart".
            stdin: Exact input for an active prompt or debugger.
            timeout_ms: Maximum observation wait; timeout does not cancel work.
                Omission or None uses the server default of 60,000 ms.

        Returns:
            Collected text output and lifecycle notices. Images appear as MIME
            placeholders. MCP tool errors raise `RuntimeError`.

        Examples:
            A short observation window may return before the cell finishes.
            Poll with an empty call, collecting each chunk without rerunning
            the cell:

            ```python
            import asyncio
            from mcp_console import AsyncMCPConsole


            async def main():
                async with AsyncMCPConsole() as console:
                    output = await console.send(
                        python="sum(range(1_000_000))",
                        timeout_ms=0,
                    )
                    print(output, end="")
                    while output.endswith("[running; poll with an empty send]"):
                        output = await console.send()
                        print(output, end="")


            asyncio.run(main())
            ```
        """
        arguments = {
            "code": code,
            "r": r,
            "python": python,
            "sql": sql,
            "control": control,
            "requirements": requirements,
            "stdin": stdin,
            "timeout_ms": timeout_ms,
        }
        return result_text(
            await self._call_send(
                {name: value for name, value in arguments.items() if value is not None}
            )
        )

    # Ordinary Python shorthand; SDK registration uses the product adapters.
    __call__ = send

    @property
    def send_tool(self) -> "Tool":
        """The connected server's MCP tool definition, used by SDK adapters.

        Available after connecting. Its input schema reflects the server's
        configured capabilities. Use a framework adapter to register this
        schema; automatic inference from `send()` annotations loses that detail.
        Recreate adapters after reconnecting.
        """
        if self._send_tool is None:
            raise RuntimeError(
                "Connect the console before creating tools; enter its context or call connect()"
            )
        return self._send_tool
