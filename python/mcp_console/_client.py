from __future__ import annotations

from collections.abc import Mapping, Sequence
from contextlib import AsyncExitStack
from typing import TYPE_CHECKING, Any, Literal, Self, TypedDict

from ._common import Command, stdio_command

if TYPE_CHECKING:
    from mcp import Client
    from mcp.types import CallToolResult, Tool

    from ._result import ConsoleResult


class Requirements(TypedDict, total=False):
    """Dependency declarations accepted by the clients' `send(requirements=...)`.

    `add` merges packages into the session declaration. `get` inspects it, `set`
    replaces it (emptying omitted fields), and `reset` restores startup defaults.
    Changed replacements of a live worker require `control="restart"`, which
    also discards its variables. Python packages run in Console's worker
    environment; installing a framework extra prepares the calling application.

    Inspection normally returns declarations in `result.structured_content`, but
    can return a startup notice before the runtime is ready. Check that field
    before reading it; notices are available in `result.text`.
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
    Variables persist across `send()` calls to the same named session. A new
    connection starts with a fresh default session.
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
        See [Anthropic](anthropic.callable_tools.html), [chatlas](chatlas.tools.html),
        [Responses](openai.responses_tools.html), or [Agents](openai.agents_tools.html).
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
        self._tools: list[Tool] | None = None

    async def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._client is not None:
            return self

        from mcp import Client, StdioServerParameters

        command, args = stdio_command(self._command, self._args)
        parameters = self._server_parameters | {"command": command, "args": args}
        async with AsyncExitStack() as stack:
            # The SDK sends `initialize` when discovery is unanswered after ten
            # seconds, for example while remote startup is pending. Console
            # 0.0.6 and later then keep the legacy lifecycle. Console 0.0.5 and
            # earlier answer the late discovery and reject the legacy requests
            # that follow; a slow start of those versions is unsupported.
            client = await stack.enter_async_context(
                Client(StdioServerParameters(**parameters))
            )
            tools = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor)
                tools.extend(page.tools)
                cursor = page.next_cursor
                if cursor is None:
                    break
            self._stack = stack.pop_all()
            self._client = client
            self._tools = tools
        return self

    async def close(self) -> None:
        """Close the MCP client session and terminate its subprocess."""
        stack = self._stack
        self._stack = self._client = self._tools = None
        if stack is not None:
            await stack.aclose()

    async def __aenter__(self) -> "Self":
        return await self.connect()

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.close()

    async def call_tool(
        self, name: str, arguments: Mapping[str, Any]
    ) -> "CallToolResult":
        """Call a discovered tool by name and return its complete MCP result.

        Arguments are forwarded unchanged, including explicit null values.
        The result preserves content blocks, structured content, and `is_error`.
        This method does not require a tool named `send`.
        """
        if self._client is None:
            raise RuntimeError(
                "AsyncMCPConsole is not connected; use `async with AsyncMCPConsole() as console` "
                "or call `await console.connect()` first"
            )
        return await self._client.call_tool(name, dict(arguments))

    async def send(
        self,
        *,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        note: str | None = None,
        session: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: int | None = None,
    ) -> ConsoleResult:
        """Run or control the persistent R, Python, and SQL console.

        Send at most one of r, python, or sql per call.
        If `result.text` ends in ``[running; poll with an empty send]``, call again
        with the same `session` and without code or stdin until completion
        before submitting another cell to that session. Different sessions
        can run concurrently. An empty call without `session` polls all active
        sessions and lists their statuses.
        `console(...)` is an alias for `send(...)`.
        If tool configuration hides `timeout_ms`, poll without that argument.

        Args:
            r: One complete R cell.
            python: One complete Python cell.
            sql: One complete SQL cell on the selected connection.
            note: Plain text or Markdown journal entry. May accompany code or
                be sent alone without polling or consuming output.
            session: Session name. Code, input, and notes use "default" when omitted.
                A new name starts an independent session on its first code
                call. Omission or None on an empty poll collects
                all active sessions; pass "default" to poll only that session.
            control: Interrupt the active operation or restart the runtime.
            requirements: Declaration keyed by r, python, or duckdb, with optional
                python_version and exclude_newer constraints. action defaults to
                add; get normally returns inspection data in `structured_content`,
                but can return a startup notice before the runtime is ready; set
                replaces omitted fields with empty values; reset restores startup defaults.
                Changed replacements of a live worker require control="restart".
            stdin: Exact input for an active prompt or debugger.
            timeout_ms: Maximum observation wait; timeout does not cancel work.
                Omission or None uses the server default of 60,000 ms.

        Returns:
            A `ConsoleResult` retaining the complete MCP response. Its `text`
            property contains text output and lifecycle notices; `images` holds
            native image blocks, and `content` preserves their order. Printing
            the result renders image placeholders; notebook display renders
            the images. Calling `send()` itself does not display anything.

        Raises:
            ConsoleError: An MCP tool error, with the complete response available
                as `error.result`. This is a `RuntimeError` subclass.

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
                    while output.text.endswith("[running; poll with an empty send]"):
                        output = await console.send(session="default")
                        print(output, end="")


            asyncio.run(main())
            ```
        """
        from ._result import ConsoleError, ConsoleResult

        arguments = {
            "r": r,
            "python": python,
            "sql": sql,
            "note": note,
            "session": session,
            "control": control,
            "requirements": requirements,
            "stdin": stdin,
            "timeout_ms": timeout_ms,
        }
        raw = await self.call_tool(
            "send",
            {name: value for name, value in arguments.items() if value is not None},
        )
        # The SDK has already validated these fields; retain its native objects.
        result = ConsoleResult.model_construct(
            _fields_set=raw.model_fields_set.copy(), **dict(raw)
        )
        if result.is_error:
            raise ConsoleError(result)
        return result

    # Ordinary Python shorthand; SDK registration uses the product adapters.
    __call__ = send

    @property
    def tools(self) -> "list[Tool]":
        """All MCP tool definitions discovered when this connection opened.

        Discovery includes every page and does not require a `send` tool.
        Use plural framework adapters to register every discovered schema.
        Recreate adapters after reconnecting to pick up changed definitions.
        """
        if self._tools is None:
            raise RuntimeError(
                "Connect the console before creating tools; enter its context or call connect()"
            )
        return list(self._tools)

    def get_tool(self, name: str) -> "Tool":
        """Return a discovered definition, or raise if the name is absent."""
        for tool in self.tools:
            if tool.name == name:
                return tool
        raise RuntimeError(f"MCP Console did not expose a tool named {name!r}")
