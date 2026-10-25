#!/usr/bin/env -S uv run --script
"""MCP closure preserves setup failures and retires unfinished preparation."""

import os
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.evidence import compact_text
from support.native import LOADER_VARIABLE, build_interposer
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PTHREAD_RUNTIME_PARKING,
    requires,
)
from support.suites import run_this_suite

SETUP_FAILURE = (
    "resolver.environment: preparation requires an absolute HOME or "
    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY; configure one when "
    "inherit_environment is false"
)


def test_eof_preserves_queued_first_send_response(binary: Path) -> Transcript:
    # Argument validation does not need a worker, even if EOF cancels startup.
    with McpClient(
        binary,
        ("serve", "--no-sandbox", "--worker", str(binary), "--relay", str(binary)),
    ) as client:
        client.initialize_and_list_tools()
        source = "1" + " " * 131072
        call = client.start_send(r=source, python="1")
        client.stdin.close()
        client.receive(call)
        assert call["result"] == {
            "content": [
                {
                    "type": "text",
                    "text": "only one of `r`, `python`, or `sql` may be supplied",
                }
            ],
            "isError": True,
        }, call
        transcript, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr
        call["send"]["r"] = compact_text(source, " ")
        return transcript + [{"exit_status": 0, "stderr": stderr}]


@requires(POSIX, NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
def test_eof_waits_for_input_queued_before_response_admission(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as directory, ExitStack() as resources:
        root = Path(directory)
        selected = root / "stage-python"
        selected.symlink_to(sys.executable)
        checkpoints = {
            name: resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("reached", "release", "eof", "input-reached")
        }
        environment = dict(os.environ, PATH=str(root), RETICULATE_PYTHON=selected.name)
        environment.pop("R_HOME", None)
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_stage_checkpoint")
                ),
                "MCP_CONSOLE_TEST_STARTUP_SELECTION": str(selected),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_REACHED": str(
                    checkpoints["reached"].path
                ),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_RELEASE": str(
                    checkpoints["release"].path
                ),
                "MCP_CONSOLE_TEST_STARTUP_EOF": str(checkpoints["eof"].path),
                "MCP_CONSOLE_TEST_QUEUED_INPUT_REACHED": str(
                    checkpoints["input-reached"].path
                ),
            }
        )
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                checkpoints["reached"].wait("Python inspection is not registered")
                call = client.start_send(r="queued-EOF", python="1")
                checkpoints["input-reached"].wait(
                    "request read before transport admission"
                )
                client.stdin.close()
                checkpoints["release"].release()
                client.receive(call)
                assert call["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "only one of `r`, `python`, or `sql` may be supplied",
                        }
                    ],
                    "isError": True,
                }, call
                transcript, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
                return transcript + [{"exit_status": 0, "stderr": stderr}]
            finally:
                checkpoints["release"].release()


@requires(POSIX, NATIVE_FIXTURES, PTHREAD_RUNTIME_PARKING)
def test_eof_before_retry_admission_exits_cleanly(binary: Path) -> Transcript:
    with TemporaryDirectory(dir="/tmp") as directory, ExitStack() as resources:
        root = Path(directory)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "admission-reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "admission-release"))
        )
        shutdown = resources.enter_context(
            closing(FifoCheckpoint.create(root / "shutdown"))
        )
        environment = dict(
            os.environ,
            **{
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_admission_checkpoint")
                ),
                "MCP_CONSOLE_TEST_ADMISSION_REACHED": str(reached.path),
                "MCP_CONSOLE_TEST_ADMISSION_RELEASE": str(release.path),
                "MCP_CONSOLE_TEST_ADMISSION_SHUTDOWN": str(shutdown.path),
            },
        )
        with McpClient(
            binary,
            ("serve", "-c", "cache=host", "-c", "resolver.inherit_environment=false"),
            environment,
            root,
        ) as client:
            try:
                client.initialize_and_list_tools()
                failure = client.send(session="default")
                assert failure["isError"] is True, failure
                assert SETUP_FAILURE in str(failure), failure
                restart = client.start_send(control="restart", timeout_ms=0)
                reached.wait("retry started before worker/resolver admission")
                client.receive(restart)
                assert not restart["result"].get("isError"), restart
                client.stdin.close()
                shutdown.wait("EOF dispatched; shutdown awaits pending admission")
                release.release()
                transcript, stderr = client.finish_with_standard_error()
                assert stderr == "", stderr
                return transcript + [{"exit_status": 0, "stderr": stderr}]
            finally:
                release.release()


