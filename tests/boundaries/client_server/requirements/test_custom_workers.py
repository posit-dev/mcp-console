#!/usr/bin/env -S uv run --script

import base64
import json
import os
import select
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed, without_elapsed_result
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, SQL, requires
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.events import Events
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.processes import capture_process_identity, host_process_id, kill_processes
from support.r import r_test_environment
from support.records import Transcript
from support.resolvers import record_resolved_r_library
from support.suites import run_this_suite

PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)

from boundaries.client_server._harness import (
    expose_idle_input_request,
    expose_idle_sideband_output,
    wait_for_marker,
)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_skips_managed_python_preflight(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["R_HOME"] = "/mcp-console-custom-worker-must-not-run-rscript"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
        environment,
    )
    client.initialize_and_list_tools()
    # The custom worker accepts its own command language in this field.
    payload = code(r"""
        echo echo
        """).removesuffix("\n")
    client.send(python=payload)
    result = client.send(requirements={"python": ["py-yaml12"]})
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
    result = client.send(
        control="restart",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    ), result
    result = client.send(
        r="echo must not run",
        requirements={"python": ["py-yaml12"]},
    )
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == (
        "Python requirements are unavailable with a custom worker"
    )
    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    client.send()
    assert last_tool_text(client) == "\n[idle]"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_standalone_preparation_before_worker_startup_is_causal_and_idempotent(
    binary: Path,
    execution: Execution,
) -> Transcript:
    return standalone_preparation(binary, execution, {})


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_standalone_replacement_is_inspectable_before_worker_startup(
    binary: Path, execution: Execution
) -> Transcript:
    return standalone_preparation(binary, execution, {"action": "set"})


def resolver_fixture_arguments(
    execution: Execution, *arguments: str
) -> tuple[str, ...]:
    # Fixture records/checkpoints use UV_TOOL_DIR's default resolver write grant.
    # Preserve those host-cache overrides in both execution modes.
    return execution.serve(*arguments, "-c", "cache=host")


def standalone_preparation(
    binary: Path, execution: Execution, action: dict
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    relay = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "server_relay"
        / "scripted_relay.py"
    )
    ir = Path(__file__).resolve().parents[3] / "fixtures" / "ordered_retirement_ir"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        library = temporary / "standalone-candidate"
        library.mkdir()
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(ir)
        resolver_started = FifoCheckpoint.create(temporary / "resolver-started")
        resolver_release = FifoCheckpoint.create(temporary / "resolver-release")
        worker_started = temporary / "zod-started"
        resolver_counter = temporary / "ir-counter"
        environment, _ = r_test_environment()
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        environment["TMPDIR"] = temporary_directory
        environment["UV_TOOL_DIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_IR_COUNTER"] = str(resolver_counter)
        environment["MCP_CONSOLE_TEST_IR_LIBRARIES"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(resolver_started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(resolver_release.path)
        environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "ready"
        environment["MCP_CONSOLE_TEST_ZOD_STARTED"] = str(worker_started)
        client = McpClient(
            binary,
            resolver_fixture_arguments(
                execution, "--worker", str(zod), "--relay", str(relay)
            ),
            environment,
        )
        finished = False
        released = False
        try:
            client.initialize_and_list_tools()
            invalid = client.start_send(
                requirements={"r": ["must-not-resolve"]},
                stdin="must not queue\n",
            )
            readable, _, _ = select.select(
                [client.stdout, resolver_started.descriptor],
                [],
                [],
                10,
            )
            assert client.stdout in readable, (
                "requirements with standalone stdin did not return validation"
            )
            assert resolver_started.descriptor not in readable, (
                "requirements with standalone stdin started a resolver"
            )
            client.receive(invalid)
            assert invalid["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "requirements-only `send` performs standalone "
                            "preparation and cannot also queue stdin"
                        ),
                    }
                ],
                "isError": True,
            }, invalid
            assert not resolver_counter.exists(), resolver_counter
            assert not worker_started.exists(), worker_started
            assert not list(temporary.rglob("mcp-console-server-relay-wire.jsonl"))

            preparation = client.start_send(
                requirements={**action, "r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            # Resolver launch uses the preparation budget, including cold
            # sandbox startup. An early response must expose its complete error.
            deadline = client.response_deadline()
            readable, _, _ = select.select(
                [resolver_started.descriptor, client.stdout],
                [],
                [],
                max(0, deadline - time.monotonic()),
            )
            if client.stdout in readable:
                client.receive(preparation)
                raise AssertionError(
                    f"standalone preparation returned before its resolver checkpoint: {preparation}"
                )
            resolver_started.wait("standalone requirement resolver", timeout=0)
            if action:
                snapshot = client.send(requirements={"action": "get"})[
                    "structuredContent"
                ]
                assert snapshot["prepared"] is False
                assert snapshot["requirements"]["r"] == []
            assert not worker_started.exists(), worker_started
            assert not list(temporary.rglob("mcp-console-server-relay-wire.jsonl"))
            readable, _, _ = select.select([client.stdout], [], [], 0.25)
            assert not readable, "timeout_ms applied to standalone preparation"

            resolver_release.release()
            released = True
            client.receive(preparation)
            assert preparation["result"] == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, preparation
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(temporary.rglob("mcp-console-server-relay-wire.jsonl"))

            repeated = client.send(
                requirements={**action, "r": ["standalone-requirement"]},
                timeout_ms=0,
            )
            assert repeated == {
                "content": [{"type": "text", "text": "[prepared]"}],
                "isError": False,
            }, repeated
            assert resolver_counter.read_text(encoding="utf-8") == "1"
            assert not worker_started.exists(), worker_started
            assert not list(temporary.rglob("mcp-console-server-relay-wire.jsonl"))
            transcript = client.finish()
            finished = True
            return transcript
        finally:
            if not released:
                resolver_release.release()
            resolver_started.close()
            resolver_release.close()
            if not finished:
                stop_client(client)


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_starts_without_home(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    environment.pop("HOME", None)
    environment.pop("XDG_CACHE_HOME", None)
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod), "-c", "cache=host"),
        environment,
    )
    client.initialize_and_list_tools()

    client.send(sql="echo echo")
    assert last_tool_text(client) == "zod sql: echo\n"

    client.send(r="echo echo")
    assert last_tool_text(client) == "zod: echo\n"
    return client.finish()


