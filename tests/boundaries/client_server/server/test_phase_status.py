#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.lifecycle.test_startup import startup_fixture
from boundaries.client_server.python.test_startup import selected_python
from support.python import isolated_python
from boundaries.client_server.server.test_startup import gated_discovery
from support.assertions import last_result_text
from support.checkpoints import (
    FifoCheckpoint,
    checkpoint_evidence,
    wait_for_path,
    wait_for_worker_file,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import (
    code,
    normalize_process_diagnostic,
    normalize_python_resolution_error,
)
from support.native import LOADER_VARIABLE, build_interposer
from support.progress import elapsed_progress, phase_progress, without_elapsed
from support.previews import (
    TEXT_BUDGET,
    assert_preview,
    cell_text,
    compact_previews,
    normalize_preview_paths,
)
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    command,
    requires,
)
from support.resolvers import checkpoint_uv_environment
from support.snapshots import execution_snapshots
from support.suites import run_this_suite


@requires(NATIVE_FIXTURES, POSIX)
def test_completed_early_cell_keeps_prelaunch_phase(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        probe, probe_release, started, startup_release = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("probe", "probe-release", "ready", "ready-release")
        ]
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code(f"""
                import sys

                if sys.argv[0] == "-c":
                    with open({str(probe.path)!r}, "wb", buffering=0) as probe:
                        assert probe.write(b"1") == 1
                    with open({str(probe_release.path)!r}, "rb", buffering=0) as release:
                        assert release.read(1) == b"1"
                """)
        )
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment.update(
            {
                "PATH": str(root),
                "MCP_CONSOLE_TEST_PHASE_LOADER": LOADER_VARIABLE,
                "MCP_CONSOLE_TEST_PHASE_LIBRARY": str(
                    build_interposer(root, "relay_stdout_read_interposer")
                ),
                "MCP_CONSOLE_TEST_RELAY_READ_MATCH": '{"kind":"ready"}',
                "MCP_CONSOLE_TEST_RELAY_READ_BLOCKED": str(started.path),
                "MCP_CONSOLE_TEST_RELAY_READ_RELEASE": str(startup_release.path),
            }
        )
        # fmt: python
        launcher = code("""
            import os
            import sys

            os.environ["MCP_CONSOLE_TEST_RELAY_READ_PID"] = str(os.getpid())
            os.environ[os.environ.pop("MCP_CONSOLE_TEST_PHASE_LOADER")] = os.environ.pop(
                "MCP_CONSOLE_TEST_PHASE_LIBRARY"
            )
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                ("-c", launcher, str(binary), *DIRECT.serve()),
                environment,
                root,
            )
        )
        resources.callback(probe_release.release)
        resources.callback(startup_release.release)
        probe.wait("selected Python inspection is blocked")
        client.initialize_and_list_tools()
        client.send(
            r="must not execute", requirements={"python": ["six"]}, timeout_ms=0
        )
        assert phase_progress(last_result_text(client)) == "dependency preparation"
        probe_release.release()
        # Prelaunch rejects the R cell before starting the Python-only worker.
        # Its completed evaluation remains installed until its result is claimed.
        started.wait("default transport is waiting before ready")
        client.send(requirements={"action": "get"}, timeout_ms=0)
        assert last_result_text(client) == "\n[phase: startup]\n[worker starting]"
        startup_release.release()
        client.send(session="default")
        assert client.transcript[-1]["result"]["isError"]
        assert (
            last_result_text(client)
            == "R cells are unavailable in Python sessions without R"
        )
        client.send(python="42")
        assert last_result_text(client) == "42\n"
        return client.finish()


@execution_snapshots
@requires(POSIX, PROCESS_EVENTS)
@executions(DIRECT, SANDBOXED)
def test_lazy_worker_startup_phase_lasts_until_transport_ready(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures/zod"
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        control = root / "startup-control"
        started = root / "startup-started"
        release = root / "startup-release"
        control.write_text("block", encoding="utf-8")
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "ZOD_STARTUP_CONTROL": str(control),
            "ZOD_STARTUP_STARTED": str(started),
            "ZOD_STARTUP_RELEASE": str(release),
        }
        writable_root = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        client = resources.enter_context(
            McpClient(
                binary,
                execution.serve("--worker", str(zod), *writable_root),
                environment,
            )
        )
        resources.callback(release.touch)
        client.initialize_and_list_tools()
        client.send(r="complete after release", timeout_ms=0)
        text = last_result_text(client)
        elapsed_progress(text)
        assert without_elapsed(text) == "\n[running; poll with an empty send]"
        # Before the checkpoint, registration may contend with optional status.
        client.transcript[-1]["result"]["content"][0]["text"] = text.replace(
            "; phase: startup", ""
        )
        # The process is registered, but the worker has not sent ready.
        wait_for_path(started, "custom worker is waiting before ready", client=client)
        for _ in range(2):
            client.send(session="default", timeout_ms=0)
            text = last_result_text(client)
            assert phase_progress(text) == "startup", text
            elapsed_progress(text)
            assert without_elapsed(text) == "\n[running; poll with an empty send]"

        release.touch()
        evaluation = wait_for_worker_file(root, "zod-evaluation-started", client)
        # Release cleanup must precede finish, which removes sandbox storage.
        with ExitStack() as completion:
            completion.callback(evaluation.with_name("zod-release-evaluation").touch)
            # Readiness ends startup visibility while the cell remains active.
            client.send(session="default", timeout_ms=0)
            text = last_result_text(client)
            assert "phase:" not in text, text
            elapsed_progress(text)
            assert without_elapsed(text) == "\n[running; poll with an empty send]"
            evaluation.with_name("zod-release-evaluation").touch()
            client.send(session="default")
            assert last_result_text(client) == "zod: complete after release\n"
            client.send(session="default")
            assert last_result_text(client) == "\n[idle]"
        return client.finish()


@execution_snapshots
@requires(NATIVE_FIXTURES, POSIX)
@executions(DIRECT, SANDBOXED)
def test_phase_observation_does_not_wait_for_failed_worker_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    # Keep the retirement owner inside its signal call, beyond any observation
    # deadline. The timed response must not wait for that owner.
    # fmt: python
    server = code(r"""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_PID"] = str(os.getpid())
        os.environ[os.environ.pop("MCP_CONSOLE_TEST_LOADER")] = os.environ.pop(
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY"
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        checkpoints = [
            resources.enter_context(closing(FifoCheckpoint.create(root / name)))
            for name in ("blocked", "release", "returned")
        ]
        blocked, release, returned = checkpoints
        environment = {
            **os.environ,
            "TMPDIR": str(root),
            "MCP_CONSOLE_TEST_LOADER": LOADER_VARIABLE,
            "MCP_CONSOLE_TEST_RETIREMENT_LIBRARY": str(
                build_interposer(root, "launcher_retirement_interposer")
            ),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED": str(blocked.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE": str(release.path),
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURNED": str(returned.path),
            "MCP_CONSOLE_TEST_PHASE_FAILURE": str(root / "failed"),
        }
        relay = (
            Path(__file__).resolve().parents[3]
            / "fixtures/server_relay/phase_retirement.py"
        )
        writable_root = ("--writable-root", str(root)) if execution == SANDBOXED else ()
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                (
                    "-c",
                    server,
                    str(binary),
                    *execution.serve(
                        "--worker", str(binary), "--relay", str(relay), *writable_root
                    ),
                ),
                environment,
            )
        )
        resources.callback(release.release)
        client.initialize_and_list_tools()
        client.send(r="42")
        assert last_result_text(client) == "[done]", client.transcript[-1]
        failed = client.start_send(r="42", timeout_ms=500)
        blocked.wait("failed-worker retirement owns the lifecycle lock")
        client.response_timeout = 2
        client.receive(failed)
        assert without_elapsed(last_result_text(client)).endswith(
            "\n[running; poll with an empty send]"
        ), failed
        assert "phase:" not in last_result_text(client)
        assert client.request("ping")["result"] == {}
        release.release()
        returned.wait("retirement signal completed")
        client.response_timeout = 600
        client.send(session="default")
        assert "scripted phase failure" in (
            failed["result"]["content"][0]["text"] + last_result_text(client)
        ), client.transcript
        assert "phase:" not in last_result_text(client)
        client.send(r="42")
        assert last_result_text(client) == "[done]"
        return client.finish()


