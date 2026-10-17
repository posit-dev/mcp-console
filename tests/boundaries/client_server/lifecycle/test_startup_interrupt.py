#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import phase_progress, without_elapsed
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, PROCESS_EVENTS, command, requires
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@contextmanager
def before_resolver_spawn(
    binary: Path, execution: Execution, ordinal: int
) -> Iterator[tuple[McpClient, FifoCheckpoint, FifoCheckpoint, Path]]:
    # fmt: python
    server = code("""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_SPAWN_SERVER"] = str(os.getpid())
        os.environ["MCP_CONSOLE_TEST_SPAWN_CHILD"] = "1"
        os.environ["DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"] = (
            os.environ.pop("MCP_CONSOLE_TEST_SPAWN_LIBRARY")
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        started = FifoCheckpoint.create(root / "spawn-started")
        release = FifoCheckpoint.create(root / "spawn-release")
        resources.callback(started.close)
        resources.callback(release.close)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        (fake_bin / "ir").symlink_to(fixtures / "startup_ir")
        (fake_bin / "uv").symlink_to(fixtures / "startup_ir")
        (fake_bin / "python3").symlink_to(sys.executable)
        environment, _ = r_test_environment()
        real_ir = shutil.which("ir")
        real_uv = shutil.which("uv")
        assert real_ir is not None and real_uv is not None
        environment.update(
            {
                "TMPDIR": str(root),
                "UV_TOOL_DIR": str(root),
                "PATH": os.pathsep.join([str(fake_bin), environment["PATH"]]),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_REAL_UV": real_uv,
                "MCP_CONSOLE_TEST_STARTUP_PHASE": "none",
                "MCP_CONSOLE_TEST_STARTUP_RECORD": str(root / "resolver.jsonl"),
                "MCP_CONSOLE_TEST_SPAWN_LIBRARY": str(
                    build_interposer(root, "resolver_spawn_interposer")
                ),
                "MCP_CONSOLE_TEST_SPAWN_ARMED": str(root / "armed"),
                "MCP_CONSOLE_TEST_SPAWN_ORDINAL": str(ordinal),
                "MCP_CONSOLE_TEST_SPAWN_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_SPAWN_RELEASE": str(release.path),
            }
        )
        (root / "armed").touch()
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "cache": "host",
                    "resolver": {
                        "environment": {
                            LOADER_VARIABLE: environment[
                                "MCP_CONSOLE_TEST_SPAWN_LIBRARY"
                            ],
                            "MCP_CONSOLE_TEST_SPAWN_OWNER": "1",
                        }
                    },
                }
            )
        )
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                ("-c", server, str(binary), *execution.serve()),
                environment,
                root,
                response_timeout=5,
            )
        )
        try:
            client.initialize_and_list_tools()
            yield client, started, release, root
        finally:
            # Release the resolver fork before transport teardown, even when an
            # assertion fails while it is paused before child creation.
            (root / "armed").unlink(missing_ok=True)
            release.release()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_before_resolver_registration(
    binary: Path, execution: Execution
) -> Transcript:
    with before_resolver_spawn(binary, execution, 1) as (
        client,
        started,
        release,
        root,
    ):
        started.wait("first resolver has not been spawned", client=client)
        assert not (root / "resolver.jsonl").exists()
        # The preparation handle is registered, but its materializer is held
        # before process creation. Observe that owner before and after interrupt.
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        client.send(control="interrupt", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        (root / "armed").unlink()
        release.release()
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_between_resolver_phases(
    binary: Path, execution: Execution
) -> Transcript:
    with before_resolver_spawn(binary, execution, 2) as (
        client,
        started,
        release,
        root,
    ):
        started.wait("default preparation has not been spawned", client=client)
        invocations = [
            json.loads(line)
            for line in (root / "resolver.jsonl").read_text().splitlines()
        ]
        assert len(invocations) == 1, invocations
        assert invocations[0]["program"] == "uv", invocations
        assert invocations[0]["arguments"][:2] == ["python", "list"], invocations
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        client.send(control="interrupt", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        (root / "armed").unlink()
        release.release()
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES, command("ir"), command("uv"))
def test_interrupts_first_cell_admitted_during_stdin_startup(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with before_resolver_spawn(binary, execution, 1) as (
        client,
        started,
        release,
        root,
    ):
        started.wait("stdin startup has not spawned its first resolver", client=client)
        assert not (root / "resolver.jsonl").exists()
        client.send(stdin="old input\n", timeout_ms=0)
        assert last_tool_text(client) == (
            "\n[phase: dependency preparation]\n[worker starting]"
        )
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        client.send(control="interrupt", timeout_ms=0)
        assert phase_progress(last_tool_text(client)) == "dependency preparation"
        assert without_elapsed(last_tool_text(client)) == RUNNING
        (root / "armed").unlink()
        release.release()
        client.response_timeout = 600
        client.send(timeout_ms=600_000)
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert client.transcript[-1]["result"].get("isError") is not True, [
            entry for entry in client.transcript if "send" in entry
        ]
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
