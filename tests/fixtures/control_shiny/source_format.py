"""Format transcript display copies with host tools; never execute submitted code."""

import asyncio
import shutil
import tempfile
from collections import OrderedDict
from typing import Literal, TypedDict


class FormatResult(TypedDict):
    source: str
    status: Literal["formatted", "unavailable", "unsupported", "failed", "truncated"]
    message: str


class SourceFormatter:
    """One Shiny session's bounded cache and formatter process coordinator."""

    SOURCE_LIMIT = 64 * 1024
    RESULT_LIMIT = 256 * 1024
    CACHE_BYTES = 1024 * 1024
    CACHE_ENTRIES = 128
    TIMEOUT_SECONDS = 5

    def __init__(self) -> None:
        self._tools = {"r": shutil.which("air"), "python": shutil.which("ruff")}
        self._concurrency = asyncio.Semaphore(2)
        self._pending: dict[tuple[str, str, bool], asyncio.Task[FormatResult]] = {}
        self._cache: OrderedDict[tuple[str, str, bool], tuple[FormatResult, int]] = (
            OrderedDict()
        )
        self._cache_bytes = 0

    async def format(
        self, language: str, source: str, *, truncated: bool = False
    ) -> FormatResult:
        key = (language, source, truncated)
        if key in self._cache:
            result, _ = self._cache[key]
            self._cache.move_to_end(key)
            return result.copy()
        if key in self._pending:
            return (await self._pending[key]).copy()
        task = asyncio.create_task(self._format(language, source, truncated))
        self._pending[key] = task
        try:
            result = await task
            size = (
                len(source.encode())
                + len(result["source"].encode())
                + len(result["message"].encode())
            )
            if size <= self.CACHE_BYTES:
                self._cache[key] = (result, size)
                self._cache_bytes += size
                while (
                    len(self._cache) > self.CACHE_ENTRIES
                    or self._cache_bytes > self.CACHE_BYTES
                ):
                    _, (_, removed_size) = self._cache.popitem(last=False)
                    self._cache_bytes -= removed_size
            return result.copy()
        finally:
            self._pending.pop(key, None)

    async def _format(
        self, language: str, source: str, truncated: bool
    ) -> FormatResult:
        assert language in {"r", "python", "sql"}
        if truncated:
            return {
                "source": source,
                "status": "truncated",
                "message": "Captured source is truncated; showing original source.",
            }
        if language == "sql":
            return {
                "source": source,
                "status": "unsupported",
                "message": "SQL formatting is not configured; showing original source.",
            }
        if len(source.encode()) > self.SOURCE_LIMIT:
            return {
                "source": source,
                "status": "unavailable",
                "message": "Source exceeds the 64 KiB display-formatting limit; showing original source.",
            }
        name = "Air" if language == "r" else "Ruff"
        executable = self._tools[language]
        if executable is None:
            return {
                "source": source,
                "status": "unavailable",
                "message": f"{name} is unavailable on the Shiny host; showing original source.",
            }
        arguments = (
            (
                "format",
                "--no-configuration",
                "--stdin-file-path",
                "source.R",
                "--no-color",
            )
            if language == "r"
            else (
                "format",
                "--isolated",
                "--no-cache",
                "--no-preview",
                "--stdin-filename",
                "source.py",
                "--color",
                "never",
                "-",
            )
        )
        async with self._concurrency:
            try:
                process = await asyncio.create_subprocess_exec(
                    executable,
                    *arguments,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=tempfile.gettempdir(),
                )
            except OSError as error:
                return {
                    "source": source,
                    "status": "unavailable",
                    "message": f"{name} could not start; showing original source. {error}",
                }
            try:
                async with asyncio.timeout(self.TIMEOUT_SECONDS):
                    stdout, stderr = await process.communicate(source.encode())
            except TimeoutError:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                return {
                    "source": source,
                    "status": "failed",
                    "message": f"{name} formatting timed out; showing original source.",
                }
            except asyncio.CancelledError:
                if process.returncode is None:
                    process.kill()
                await process.communicate()
                raise
        if process.returncode != 0:
            diagnostic = stderr.decode(errors="replace").strip()
            if len(diagnostic) > 512:
                diagnostic = diagnostic[:512] + "…"
            return {
                "source": source,
                "status": "failed",
                "message": f"{name} could not format this source; showing original source. {diagnostic}".rstrip(),
            }
        if len(stdout) > self.RESULT_LIMIT:
            return {
                "source": source,
                "status": "unavailable",
                "message": f"{name} output exceeds the 256 KiB display-formatting limit; showing original source.",
            }
        return {
            "source": stdout.decode(),
            "status": "formatted",
            "message": f"Formatted with {name}.",
        }
