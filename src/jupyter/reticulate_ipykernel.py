"""An ipykernel endpoint inside the R process initialized by reticulate.

The Jupyter event loop lives on a background thread. Console pairs each Python
execute_request with an R call to run_next(), so user code runs on R's owning
thread through an ordinary reticulate call. Both endpoints must finish before
Console admits another cell. Independent clients and subshells are unsupported.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import contextvars
import os
import queue
import sys
import threading
import traceback
from collections.abc import Callable, Coroutine, Iterator
from typing import Any

from ipykernel import version_info
from ipykernel.ipkernel import IPythonKernel
from ipykernel.kernelapp import IPKernelApp
from traitlets import Type

Execution = tuple[
    contextvars.Context,
    Callable[[], Coroutine[Any, Any, dict]],
    concurrent.futures.Future[dict],
]
_requests: queue.Queue[Execution] = queue.Queue(maxsize=1)
_execution_loop: asyncio.AbstractEventLoop | None = None


def run_next() -> None:
    """Run the paired Python request on the thread that owns R and reticulate."""
    global _execution_loop
    if _execution_loop is None:
        _execution_loop = asyncio.new_event_loop()
    context, execute, future = _requests.get(timeout=30)
    try:
        # Keep tasks and loop-bound objects across cells. This loop advances
        # during Python cells only; it is not a notebook's continuously running
        # background event loop. Retain reticulate's process SIGINT handler.
        result = context.run(_execution_loop.run_until_complete, execute())
    except BaseException as error:
        future.set_exception(error)
        raise
    else:
        future.set_result(result)


class SharedKernel(IPythonKernel):
    def __init__(self, **kwargs: Any) -> None:
        # user_ns alone creates a facade whose actual module dict differs from
        # reticulate::py_run_string()'s globals. Preserve the real module too.
        super().__init__(user_module=sys.modules["__main__"], **kwargs)
        self.async_execution: (
            tuple[asyncio.AbstractEventLoop, asyncio.Future] | None
        ) = None

    def pre_handler_hook(self) -> None:
        # The transport thread cannot install signal handlers. Replacing
        # reticulate's handler on the R thread would also break interruption.
        pass

    def post_handler_hook(self) -> None:
        pass

    async def do_execute(self, *args: Any, **kwargs: Any) -> dict:
        execute = super().do_execute
        future: concurrent.futures.Future[dict] = concurrent.futures.Future()
        _requests.put_nowait(
            (
                contextvars.copy_context(),
                lambda: self.execute_on_main(execute, args, kwargs),
                future,
            )
        )
        return await asyncio.wrap_future(future)

    async def execute_on_main(
        self, execute: Callable, args: tuple, kwargs: dict
    ) -> dict:
        stdout, stderr = self.parent.python_streams
        stdout.set_parent(self.get_parent())
        stderr.set_parent(self.get_parent())
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            try:
                return await execute(*args, **kwargs)
            finally:
                stdout.flush()
                stderr.flush()

    @contextlib.contextmanager
    def _cancel_on_sigint(self, future: asyncio.Future) -> Iterator[None]:
        # ipykernel's two execution paths normally install/skip a signal
        # handler. Our control channel cancels an awaited coroutine directly.
        self.async_execution = asyncio.get_running_loop(), future
        try:
            yield
        finally:
            self.async_execution = None

    _dummy_context_manager = _cancel_on_sigint

    async def interrupt_request(self, stream: Any, ident: Any, parent: dict) -> None:
        pending = self.async_execution
        if pending is None:
            await super().interrupt_request(stream, ident, parent)
        else:
            loop, future = pending
            loop.call_soon_threadsafe(future.cancel)
            self.session.send(
                stream, "interrupt_reply", {"status": "ok"}, parent, ident=ident
            )


class SharedApp(IPKernelApp):
    kernel_class = Type(SharedKernel)

    def init_signal(self) -> None:
        pass

    def log_connection_info(self) -> None:
        # This endpoint belongs to Console; there is no independent frontend
        # to attach using ipykernel's printed connection instructions.
        pass

    def init_io(self) -> None:
        stdout, stderr, hook = sys.stdout, sys.stderr, sys.displayhook
        try:
            super().init_io()
            self.python_streams = sys.stdout, sys.stderr
        finally:
            # R-initiated Python output must continue through Ark. Install the
            # Python endpoint's streams only for its own paired execution.
            sys.stdout, sys.stderr, sys.displayhook = stdout, stderr, hook


def start(connection_file: str) -> None:
    if version_info < (7, 4):
        raise RuntimeError("Shared reticulate kernels require ipykernel >= 7.4")
    os.environ.setdefault("PYDEVD_DISABLE_FILE_VALIDATION", "1")
    app = SharedApp.instance(user_ns=sys.modules["__main__"].__dict__)

    def run() -> None:
        try:
            app.initialize(
                [
                    "-f",
                    connection_file,
                    "--IPKernelApp.capture_fd_output=False",
                    "--IPKernelApp.quiet=True",
                ]
            )
            app.start()
        except BaseException:
            traceback.print_exc(file=sys.__stderr__)
            os._exit(1)
        # Either endpoint ending ends the shared worker. A daemon thread must
        # not disappear and leave a live R process with unanswerable requests.
        os._exit(0)

    threading.Thread(target=run, name="console-python-kernel", daemon=True).start()
