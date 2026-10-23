#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires


@requires(POSIX, NATIVE_FIXTURES)
def test_eof_cancels_preparation_after_inventory_collection(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        collected, controlled, release = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("collected", "controlled", "release")
        ]
        uv = root / "uv"
        uv.write_text("#!/bin/sh\nprintf '[]\\n'\n")
        uv.chmod(0o755)
        environment = dict(os.environ, PATH=str(root), RETICULATE_PYTHON="managed")
        environment.pop("R_HOME", None)
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "preparation_collected_cancellation")
                ),
                "MCP_CONSOLE_TEST_INVENTORY_COLLECTED": str(collected.path),
                "MCP_CONSOLE_TEST_INVENTORY_CONTROLLED": str(controlled.path),
                "MCP_CONSOLE_TEST_INVENTORY_RELEASE": str(release.path),
            }
        )
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                collected.wait(
                    "successful Python inventory collected before control check"
                )
                client.stdin.close()
                controlled.wait("EOF cancellation acknowledged after collection")
                release.release()
                exit_status = client.process.wait(timeout=10)
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=exit_status
                )
                assert exit_status == 0, {
                    "exit_status": exit_status,
                    "stderr": stderr,
                }
                assert stderr == "", stderr
                return transcript + [{"exit_status": exit_status, "stderr": stderr}]
            finally:
                release.release()


@requires(POSIX, NATIVE_FIXTURES)
def test_setup_failure_survives_later_eof_cancellation(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        captured, controlled, release = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("captured", "controlled", "release")
        ]
        selected = root / "selected-python"
        selected.write_text(
            "#!/bin/sh\nprintf 'captured setup failure\\n' >&2\nexit 23\n"
        )
        selected.chmod(0o755)
        environment = dict(os.environ, PATH=str(root), RETICULATE_PYTHON=str(selected))
        environment.pop("R_HOME", None)
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "preparation_failure_retirement")
                ),
                "MCP_CONSOLE_TEST_FAILURE_CAPTURED": str(captured.path),
                "MCP_CONSOLE_TEST_FAILURE_EXECUTABLE": str(selected),
                "MCP_CONSOLE_TEST_FAILURE_CONTROLLED": str(controlled.path),
                "MCP_CONSOLE_TEST_FAILURE_RELEASE": str(release.path),
            }
        )
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                captured.wait(
                    "genuine inspection error captured before terminal report"
                )
                client.stdin.close()
                controlled.wait(
                    "later EOF cancellation acknowledged for the failed operation"
                )
                release.release()
                exit_status = client.process.wait(timeout=10)
                assert exit_status == 1, {
                    "exit_status": exit_status,
                    "stderr": client.stderr.buffer.decode(),
                }
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert stderr == (
                    "selected Python inspection failed (exit status: 23): captured setup failure\n\n"
                ), stderr
                return transcript + [{"exit_status": 1, "stderr": stderr}]
            finally:
                release.release()
