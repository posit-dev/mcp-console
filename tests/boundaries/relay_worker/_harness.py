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
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.control import ControlClient
from support.execution import Execution
from support.normalization import code
from support.records import ToolResult, Transcript

CAPTURE_NAME = "mcp-console-worker-wire.jsonl"

CONTROL_NAME = "control.sock"

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
            execution.serve(
                "--worker", str(mitm), "--control-socket", str(root / CONTROL_NAME)
            ),
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
        # Raw pipes and the sideband are independent streams, and raw output
        # the server receives before cell entry is idle output. A frame the
        # proxy captured has not necessarily reached the server, so release
        # the raw-output producer only after the server confirms entry.
        # fmt: python
        source = code("""
            import os

            with open(os.environ["MCP_CONSOLE_MITM_OUTPUT_GATE"], "rb", buffering=0) as output_gate:
                assert output_gate.read(1) == b"1"
            """)
        source += python
        root = Path(self._temporary.name)
        with (
            ControlClient(root / CONTROL_NAME) as observer,
            closing(FifoCheckpoint.create(root / "output-gate")) as release,
        ):
            subscribed = observer.request(op="subscribe", format="events")
            assert subscribed.get("result") == {"subscribed": True}, subscribed
            deadline = self._client.response_deadline()
            pending = self._client.start_send(python=source)
            try:
                while True:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0, "server did not confirm cell entry"
                    snapshot = json.loads(observer.reader.readline(timeout=remaining))
                    if any(
                        evaluation["source"] == source and evaluation["started"]
                        for evaluation in snapshot["result"]["evaluations"]
                    ):
                        break
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