def _write_selected_ir(path: Path) -> None:
    path.write_text(
        code(r"""
            #!/bin/sh
            if [ "$1" = --version ]; then
              printf "ir 0.4.0\n"
            elif [ -n "${MCP_CONSOLE_TEST_IR_FAIL_ONCE:-}" ] && [ ! -e "$MCP_CONSOLE_TEST_IR_FAIL_ONCE" ]; then
              printf 1 > "$MCP_CONSOLE_TEST_IR_FAIL_ONCE"
              printf "fixture rejection\n" >&2
              exit 77
            else
              printf "%s" "$MCP_CONSOLE_TEST_IR_LIBRARY"
            fi
            """),
        encoding="utf-8",
    )
    path.chmod(0o755)


@requires(POSIX)
@executions(DIRECT)
def test_custom_worker_preserves_non_utf8_r_libs(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        _write_selected_ir(fake_bin / "ir")
        library = root / "managed-library"
        library.mkdir()
        environment, _ = r_test_environment()
        environment["PATH"] = os.pathsep.join((str(fake_bin), environment["PATH"]))
        environment["MCP_CONSOLE_TEST_IR_LIBRARY"] = str(library)
        ambient = os.fsdecode(os.fsencode(str(root)) + b"/ambient-\xff")
        if sys.platform == "linux":
            Path(ambient).mkdir()
        environment["R_LIBS"] = ambient
        client = McpClient(binary, execution.serve("--worker", str(zod)), environment)
        client.initialize_and_list_tools()
        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        client.send(r="report raw R library bytes")
        assert last_tool_text(client) == "zod raw R library: preserved=true\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_keeps_first_r_resolver_selection(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first, second = root / "first", root / "second"
        first.mkdir()
        second.mkdir()
        _write_selected_ir(second / "ir")
        library = root / "managed-library"
        library.mkdir()
        environment, _ = r_test_environment()
        environment["PATH"] = os.pathsep.join(
            (str(first), str(second), environment["PATH"])
        )
        environment["MCP_CONSOLE_TEST_IR_LIBRARY"] = str(library)
        environment["UV_TOOL_DIR"] = temporary
        unexpected = root / "unexpected-ir"
        environment["MCP_CONSOLE_TEST_UNEXPECTED_IR"] = str(unexpected)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]

        (first / "ir").write_text(
            '#!/bin/sh\nprintf 1 > "$MCP_CONSOLE_TEST_UNEXPECTED_IR"\nexit 79\n',
            encoding="utf-8",
        )
        (first / "ir").chmod(0o755)
        client.send(requirements={"r": ["zeallot"]})
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        assert not unexpected.exists(), "resolver selection changed within the session"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_keeps_selection_after_failed_first_manifest(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        first, second = root / "first", root / "second"
        first.mkdir()
        second.mkdir()
        _write_selected_ir(second / "ir")
        library = root / "managed-library"
        library.mkdir()
        environment, _ = r_test_environment()
        environment["PATH"] = os.pathsep.join(
            (str(first), str(second), environment["PATH"])
        )
        environment["MCP_CONSOLE_TEST_IR_LIBRARY"] = str(library)
        environment["MCP_CONSOLE_TEST_IR_FAIL_ONCE"] = str(root / "first-failed")
        environment["UV_TOOL_DIR"] = temporary
        unexpected = root / "unexpected-ir"
        environment["MCP_CONSOLE_TEST_UNEXPECTED_IR"] = str(unexpected)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        failed = client.send(requirements={"r": ["praise"]})
        assert failed["isError"] is True, failed
        assert failed["content"][0]["text"] == (
            "R package resolution failed with exit status: 77: fixture rejection"
        ), failed

        (first / "ir").write_text(
            '#!/bin/sh\nprintf 1 > "$MCP_CONSOLE_TEST_UNEXPECTED_IR"\nexit 79\n',
            encoding="utf-8",
        )
        (first / "ir").chmod(0o755)
        client.send(requirements={"r": ["zeallot"]})
        assert last_tool_text(client) == "[prepared]", client.transcript[-1]
        assert not unexpected.exists(), "failed manifest lost the first selection"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, NATIVE_FIXTURES)
