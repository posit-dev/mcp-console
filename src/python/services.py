"""Managed console hooks; other contexts retain their original streams."""

from __future__ import annotations

import builtins
from importlib.machinery import PathFinder
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
    return os.getpid() == _pid and (
        threading.get_ident() == _thread or bool(priority_active())
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
    if priority_active():
        raise RuntimeError("stdin is unavailable during priority execution")
    if not _managed():
        return _input(prompt)
    return readline(str(prompt))


def eval_priority(source: str, filename: str) -> None:
    import _mcp_console

    _mcp_console.eval_cell(source, filename, finalize=False, propagate_exit=False)


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


class _Rpy2Loader:
    def __init__(self, loader):
        self.loader = loader

    def __getattr__(self, name):
        return getattr(self.loader, name)

    def create_module(self, specification):
        # Native profiles may import this interface themselves. Finish startup
        # before importlib publishes an empty module in sys.modules.
        ensure_r_ready()
        self.loaded = sys.modules.get(specification.name)
        if self.loaded is not None:
            return self.loaded
        return self.loader.create_module(specification)

    def exec_module(self, module):
        module.__spec__.loader = module.__loader__ = self.loader
        if self.loaded is None:
            self.loader.exec_module(module)


class _Rpy2Import:
    @staticmethod
    def find_spec(fullname, path=None, target=None):
        if fullname not in {"rpy2.rinterface", "rpy2.robjects"} or os.getpid() != _pid:
            return None
        specification = PathFinder.find_spec(fullname, path, target)
        if specification is not None and specification.loader is not None:
            specification.loader = _Rpy2Loader(specification.loader)
        return specification


def guard_rpy2_import():
    # Let imports from sitecustomize and ordinary cells use Console's R.
    # A find_spec availability probe does not start an interpreter.
    sys.meta_path.insert(sys.meta_path.index(PathFinder), _Rpy2Import)


def set_r_installation_path(name: str, path: str) -> None:
    os.environ[name] = path


def publish_r_session():
    # CPython caches os.environ even when a native library sets a variable.
    os.environ["R_SESSION_INITIALIZED"] = f'PID={_pid}:NAME="reticulate"'
    if _Rpy2Import in sys.meta_path:
        sys.meta_path.remove(_Rpy2Import)


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