@requires(command("rustc"))
def test_shared_startup_phase_has_no_future_cell_clock(binary: Path) -> Transcript:
    with gated_discovery(binary) as (client, release):
        client.initialize_and_list_tools()
        for arguments in ({}, {"requirements": {"action": "get"}}):
            client.send(session="default", **arguments, timeout_ms=0)
            assert last_result_text(client) == "\n[phase: startup]\n[worker starting]"
            assert "elapsed:" not in last_result_text(client)
        client.send(r="stop('must not run')", timeout_ms=0)
        assert without_elapsed(last_result_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        assert phase_progress(last_result_text(client)) == "startup"
        elapsed_progress(last_result_text(client))
        client.send(session="default", timeout_ms=0)
        assert without_elapsed(last_result_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        assert phase_progress(last_result_text(client)) == "startup"
        release.release()
        client.send(session="default")
        assert client.transcript[-1]["result"]["isError"]
        assert "fixture R discovery failed" in last_result_text(client)
        assert "phase:" not in last_result_text(client)
        client.send(session="default", timeout_ms=0)
        assert "phase:" not in last_result_text(client)
        transcript, stderr = client.finish_with_standard_error(expected_exit_status=1)
        return json.loads(
            normalize_process_diagnostic(json.dumps([*transcript, {"stderr": stderr}]))
        )


@requires(POSIX, PROCESS_EVENTS, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_admitted_cell_keeps_shared_preparation_phase(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_fixture(binary, execution, phase="preparation") as fixture:
        client = fixture.client
        fixture.wait_for_resolver()
        client.initialize_and_list_tools()
        client.send(session="default", timeout_ms=0)
        assert last_result_text(client) == (
            "\n[phase: dependency preparation]\n[worker starting]"
        )
        assert "elapsed:" not in last_result_text(client)
        client.send(r="cat('cell completed\\n')", timeout_ms=0)
        assert without_elapsed(last_result_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        assert phase_progress(last_result_text(client)) == "dependency preparation", (
            last_result_text(client)
        )
        elapsed_progress(last_result_text(client))
        client.send(session="default", timeout_ms=0)
        assert without_elapsed(last_result_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        assert phase_progress(last_result_text(client)) == "dependency preparation", (
            last_result_text(client)
        )
        elapsed_progress(last_result_text(client))
        client.response_timeout = 600
        fixture.release.release()
        client.send(session="default", timeout_ms=600_000)
        assert last_result_text(client) == "cell completed\n"
        client.send(session="default")
        assert last_result_text(client) == "\n[idle]"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reached_preparation_phase_disappears_on_completion(
    binary: Path, execution: Execution
) -> Transcript:
    return reached_preparation(binary, execution, large_output=False)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preparation_phase_shares_the_complete_response_budget(
    binary: Path, execution: Execution
) -> Transcript:
    return reached_preparation(binary, execution, large_output=True)


def reached_preparation(
    binary: Path, execution: Execution, *, large_output: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        environment, started, release = checkpoint_uv_environment(
            root,
            "py-yaml12",
            reuse_resolved_python_for=("py-yaml12",),
            provide_python_module=("py-yaml12", "yaml12"),
        )
        environment.pop("RETICULATE_PYTHON", None)
        resources.callback(started.close)
        resources.callback(release.close)
        client = resources.enter_context(
            McpClient(
                binary,
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                environment,
            )
        )
        resources.callback(release.release)
        client.initialize_and_list_tools()
        client.send(python="import sys; print(sys.executable)")
        executable = last_result_text(client).strip()
        assert Path(executable).is_file(), checkpoint_evidence(
            "Python startup did not return an executable path", client
        )
        Path(environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"]).write_text(executable)
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
        begin = resources.enter_context(
            closing(
                FifoCheckpoint.create(
                    Path(client.temporary_directory.name) / "phase-code-release"
                )
            )
        )
        resources.callback(begin.release)
        output = "head\n" + "x" * 12_000 + "tail\n" if large_output else "prefix\n"
        print_source = (
            'print("head\\n" + "x" * 12_000 + "tail")'
            if large_output
            else 'print("prefix")'
        )
        # fmt: python
        python = code(f"""
            with open("phase-code-release", "rb", buffering=0) as gate:
                assert gate.read(1) == b"1"
            attempts = globals().get("attempts", 0) + 1
            {print_source}
            import yaml12

            print(yaml12.__name__, attempts)
            """)
        client.send(python=python, timeout_ms=0)
        assert "phase:" not in last_result_text(client)
        begin.release()
        started.wait("reached import owns dependency preparation")
        client.send(session="default", timeout_ms=0)
        text = last_result_text(client)
        assert len(text.encode()) <= TEXT_BUDGET
        if large_output:
            assert_preview(
                without_elapsed(text).removesuffix(
                    "\n[running; poll with an empty send]"
                ),
                output,
            )
            assert cell_text(client, 2) == output
        else:
            assert without_elapsed(text) == (
                "prefix\n\n[running; poll with an empty send]"
            ), text
        assert phase_progress(last_result_text(client)) == "dependency preparation"
        elapsed_progress(last_result_text(client))
        for _ in range(2):
            client.send(session="default", timeout_ms=0)
            assert without_elapsed(last_result_text(client)) == (
                "\n[running; poll with an empty send]"
            )
            assert phase_progress(last_result_text(client)) == "dependency preparation"
        release.release()
        client.send(session="default")
        assert last_result_text(client) == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "yaml12 1\n"
        ), last_result_text(client)
        client.send(session="default")
        assert last_result_text(client) == "\n[idle]"
        if large_output:
            normalize_preview_paths(client)
            compact_previews(client, "x")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_explicit_preparation_interrupt_keeps_phase_until_completion(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as resources:
        root = Path(temporary)
        environment, started, release = checkpoint_uv_environment(root, "six")
        interrupted = FifoCheckpoint.create(root / "interrupted")
        interrupt_release = FifoCheckpoint.create(root / "interrupt-release")
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPTED"] = str(interrupted.path)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )
        for checkpoint in (started, release, interrupted, interrupt_release):
            resources.callback(checkpoint.close)
        client = resources.enter_context(
            McpClient(binary, execution.serve("-c", "cache=host"), environment)
        )
        resources.callback(interrupt_release.release)
        client.initialize_and_list_tools()
        client.send(python="import sys; marker = 42; print(sys.executable)")
        executable = last_result_text(client).strip()
        assert Path(executable).is_file(), checkpoint_evidence(
            "Python startup did not return an executable path", client
        )
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
        client.send(session="default")
        assert last_result_text(client) == "\n[idle]"
        pending = client.start_send(requirements={"python": ["six"]})
        started.wait("explicit preparation owns resolver")
        interrupt = client.start_send(control="interrupt", timeout_ms=0)
        interrupted.wait("resolver accepted SIGINT but has not completed")
        client.receive(interrupt)
        assert last_result_text(client) == (
            "\n[phase: dependency preparation]\n[running; poll with an empty send]"
        ), last_result_text(client)
        assert "elapsed:" not in last_result_text(client)
        assert "result" not in pending
        interrupt_release.release()
        client.receive(pending)
        assert pending["result"]["isError"]
        error = pending["result"]["content"][0]["text"]
        assert "managed Python resolution failed" in error, error
        assert "phase:" not in error
        pending["result"]["content"][0]["text"] = normalize_python_resolution_error(
            error, executable=executable
        )
        client.send(requirements={"python": ["six"]})
        assert last_result_text(client) == "[prepared]"
        client.send(python="marker")
        assert last_result_text(client) == "42\n"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
