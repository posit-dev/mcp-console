#!/usr/bin/env -S uv run --script

from __future__ import annotations

import json
import os
import select
import shutil
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import phase_progress, without_elapsed
from support.requirements import (
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    R,
    SQL,
    command,
    requires,
)
from support.assertions import (
    collect_running_output,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    ProcessIdentity,
    capture_process_identity,
    host_process_id,
    kill_processes,
)
from support.normalization import code
from support.native import LOADER_VARIABLE, build_interposer
from support.r import r_test_environment
from support.events import Events
from support.records import Transcript
from support.suites import run_this_suite

RUNNING = "\n[running; poll with an empty send]"


@dataclass
class StartupFixture:
    client: McpClient
    root: Path
    started: FifoCheckpoint
    release: FifoCheckpoint
    exits: Events
    identities: list[ProcessIdentity] = field(default_factory=list)

    def wait_for_resolver(self) -> None:
        self.started.wait("first-use resolver")
        identity = self.root / "identity"
        pids = {
            host_process_id(int(pid), self.client.process.pid)
            for pid in identity.read_text(encoding="utf-8").split()
        }
        assert len(pids) == 2, pids
        self.identities = [capture_process_identity(pid) for pid in pids]
        for pid in pids:
            self.exits.watch_process(pid)

    def wait_for_resolver_exit(self) -> None:
        pending = {identity[0] for identity in self.identities}
        deadline = time.monotonic() + 5
        while pending:
            remaining = deadline - time.monotonic()
            assert remaining > 0, f"resolver processes did not exit: {pending}"
            observed = self.exits.wait(remaining)
            assert observed, f"resolver processes did not exit: {pending}"
            pending.difference_update(observed)

    def invocations(self) -> list[dict[str, object]]:
        record = self.root / "resolver.jsonl"
        if not record.exists():
            return []
        return [json.loads(line) for line in record.read_text().splitlines()]


