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


def _managed() -> bool:
    return os.getpid() == _pid and threading.get_ident() == _thread


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
    if not _managed():
        return _input(prompt)
    return readline(str(prompt))


def resolve_import(module: str, distribution: str) -> str:
    return resolve_import_request(
        json.dumps({"module": module, "distribution": distribution})
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
