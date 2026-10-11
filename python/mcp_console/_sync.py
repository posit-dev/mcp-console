from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from functools import partial
from typing import TYPE_CHECKING, Any, Literal, Self

from ._client import AsyncMCPConsole, Requirements
from ._common import Command

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from mcp.types import Tool


class MCPConsole:
    """A synchronous, persistent connection to ``mcp-console serve``.

    Use as a context manager or explicitly call ``connect()`` and ``close()``.
    The context starts Console and discovers its tools; leaving it closes the
    connection and subprocess. Variables persist across calls on this connection.
    The application owns its model clients, agents, and tool loops.

    Args:
        command: Console executable. Defaults to the installed `mcp-console`.
        args: Server arguments. Defaults to `["serve"]`.
        server_parameters: MCP stdio settings such as `cwd` and `env`.

    Examples:
        Install with `pip install 'mcp-console[client]'`. Define values in one
        cell and use them in the next:

        ```python
        from mcp_console import MCPConsole

        with MCPConsole() as console:
            print(console.send(python="values = [12, 15, 18, 20, 25]; values"))
            print(
                console.send(
                    python="import statistics; statistics.mean(values)"
                )
            )
        ```

        Create framework adapters inside this context, after discovery. See
        [chatlas](chatlas.tool.html), [Anthropic](anthropic.tool.html),
        [Responses](openai.responses_tool.html), or [Agents](openai.agents_tool.html).
    """

    def __init__(
        self,
        *,
        command: Command | None = None,
        args: Sequence[Command] | None = None,
        server_parameters: Mapping[str, Any] | None = None,
    ) -> None:
        self._async = AsyncMCPConsole(
            command=command, args=args, server_parameters=server_parameters
        )
        self._stack: ExitStack | None = None
        self._portal: BlockingPortal | None = None

    def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._stack is None:
            from anyio.from_thread import start_blocking_portal

            with ExitStack() as stack:
                portal = stack.enter_context(start_blocking_portal())
                # The MCP transport must enter and exit in the same async task.
                stack.enter_context(portal.wrap_async_context_manager(self._async))
                self._portal = portal
                self._stack = stack.pop_all()
        return self

    def close(self) -> None:
        """Close the MCP session, subprocess, and portal thread."""
        stack = self._stack
        self._stack = self._portal = None
        if stack is not None:
            stack.close()

    def __enter__(self) -> "Self":
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _run(self, func: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        if self._portal is None:
            raise RuntimeError(
                "MCPConsole is not connected; use `with MCPConsole() as console` "
                "or call `console.connect()` first"
            )
        return self._portal.call(partial(func, *args, **kwargs))

    def send(
        self,
        *,
        r: str | None = None,
        python: str | None = None,
        sql: str | None = None,
        control: Literal["interrupt", "restart"] | None = None,
        requirements: Requirements | None = None,
        stdin: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Run or control a cell on this connection and return its text output.

        The arguments and polling behavior match
        [AsyncMCPConsole.send()](AsyncMCPConsole.html#send). Send one language
        per call. If output ends in `[running; poll with an empty send]`, collect
        the remaining output with `console.send()` before submitting another cell.
        Images appear as MIME placeholders; MCP tool errors raise `RuntimeError`.
        """
        return self._run(
            self._async.send,
            r=r,
            python=python,
            sql=sql,
            control=control,
            requirements=requirements,
            stdin=stdin,
            timeout_ms=timeout_ms,
        )

    send.__doc__ = AsyncMCPConsole.send.__doc__
    __call__ = send

    @property
    def send_tool(self) -> "Tool":
        """The connected server's MCP tool definition, used by SDK adapters.

        Available after connecting. Its input schema reflects the server's
        configured capabilities. Use a framework adapter to register this
        schema; recreate adapters after reconnecting.
        """
        return self._async.send_tool
