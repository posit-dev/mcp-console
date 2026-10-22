import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.assertions import tool_text as _tool_text
from support.capture import read_jsonl, read_jsonl_path
from support.client import McpClient
from support.execution import Execution
from support.records import ToolResult, Transcript

CAPTURE_NAME = "mcp-console-worker-wire.jsonl"

CAPTURE_STDIN_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_STDIN_CLOSE"

CAPTURE_WORKER_SIDEBAND_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_WORKER_SIDEBAND_CLOSE"


class RelayWorkerClient:
    def __init__(
        self,
        binary: Path,
        *,
        execution: Execution,
        worker_command: tuple[str, ...] | None = None,
        capture_stdin_close: bool = False,
        capture_worker_sideband_close: bool = False,
        disable_r_segv_handler: bool = False,
    ) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        root = Path(self._temporary.name)
        environment = os.environ.copy()
        environment["TMPDIR"] = str(root)
        environment["MCP_CONSOLE_MITM_WORKER_COMMAND"] = json.dumps(
            (str(binary), "worker") if worker_command is None else worker_command
        )
        if capture_stdin_close:
            environment[CAPTURE_STDIN_CLOSE_ENV] = "1"
        if capture_worker_sideband_close:
            environment[CAPTURE_WORKER_SIDEBAND_CLOSE_ENV] = "1"
        if disable_r_segv_handler:
            environment["R_NO_SEGV_HANDLER"] = "1"
        mitm = (
            Path(__file__).resolve().parents[2]
            / "fixtures"
            / "relay_worker"
            / "proxy.py"
        )
        self._client = McpClient(
            binary,
            execution.serve("--worker", str(mitm)),
            environment,
        )
        self._client.initialize_and_list_tools()

    def send(self, **arguments: object) -> ToolResult:
        return self._client.send(**arguments)

    def _open_capture(self) -> tuple[Path, TextIO]:
        capture = self._capture_path()
        return capture, capture.open(encoding="utf-8")

    def _collect_output(self, output: str, expected_size: int) -> str:
        chunks = [] if output == "[done]" else [output]
        deadline = time.monotonic() + 10
        while sum(map(len, chunks)) < expected_size:
            assert time.monotonic() < deadline, sum(map(len, chunks))
            polled = _tool_text(self.send())
            assert polled.endswith("\n[idle]"), repr(polled)
            chunks.append(polled.removesuffix("\n[idle]"))
        output = "".join(chunks)
        assert len(output) == expected_size, len(output)
        return output

    def finish(self) -> Transcript:
        transcript = self._read_capture(self._capture_path())
        self._client.finish()
        self._temporary.cleanup()
        return transcript

    def _finish_replacement(
        self,
        old_path: Path,
        old_capture: TextIO,
    ) -> Transcript:
        transcript = self._read_open_capture(old_capture)
        transcript.extend(self._read_capture(self._capture_path(excluding=old_path)))
        old_capture.close()
        self._client.finish()
        self._temporary.cleanup()
        return transcript

    def _capture_path(self, excluding: Path | None = None) -> Path:
        root = Path(self._temporary.name)
        captures = [path for path in root.rglob(CAPTURE_NAME) if path != excluding]
        assert len(captures) == 1, captures
        return captures[0]

    @staticmethod
    def _read_capture(capture: Path) -> Transcript:
        return read_jsonl_path(capture)

    @staticmethod
    def _read_open_capture(capture: TextIO) -> Transcript:
        return read_jsonl(capture)