@contextmanager
def startup_fixture(
    binary: Path,
    execution: Execution,
    *,
    bootstrap: str = "ir",
    phase: str = "all",
    server_environment: dict[str, str] | None = None,
) -> Iterator[StartupFixture]:
    with tempfile.TemporaryDirectory() as directory, Events() as exits:
        temporary = Path(directory)
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        fixtures = Path(__file__).resolve().parents[3] / "fixtures"
        (fake_bin / bootstrap).symlink_to(fixtures / "startup_ir")
        if bootstrap == "ir":
            # Gate ir while keeping Python preparation on the available uv.
            # Concurrent cases must not bootstrap uv into reticulate's shared cache.
            (fake_bin / "uv").symlink_to(fixtures / "startup_ir")
        (fake_bin / "python3").symlink_to(sys.executable)
        started = FifoCheckpoint.create(temporary / "started")
        release = FifoCheckpoint.create(temporary / "release")
        environment, _ = r_test_environment()
        real_ir = shutil.which("ir")
        real_uv = shutil.which("uv")
        assert real_ir is not None and real_uv is not None
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join(
            [str(fake_bin)]
            + [
                entry
                for entry in path.split(os.pathsep)
                if not any((Path(entry) / name).exists() for name in ("ir", "uv"))
            ]
        )
        environment.pop("RETICULATE_UV", None)
        environment.pop("RETICULATE_PYTHON", None)
        environment.update(
            {
                "TMPDIR": str(temporary),
                "UV_TOOL_DIR": str(temporary),
                "MCP_CONSOLE_TEST_REAL_IR": real_ir,
                "MCP_CONSOLE_TEST_REAL_UV": real_uv,
                "MCP_CONSOLE_TEST_STARTUP_PHASE": phase,
                "MCP_CONSOLE_TEST_STARTUP_CLAIM": str(temporary / "gate-claimed"),
                "MCP_CONSOLE_TEST_STARTUP_IDENTITY": str(temporary / "identity"),
                "MCP_CONSOLE_TEST_STARTUP_RECORD": str(temporary / "resolver.jsonl"),
                "MCP_CONSOLE_TEST_STARTUP_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_STARTUP_RELEASE": str(release.path),
            }
        )
        environment.update(server_environment or {})
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            response_timeout=5,
        )
        fixture = StartupFixture(client, temporary, started, release, exits)
        try:
            yield fixture
        finally:
            # An early handshake failure can leave the resolver gated before
            # the test gets to observe it. Capture its identity before cleanup.
            if (
                not fixture.identities
                and select.select([started.descriptor], [], [], 0)[0]
            ):
                fixture.wait_for_resolver()
            try:
                client.close()
            finally:
                kill_processes(fixture.identities)
                started.close()
                release.close()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_preserves_initialize_buffered_during_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution) as fixture:
        client = fixture.client
        fixture.wait_for_resolver()
        invocations = fixture.invocations()
        client.initialize_and_list_tools()
        client.request("ping")
        client.send(session="default", timeout_ms=0)
        assert last_tool_text(client) == (
            "\n[phase: dependency preparation]\n[worker starting]"
        )
        client.send(r="must not run", requirements={"r": [""]})
        assert client.transcript[-1]["result"]["isError"] is True
        assert fixture.invocations() == invocations, (
            "poll or invalid input duplicated startup"
        )
        # The resolver owns one native temp directory; a worker would add another.
        storage = list(fixture.root.glob("sandbox-*"))
        assert len(storage) == (1 if execution is SANDBOXED else 0), storage
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_initializes_before_uv_bootstrap_installation(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, bootstrap="uv") as fixture:
        fixture.wait_for_resolver()
        fixture.client.initialize_and_list_tools()
        fixture.client.request("ping")
        return fixture.client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_prepares_python_before_r_bootstrap_validation(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="discovery") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        fixture.wait_for_resolver()
        # fmt: r
        r = code(r"""
            cat("ready\n")
            """)
        client.send(r=r, timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        assert fixture.invocations()[-1] == {
            "program": "ir",
            "arguments": ["--version"],
        }
        assert any(
            invocation["program"] == "uv"
            and invocation["arguments"][:2] == ["tool", "run"]
            for invocation in fixture.invocations()
        ), "Python preparation waited for R bootstrap validation"
        fixture.release.release()
        client.response_timeout = 600
        assert collect_running_output(client, "first cell", timeouts_ms=(600_000,)) == (
            "ready\n",
        )
        fixture.wait_for_resolver_exit()
        return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_first_cell_prepares_defaults_after_running_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        fixture.wait_for_resolver()
        # fmt: r
        r = code(r"""
            defaults <- c(
              "tidyverse",
              "dplyr",
              "dbplyr",
              "reticulate",
              "DBI",
              "duckdb",
              "nanoarrow",
              "yyjsonr",
              "ggplot2"
            )
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            stopifnot(all(defaults %in% list.files(.libPaths()[[managed_index]])))
            stopifnot(identical(
              reticulate::py_require()$packages,
              c("numpy", "pandas", "matplotlib", "plotnine")
            ))
            cat("scientific defaults ready\n")
            """)
        client.send(r=r, timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        storage = list(fixture.root.glob("sandbox-*"))
        assert len(storage) == (1 if execution is SANDBOXED else 0), storage
        assert any(
            invocation["program"] == "uv"
            and invocation["arguments"][:2] == ["tool", "run"]
            for invocation in fixture.invocations()
        ), "Python defaults waited for the managed R library"
        preparation = fixture.invocations()[-1]["arguments"]
        assert isinstance(preparation, list)
        assert {
            preparation[index + 1]
            for index, argument in enumerate(preparation[:-1])
            if argument == "--with"
        } == {
            "tidyverse",
            "dplyr",
            "dbplyr",
            "reticulate",
            "DBI",
            "duckdb",
            "nanoarrow",
            "yyjsonr",
            "ggplot2",
            "jsonlite",
            "pillar",
            "tibble",
            "utf8",
        }, preparation
        client.send(session="default", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        fixture.release.release()
        client.response_timeout = 600
        assert collect_running_output(client, "first cell", timeouts_ms=(600_000,)) == (
            "scientific defaults ready\n",
        )
        fixture.wait_for_resolver_exit()
        # fmt: python
        python = code("""
            import numpy, pandas

            print("Python defaults ready")
            """)
        client.send(python=python)
        assert last_tool_text(client) == "Python defaults ready\n"
        sql = code("""
            SELECT extension_name
            FROM duckdb_extensions()
            WHERE installed AND extension_name IN ('icu', 'json')
            ORDER BY extension_name
            """)
        client.send(sql=sql)
        assert {'"icu"', '"json"'} <= set(last_tool_text(client).split()), (
            client.transcript[-1]
        )
        assert (
            len(
                [
                    invocation
                    for invocation in fixture.invocations()
                    if invocation["arguments"][0] == "run"
                ]
            )
            == 1
        ), fixture.invocations()
        assert any(
            invocation["program"] == "uv" for invocation in fixture.invocations()
        ), "Python preparation bypassed the fixture's uv executable"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_explicit_preparation_keeps_its_wait_precondition(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        preparation = client.start_send(requirements={"r": ["DBI"]})
        fixture.wait_for_resolver()
        client.request("ping")
        assert "result" not in preparation, (
            "explicit preparation returned before resolution"
        )
        storage = list(fixture.root.glob("sandbox-*"))
        assert len(storage) == (1 if execution is SANDBOXED else 0), storage
        fixture.release.release()
        client.response_timeout = 600
        client.receive(preparation)
        assert preparation["result"] == {
            "content": [{"type": "text", "text": "[prepared]"}],
            "isError": False,
        }
        fixture.wait_for_resolver_exit()
        client.send(r="42L")
        assert last_tool_text(client) == "[1] 42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_cancels_resolver_discovery_when_stdin_closes(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="discovery") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        # Bootstrap discovery is held inside the default preparation operation.
        fixture.wait_for_resolver()
        client.send(r="42L", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        fixture.wait_for_resolver_exit()
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_cancels_default_preparation_when_stdin_closes(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        # Hold preparation before admission so the observed phase is fixed.
        fixture.wait_for_resolver()
        client.send(r="42L", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        client.stdin.close()
        exit_code = client.process.wait(timeout=5)
        fixture.wait_for_resolver_exit()
        return [
            *client.transcript,
            {
                "exit_code": exit_code,
                "stdout": client.stdout.read(),
                "stderr": client.stderr.read(),
            },
        ]


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"), command("uv"))
def test_interrupts_first_use_preparation_without_running_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        client.initialize_and_list_tools()
        # Hold preparation before admission so the observed phase is fixed.
        fixture.wait_for_resolver()
        client.send(r="startup_cell_ran <- TRUE", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            last_tool_text(client)
        )
        client.send(control="interrupt", timeout_ms=30_000)
        fixture.wait_for_resolver_exit()
        client.response_timeout = 600
        client.send(
            r='exists("startup_cell_ran", inherits = FALSE)', timeout_ms=600_000
        )
        assert last_tool_text(client) == "[1] FALSE\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, PROCESS_EVENTS, command("ir"), command("uv"))
def test_restart_replaces_first_use_cell_and_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    with ExitStack() as resources:
        root = Path(resources.enter_context(tempfile.TemporaryDirectory()))
        completion_started = FifoCheckpoint.create(root / "completion-started")
        release = FifoCheckpoint.create(root / "release")
        parked = FifoCheckpoint.create(root / "parked")
        for checkpoint in (completion_started, release, parked):
            resources.callback(checkpoint.close)
        armed = root / "armed"
        environment = {
            LOADER_VARIABLE: str(build_interposer(root, "startup_return_interposer")),
            "MCP_CONSOLE_TEST_COMPLETION_ARMED": str(armed),
            "MCP_CONSOLE_TEST_COMPLETION_STARTED": str(completion_started.path),
            "MCP_CONSOLE_TEST_COMPLETION_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_COMPLETION_PARKED": str(parked.path),
        }
        fixture = resources.enter_context(
            startup_fixture(
                binary, execution, phase="preparation", server_environment=environment
            )
        )
        resources.callback(release.release)
        client = fixture.client
        client.initialize_and_list_tools()
        # Hold preparation before admission so the first observed phase is fixed.
        fixture.wait_for_resolver()
        client.send(python="startup_cell_ran = True", stdin="old input\n", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert phase_progress(last_tool_text(client)) == "dependency preparation", (
            client.transcript[-1]
        )
        armed.touch()
        client.response_timeout = 600
        # fmt: python
        python = code("""
            assert "startup_cell_ran" not in globals()
            assert input() == "replacement input"
            print("replacement only")
            """)
        # Withhold the newline so managed input must report waiting before
        # the replacement can complete, regardless of the input exposure grace.
        replacement = client.start_send(
            control="restart",
            python=python,
            stdin="replacement input",
            timeout_ms=600_000,
        )
        # Hold the cancelled initializer after it closes its input watcher,
        # before its blocking task returns. The replacement must remain usable
        # while the old startup outcome is still pending.
        completion_started.wait("cancelled startup closed its completion socket")
        client.receive(replacement)
        assert last_tool_text(client) == code("""
            [active evaluation stopped by session restart request]
            [starting new worker]
            [input requested: ""]
            [waiting for stdin]
            """).removesuffix("\n")
        wait_for_evaluation_output(
            client,
            "replacement only\n[done]",
            "replacement managed input",
            controlled_completion=True,
            stdin="\n",
        )
        assert last_tool_text(client).count("replacement only\n") == 1, (
            client.transcript[-1]
        )
        # The replacement's active cell cannot borrow the still-pending initial
        # startup observation, even before its late outcome reaches the server.
        new_cell_release = resources.enter_context(
            closing(
                FifoCheckpoint.create(
                    Path(client.temporary_directory.name) / "new-cell-release"
                )
            )
        )
        resources.callback(new_cell_release.release)
        # fmt: python
        python = code("""
            with open("new-cell-release", "rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            input("replacement> ")
            """)
        client.send(python=python, timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert "phase:" not in last_tool_text(client)
        fixture.wait_for_resolver_exit()
        release.release()
        parked.wait("cancelled startup task returned to the pool")
        client.send(session="default", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert "phase:" not in last_tool_text(client)
        # Queue input while the replacement is still held so this response
        # cannot accidentally stand in for its eventual completion receipt.
        client.send(stdin="replacement still usable\n", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == RUNNING
        assert "phase:" not in last_tool_text(client)
        new_cell_release.release()
        client.expect(
            "[input requested: \"replacement> \"]\n'replacement still usable'\n"
        )
        assert "phase:" not in last_tool_text(client)
        client.send(session="default")
        assert last_tool_text(client) == "\n[idle]"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
