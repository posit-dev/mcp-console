"""Main-thread console hooks; other contexts retain their original streams."""

from __future__ import annotations

import builtins
import json
import os
import signal
import sys
import threading
from typing import Any, Callable, TextIO

_pid = os.getpid()
_thread = threading.get_ident()
_input = builtins.input
_priority = threading.local()


def _managed() -> bool:
    return os.getpid() == _pid and (
        threading.get_ident() == _thread or getattr(_priority, "active", False)
    )


class _Output:
    def __init__(self, target: TextIO, publish: Callable[[str], None]) -> None:
        self.target = target
        self.publish = publish

    def write(self, text: str) -> int:
        if not _managed():
            return self.target.write(text)
        self.publish(text)
        return len(text)

    def flush(self) -> None:
        if not _managed():
            return self.target.flush()

    def isatty(self) -> bool:
        return True if _managed() else self.target.isatty()

    def close(self) -> None:
        return None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.target, name)


def _console_input(prompt: object = "") -> str:
    if getattr(_priority, "active", False):
        raise RuntimeError("stdin is unavailable during priority execution")
    if not _managed():
        return _input(prompt)
    return readline(str(prompt))


def eval_priority(source: str, filename: str) -> None:
    import _mcp_console

    _priority.active = True
    try:
        _mcp_console.eval_cell(source, filename, finalize=False, propagate_exit=False)
    finally:
        _priority.active = False


def resolve_import(module: str, distribution: str) -> str:
    return resolve_import_request(
        json.dumps({"module": module, "distribution": distribution})
    )


def report_execution_error(
    error: BaseException, message: str, traceback: list[str] | None = None
) -> None:
    # Reuse TracebackException's captured message. Calling str(error) again can
    # execute user code and replace the exception while reporting it.
    execution_error(
        json.dumps(
            {
                "ename": type(error).__name__,
                "evalue": message,
                "traceback": [] if traceback is None else traceback,
            }
        )
    )


def install_interrupt() -> None:
    signal.signal(signal.SIGINT, interrupt)


# Reticulate output remapping is disabled. Install these wrappers once so
# restoring input and interrupt hooks preserves user stream redirections.
sys.stdout = _Output(sys.stdout, write)
sys.stderr = _Output(sys.stderr, diagnostic)


def install_services() -> None:
    builtins.input = _console_input
    install_interrupt()


install_services()


class _LazyR:
    @staticmethod
    def _bridge() -> Any:
        attach_r()
        import __main__

        bridge = __main__.__dict__.get("r", builtins.r)
        if isinstance(bridge, _LazyR):
            raise RuntimeError("reticulate did not install Python-side R access")
        return bridge

    def __getattr__(self, name: str) -> Any:
        return getattr(self._bridge(), name)

    def __getitem__(self, code: str) -> Any:
        return self._bridge()[code]

    def __setattr__(self, name: str, value: Any) -> None:
        setattr(self._bridge(), name, value)

    def __setitem__(self, name: str, value: Any) -> None:
        self._bridge()[name] = value


builtins.r = _LazyR()


# Import while no_site is set, keeping site processing explicit and retryable.
import site as _site


def initialize_site() -> None:
    _site.main()