def test_interrupt_after_local_resolver_exit_rejects_success(
    binary: Path, execution: Execution
) -> Transcript:
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "finished_ir_with_open_stdout"
    )
    zod = fixture.with_name("zod")
    with tempfile.TemporaryDirectory() as temporary, Events() as exits:
        root = Path(temporary)
        fake_bin = root / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(fixture)
        library = root / "managed-library"
        library.mkdir()
        started = FifoCheckpoint.create(root / "ir-started")
        ir_release = FifoCheckpoint.create(root / "ir-release")
        holder_release = FifoCheckpoint.create(root / "holder-release")
        observer_entered = FifoCheckpoint.create(root / "observer-entered")
        observer_release = FifoCheckpoint.create(root / "observer-release")
        child_killed = FifoCheckpoint.create(root / "child-killed")
        interposer = build_interposer(root, "child_exit_observation")
        environment, _ = r_test_environment()
        environment["PATH"] = os.pathsep.join((str(fake_bin), environment["PATH"]))
        environment["UV_TOOL_DIR"] = str(root)
        environment.update(
            {
                "MCP_CONSOLE_TEST_IR_LIBRARY": str(library),
                "MCP_CONSOLE_TEST_IR_PID": str(root / "ir-pid"),
                "MCP_CONSOLE_TEST_IR_HOLDER_PID": str(root / "holder-pid"),
                "MCP_CONSOLE_TEST_IR_STARTED": str(started.path),
                "MCP_CONSOLE_TEST_IR_RELEASE": str(ir_release.path),
                "MCP_CONSOLE_TEST_IR_HOLDER_RELEASE": str(holder_release.path),
                "MCP_CONSOLE_TEST_OBSERVER_LIBRARY": str(interposer),
                "MCP_CONSOLE_TEST_OBSERVER_TARGET": str(root / "ir-pid"),
                "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
                "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(observer_entered.path),
                "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(observer_release.path),
                "MCP_CONSOLE_TEST_CHILD_KILLED": str(child_killed.path),
                "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
            }
        )
        # Direct preparation inherits the loader from the server. Sandboxed
        # preparation receives it in its own environment after runner setup.
        # fmt: python
        launcher = code("""
            import os
            import sys

            os.environ["MCP_CONSOLE_TEST_OBSERVER_SERVER"] = str(os.getpid())
            loader = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"
            os.environ[loader] = os.environ.pop("MCP_CONSOLE_TEST_OBSERVER_LIBRARY")
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        client = McpClient(
            Path(sys.executable),
            (
                "-c",
                launcher,
                str(binary),
                *resolver_fixture_arguments(
                    execution,
                    "--worker",
                    str(zod),
                    "-c",
                    f"resolver.environment.{LOADER_VARIABLE}={json.dumps(str(interposer))}",
                ),
            ),
            environment,
        )
        holder_identity = None
        try:
            client.initialize_and_list_tools()
            pending = client.start_send(requirements={"r": ["praise"]})
            started.wait("materializer published its output and descriptor holder")
            ir_pid = host_process_id(
                int((root / "ir-pid").read_text()), client.process.pid
            )
            holder_identity = capture_process_identity(
                host_process_id(
                    int((root / "holder-pid").read_text()), client.process.pid
                )
            )
            exits.watch_process(ir_pid)
            ir_release.release()
            assert ir_pid in exits.wait(10), "resolver child did not exit"
            # I/O retirement no longer waits for the holder's stdout EOF. Hold
            # exit observation instead, keeping this preparation interruptible
            # after actual child exit and before its result can be published.
            observer_entered.wait("materializer exit observation held")

            client.send(control="interrupt", timeout_ms=0)
            assert (
                last_tool_text(client)
                == "\n[phase: dependency preparation]\n[running; poll with an empty send]"
            ), client.transcript[-1]
            observer_release.release()
            holder_release.release()
            client.receive(pending)
            assert pending["result"]["isError"] is True, pending
            assert (
                "local resolver interrupted" in pending["result"]["content"][0]["text"]
            )
            state = client.send(requirements={"action": "get"})["structuredContent"]
            assert state["requirements"]["r"] == [], state
            assert not (root / "early-reap").exists(), (
                "materializer reaped before its observation settled"
            )
            return client.finish()
        finally:
            ir_release.release()
            observer_release.release()
            holder_release.release()
            stop_client(client)
            if holder_identity is not None:
                kill_processes((holder_identity,))
            started.close()
            ir_release.close()
            holder_release.close()
            observer_entered.close()
            observer_release.close()
            child_killed.close()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_custom_worker_prepares_r_and_duckdb_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        environment["TMPDIR"] = temporary
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="echo echo")

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"

        client.send(requirements={"duckdb": ["json"]})
        assert last_tool_text(client) == "[prepared]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"

        client.send(r="fail next r preparation after output")
        assert last_tool_text(client) == "[done]"
        result = client.send(
            r="echo failed preparation cell ran",
            requirements={"r": ["zeallot"]},
        )
        assert result["isError"] is True, result
        assert result["content"] == [
            {"type": "text", "text": "before failed preparation\n"},
            {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
            {
                "type": "text",
                "text": (
                    "\nzod rejected R preparation; further requirement changes "
                    "are unavailable until session restart"
                ),
            },
        ], result

        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "capture" / "events.jsonl")
            .read_text(encoding="utf-8")
            .splitlines()
        ]
        artifact = next(
            event for event in reversed(events) if event["event"] == "image_retained"
        )
        recorded_result = next(
            event for event in reversed(events) if event["event"] == "tool_result"
        )
        assert artifact["sequence"] < recorded_result["sequence"]
        assert artifact["event"] == "image_retained", artifact
        assert recorded_result["event"] == "tool_result", recorded_result
        assert artifact["call_id"] is None, artifact
        call = next(
            event for event in reversed(events) if event["event"] == "tool_call"
        )
        assert recorded_result["call_id"] == call["call_id"], events[-2:]
        assert recorded_result["result"]["content"][1] == {
            "type": "image",
            "artifactId": artifact["artifact_id"],
            "path": artifact["path"],
            "mimeType": "image/png",
        }, recorded_result
        assert (session / artifact["path"]).read_bytes() == base64.b64decode(PNG_1X1)

        client.send(r="emit output and image before completion", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)
        try:
            result = client.send(
                r="echo active restart-required cell ran",
                requirements={"r": ["cli"]},
            )
            assert result == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "worker is already evaluating a cell; poll it before "
                            "preparing requirements"
                        ),
                    }
                ],
                "isError": True,
            }, result

            client.send(timeout_ms=0)
            assert without_elapsed_result(client.transcript[-1]["result"]) == {
                "content": [
                    {"type": "text", "text": "before pending image\n"},
                    {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                    {
                        "type": "text",
                        "text": "after pending image\n\n[running; poll with an empty send]",
                    },
                ],
                "isError": False,
            }, client.transcript[-1]
        finally:
            (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"

        result = client.send(
            r="echo restart-required cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": "requirements require session restart; cell was not run",
                }
            ],
            "isError": True,
        }, result
        client.send(r="echo worker remains usable")
        assert last_tool_text(client) == "zod: worker remains usable\n"

        result = client.send(r="report managed python activation")
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert "custom worker reported a managed Python activation" in failure, failure
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_custom_worker_reports_idle_input_before_preparation_failure(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        environment["TMPDIR"] = temporary
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="echo worker ready")
        assert last_tool_text(client) == "zod: worker ready\n"
        expose_idle_input_request(client, temporary_path)

        result = client.send(requirements={"r": ["praise"]})
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            '[idle R callback requested input "idle> " during requirement '
            "preparation; collect callback input with send before preparing requirements]\n"
            "[worker terminated by signal 9]\n"
            "[worker stopped: in-memory state lost]"
        ), result
        result = client.send(requirements={"r": ["zeallot"]})
        assert result == {
            "content": [{"type": "text", "text": "[restart required]"}],
            "isError": False,
        }, result
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_resolves_idle_activity_before_preparation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="resolve python while idle")
        assert last_tool_text(client) == "[done]"

        client.send(requirements={"r": ["praise"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_combined_requirements_keep_idle_output_as_one_prelude(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        failure = temporary_path / "fail-r-resolution"
        environment["TMPDIR"] = temporary
        environment["MCP_CONSOLE_TEST_R_RESOLUTION_FAILURE"] = str(failure)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        expose_idle_sideband_output(client, temporary_path, "combined-requirements")

        client.send(
            r="echo combined cell",
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == (
            "zod background sideband\n"
            "[output produced while idle]\n"
            "zod: combined cell\n"
        ), last_tool_text(client)
        client.send()
        assert last_tool_text(client) == "\n[idle]"

        expose_idle_sideband_output(
            client,
            temporary_path,
            "combined-requirements-failure",
        )
        failure.touch()
        result = client.send(
            r="echo failed resolver cell ran",
            requirements={"r": ["cli"]},
        )
        assert result == {
            "content": [
                {"type": "text", "text": "idle before failure image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": (
                        "idle after failure image\n"
                        "[output produced while idle]\n"
                        "R package resolution failed with exit status: 1: "
                        "fixture R resolver failed"
                    ),
                },
            ],
            "isError": True,
        }, result
        failure.unlink()
        client.send(r="echo worker still usable")
        assert last_tool_text(client) == "zod: worker still usable\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_custom_worker_resolves_idle_activity_before_evaluation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        environment["TMPDIR"] = temporary
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="resolve python while idle")
        assert last_tool_text(client) == "[done]", repr(last_tool_text(client))

        client.send(r="echo echo")
        assert last_tool_text(client) == "zod: echo\n"

        expose_idle_input_request(client, temporary_path)
        poll_start = len(client.transcript)
        submitted = client.start_send(r="echo echo", stdin="continue\n")
        wait_for_marker(
            temporary_path,
            "zod-idle-input-received",
            client,
        )
        client.receive(submitted)
        output = last_tool_text(client)
        if output != "zod: echo\n":
            assert output == "\n[waiting for stdin]", repr(output)
            client.send()
            output = last_tool_text(client)
        assert output == "zod: echo\n", repr(output)
        calls = client.transcript[poll_start:]
        submitted["result"] = calls[-1]["result"]
        client.transcript[poll_start:] = [submitted]
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_custom_worker_restart_prepares_r_and_duckdb_requirements(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with tempfile.TemporaryDirectory() as temporary:
        temporary_path = Path(temporary)
        isolated_library = temporary_path / "isolated-library"
        isolated_library.mkdir()
        environment["R_LIBS"] = str(isolated_library)
        environment["R_LIBS_SITE"] = str(isolated_library)
        environment["R_LIBS_USER"] = str(isolated_library)
        record_resolved_r_library(environment, temporary_path)
        client = McpClient(
            binary,
            resolver_fixture_arguments(execution, "--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

        client.send(
            control="restart",
            requirements={"r": ["praise"], "duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[starting new worker]\n[idle]"

        client.send(r="report managed R requirement")
        assert last_tool_text(client) == "zod R requirement: prepared=true\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
