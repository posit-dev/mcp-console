import json
import os
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path
from typing import Self, TextIO

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.assertions import tool_text as _tool_text
from support.capture import read_jsonl, read_jsonl_path
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.client import McpClient
from support.events import Events
from support.execution import Execution
from support.normalization import code
from support.records import ToolResult, Transcript

CAPTURE_NAME = "mcp-console-worker-wire.jsonl"

CAPTURE_STDIN_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_STDIN_CLOSE"

CAPTURE_WORKER_SIDEBAND_CLOSE_ENV = "MCP_CONSOLE_MITM_CAPTURE_WORKER_SIDEBAND_CLOSE"


class RelayWorkerClient:
    """Capture the relay/worker seam through a custom-worker proxy.

    Built-in worker cases using R need reticulate in the inherited R library.
    The custom-worker launch bypasses Console's host runtime preparation.
    """

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
        environment["MCP_CONSOLE_MITM_OUTPUT_GATE"] = str(root / "output-gate")
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
        try:
            self._client.initialize_and_list_tools()
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception: object) -> None:
        self.close()

    def close(self) -> None:
        try:
            self._client.close()
        finally:
            self._temporary.cleanup()

    def send(self, **arguments: object) -> ToolResult:
        return self._client.send(**arguments)

    def send_gated_python(self, python: str) -> ToolResult:
        # Raw pipes and the sideband are independent streams. Observe the
        # current cell_started receipt before releasing its raw-output producer.
        # fmt: python
        source = code("""
            import os

            with open(os.environ["MCP_CONSOLE_MITM_OUTPUT_GATE"], "rb", buffering=0) as output_gate:
                assert output_gate.read(1) == b"1"
            """)
        source += python
        root = Path(self._temporary.name)

        def entered() -> Path | None:
            for capture in root.rglob(CAPTURE_NAME):
                events.watch_file(capture)
                submitted = False
                # The proxy can still be appending the final UTF-8/JSON frame.
                for line in capture.read_bytes().split(b"\n")[:-1]:
                    event = json.loads(line)
                    if event == {
                        "relay": {
                            "kind": "evaluate",
                            "language": "python",
                            "source": source,
                        }
                    }:
                        submitted = True
                    elif submitted and event == {"worker": {"kind": "cell_started"}}:
                        return capture
            return None

        with (
            Events() as events,
            closing(FifoCheckpoint.create(root / "output-gate")) as release,
        ):
            deadline = self._client.response_deadline()
            pending = self._client.start_send(python=source)
            try:
                wait_for_checkpoint(
                    entered,
                    "proxy captured the raw-output cell entry",
                    root=root,
                    recursive=True,
                    client=self._client,
                    timeout=max(0, deadline - time.monotonic()),
                    events=events,
                )
            finally:
                release.release()
            self._client.receive(pending)
            return self._client.expect(None, received=pending)

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