@requires(POSIX)
def test_eof_preserves_completed_setup_failure_without_resolver(
    binary: Path,
) -> Transcript:
    with McpClient(
        binary,
        ("serve", "-c", "cache=host", "-c", "resolver.inherit_environment=false"),
    ) as client:
        client.initialize_and_list_tools()
        failure = client.send(python="raise AssertionError('must not execute')")
        assert failure["isError"] is True, failure
        assert SETUP_FAILURE in str(failure), failure
        assert client.request("tools/list")["result"] == client.transcript[2]["result"]
        client.transcript[-1] = {"tools_schema_unchanged": True}
        transcript, stderr = client.finish_with_standard_error(expected_exit_status=1)
        assert stderr == SETUP_FAILURE + "\n", stderr
        return transcript + [{"exit_status": 1, "stderr": stderr}]


@contextmanager
def closed_between_stages(
    binary: Path,
) -> Iterator[tuple[McpClient, Path]]:
    with TemporaryDirectory(dir="/tmp") as directory, ExitStack() as resources:
        root = Path(directory)
        selected = root / "stage-python"
        selected.symlink_to(sys.executable)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        release = resources.enter_context(
            closing(FifoCheckpoint.create(root / "release"))
        )
        eof = resources.enter_context(closing(FifoCheckpoint.create(root / "eof")))
        environment = dict(os.environ, PATH=str(root), RETICULATE_PYTHON=selected.name)
        environment.pop("R_HOME", None)
        environment.update(
            {
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_stage_checkpoint")
                ),
                "MCP_CONSOLE_TEST_STARTUP_SELECTION": str(selected),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_REACHED": str(reached.path),
                "MCP_CONSOLE_TEST_STARTUP_STAGE_RELEASE": str(release.path),
                "MCP_CONSOLE_TEST_STARTUP_EOF": str(eof.path),
            }
        )
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            try:
                client.initialize_and_list_tools()
                reached.wait("discovery completed; Python inspection not registered")
                client.stdin.close()
                eof.wait("MCP reader received EOF while inspection is withheld")
                release.release()
                yield client, root
            finally:
                release.release()


@requires(POSIX, NATIVE_FIXTURES)
def test_eof_between_discovery_and_python_inspection_exits_cleanly(
    binary: Path,
) -> list:
    with closed_between_stages(binary) as (client, _):
        _, stderr = client.finish_with_standard_error()
        assert stderr == "", stderr
        return [
            {
                "closed_between": ["discovery", "Python inspection"],
                "exit_status": 0,
                "stderr": stderr,
            }
        ]


@requires(POSIX, NATIVE_FIXTURES)
def test_eof_during_inspection_preserves_unconfirmed_preparation_close(
    binary: Path,
) -> list:
    with TemporaryDirectory(dir="/tmp") as directory, ExitStack() as resources:
        root = Path(directory)
        reached = resources.enter_context(
            closing(FifoCheckpoint.create(root / "reached"))
        )
        resources.enter_context(closing(FifoCheckpoint.create(root / "release")))
        eof = resources.enter_context(closing(FifoCheckpoint.create(root / "eof")))
        selected = root / "stage-python"
        selected.write_text(
            '#!/bin/sh\nprintf 1 > "$MCP_CONSOLE_TEST_INSPECTION_REACHED"\n'
            'read token < "$MCP_CONSOLE_TEST_INSPECTION_RELEASE"\n'
        )
        selected.chmod(0o755)
        environment = dict(
            os.environ,
            PATH=str(root),
            RETICULATE_PYTHON=str(selected),
            **{
                LOADER_VARIABLE: str(
                    build_interposer(root, "startup_stage_checkpoint")
                ),
                "MCP_CONSOLE_TEST_STARTUP_SELECTION": str(selected),
                "MCP_CONSOLE_TEST_STARTUP_EOF": str(eof.path),
                "MCP_CONSOLE_TEST_STARTUP_LOSE_CLOSE": "1",
                "MCP_CONSOLE_TEST_STARTUP_CLOSE_LOST": str(root / "close-lost"),
                "MCP_CONSOLE_TEST_INSPECTION_REACHED": str(reached.path),
                "MCP_CONSOLE_TEST_INSPECTION_RELEASE": str(root / "release"),
            },
        )
        environment.pop("R_HOME", None)
        with McpClient(binary, ("serve", "--no-sandbox"), environment, root) as client:
            client.initialize_and_list_tools()
            reached.wait("Python inspection admitted before MCP closure")
            client.stdin.close()
            _, stderr = client.finish_with_standard_error(expected_exit_status=1)
            assert (root / "close-lost").exists(), stderr
            assert (
                stderr
                == "Python inspection resolution cancelled; resolver input closed\n"
            ), stderr
            return [{"close_receipt_lost": True, "exit_status": 1, "stderr": stderr}]


if __name__ == "__main__":
    run_this_suite(__file__)
