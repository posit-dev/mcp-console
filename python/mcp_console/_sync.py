import asyncio
from collections.abc import Mapping, Sequence
from concurrent.futures import Future
from contextlib import ExitStack
from threading import Event, Lock
from typing import TYPE_CHECKING, Any, Literal, Self

from ._client import AsyncMCPConsole, Requirements
from ._common import Command, result_text

_CANCELLATION_ERROR = (
    "Request cancellation cleanup did not finish; "
    "close and reconnect before reusing this connection"
)
_CANCELLATION_PENDING = (
    "Request cancellation cleanup is in progress; wait before reusing this connection"
)

if TYPE_CHECKING:
    from anyio.from_thread import BlockingPortal
    from mcp.types import CallToolResult, Tool


class _Request:
    """Own one SDK call from queued admission through confirmed task retirement.

    The host owns the response Future, so cancelling it never waits for the
    event loop. SDK cancellation runs on the loop; the task's done callback
    confirms retirement even if cancellation prevents its coroutine starting.
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        console: AsyncMCPConsole,
        arguments: Mapping[str, Any],
    ) -> None:
        self.future: Future["CallToolResult"] = Future()
        self.finished = Event()
        self._loop = loop
        self._console = console
        self._arguments = arguments
        self._lock = Lock()
        self._task: asyncio.Task["CallToolResult"] | None = None

    def start(self) -> None:
        self._loop.call_soon_threadsafe(self._start)

    def _start(self) -> None:
        with self._lock:
            if self.future.cancelled():
                self.finished.set()
                return
            self._task = self._loop.create_task(self._run())
            self._task.add_done_callback(self._finish)

    async def _run(self) -> "CallToolResult":
        if self.future.cancelled():
            raise asyncio.CancelledError
        return await self._console._call_send(self._arguments)

    def _finish(self, task: asyncio.Task["CallToolResult"]) -> None:
        with self._lock:
            try:
                result = task.result()
            except asyncio.CancelledError:
                self.future.cancel()
            except BaseException as error:
                if not self.future.cancelled():
                    self.future.set_exception(error)
            else:
                if not self.future.cancelled():
                    self.future.set_result(result)
            finally:
                self.finished.set()

    def cancel(self) -> None:
        with self._lock:
            self.future.cancel()
            if self._task is None:
                # The queued start must check the same cancelled Future under
                # this lock, so it cannot create a task after this receipt.
                self.finished.set()
                return
        self._loop.call_soon_threadsafe(self._cancel)

    def _cancel(self) -> None:
        if self._task is not None:
            self._task.cancel()

    def abort(self) -> None:
        # A second native cancellation escapes shielded SDK courtesy writes.
        self._loop.call_soon_threadsafe(self._cancel)


class MCPConsole:
    """A synchronous, persistent connection to ``mcp-console serve``.

    Use as a context manager or explicitly call ``connect()`` and ``close()``.
    The context starts Console and discovers its tools; leaving it closes the
    connection and subprocess. Variables persist across calls on this connection.
    The application owns its model clients, agents, and tool loops.

    Calls block while the official MCP client runs on a background event loop.
    Catching `KeyboardInterrupt` while waiting cancels that request's wait,
    leaving an admitted cell running.
    Poll to collect its output or send `control="interrupt"` to stop execution.
    Connect and close only when no other call is in progress.

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
        self._loop: asyncio.AbstractEventLoop | None = None
        self._admission_lock = Lock()
        self._cancellation: _Request | None = None
        self._cancellation_failed = False

    def connect(self) -> "Self":
        """Start MCP Console and initialize its MCP client session."""
        if self._cancellation_failed:
            raise RuntimeError(_CANCELLATION_ERROR)
        if self._cancellation is not None:
            raise RuntimeError(_CANCELLATION_PENDING)
        if self._stack is None:
            from anyio.from_thread import start_blocking_portal

            with ExitStack() as stack:
                portal = stack.enter_context(start_blocking_portal())
                # The MCP transport must enter and exit in the same async task.
                stack.enter_context(portal.wrap_async_context_manager(self._async))
                self._portal = portal
                self._loop = portal.call(asyncio.get_running_loop)
                self._stack = stack.pop_all()
        return self

    def close(self) -> None:
        """Close the MCP session, subprocess, and portal thread."""
        stack = self._stack
        if stack is not None:
            if self._cancellation is not None and not self._cancellation.finished.wait(
                timeout=5
            ):
                # Retain ownership rather than joining a request that still
                # suppresses native cancellation or permitting a replacement.
                raise RuntimeError(
                    "Cancelled request did not stop; connection remains invalid"
                )
            try:
                stack.close()
            finally:
                # ExitStack consumes all its callbacks even when teardown
                # raises; the old portal must never look connected afterward.
                self._stack = self._portal = self._loop = None
                self._cancellation = None
                self._cancellation_failed = False

    def __enter__(self) -> "Self":
        return self.connect()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.close()

    def _call_send(self, arguments: Mapping[str, Any]) -> "CallToolResult":
        request: _Request | None = None
        try:
            # Only admission is serialized. Submission is nonblocking and is
            # itself inside the interrupt boundary, before the SDK task starts.
            with self._admission_lock:
                if self._cancellation_failed:
                    raise RuntimeError(_CANCELLATION_ERROR)
                if self._cancellation is not None:
                    raise RuntimeError(_CANCELLATION_PENDING)
                if self._loop is None:
                    raise RuntimeError(
                        "MCPConsole is not connected; use `with MCPConsole() as console` "
                        "or call `console.connect()` first"
                    )
                request = _Request(self._loop, self._async, arguments)
                request.start()
            return request.future.result()
        except KeyboardInterrupt:
            if request is None:
                raise
            with self._admission_lock:
                self._cancellation = request
            completed = False
            try:
                request.cancel()
                completed = request.finished.wait(timeout=5)
            finally:
                with self._admission_lock:
                    if completed:
                        self._cancellation = None
                    else:
                        self._cancellation_failed = True
                        request.abort()
            raise

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
        arguments = {
            "r": r,
            "python": python,
            "sql": sql,
            "control": control,
            "requirements": requirements,
            "stdin": stdin,
            "timeout_ms": timeout_ms,
        }
        return result_text(
            self._call_send(
                {name: value for name, value in arguments.items() if value is not None}
            )
        )

    __call__ = send

    @property
    def send_tool(self) -> "Tool":
        """The connected server's MCP tool definition, used by SDK adapters.

        Available after connecting. Its input schema reflects the server's
        configured capabilities. Use a framework adapter to register this
        schema; recreate adapters after reconnecting.
        """
        return self._async.send_tool
