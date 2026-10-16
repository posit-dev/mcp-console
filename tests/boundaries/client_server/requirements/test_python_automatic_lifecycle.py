#!/usr/bin/env -S uv run --script

import re
import signal
import shlex
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import phase_progress, without_elapsed, without_elapsed_result
from support.requirements import POSIX, PROCESS_EVENTS, R, requires
from support.assertions import entry_result_text, last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.processes import (
    capture_process_identity,
    child_process_identities,
    live_processes,
)
from support.normalization import code, normalize_python_resolution_error
from support.records import Transcript
from boundaries.client_server.python.test_peer_runtime import without_r
from support.resolvers import (
    checkpoint_uv_environment,
    initialize_python_and_record_baseline,
    local_resolver_owner,
    recording_uv_environment,
    uv_tool_run_requirements,
)
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_automatic_resolution_from_background_thread(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import threading

            thread_result = []


            def import_from_thread():
                import json

                try:
                    import mcp_console_thread_missing
                except ModuleNotFoundError as error:
                    thread_result.extend((json.__name__, error.name, str(error)))


            thread = threading.Thread(target=import_from_thread)
            thread.start()
            thread.join()
            print(f"available module: {thread_result[0]}")
            print(f"background-thread name: {thread_result[1]}")
            print(thread_result[2])
            """)
        client.send(python=python)
        output = last_result_text(client)
        for expected in (
            "available module: json",
            "background-thread name: mcp_console_thread_missing",
            "configuring thread",
            "before starting the background thread",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_automatic_resolution_from_fork_child(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(directory)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        client.initialize_and_list_tools()
        baseline = initialize_python_and_record_baseline(client, record)

        # fmt: python
        python = code("""
            import importlib
            import os
            import select
            import signal
            import threading

            # Make CPython's thread-count warning deterministic even when the
            # sandbox prevents native thread enumeration.
            release_thread = threading.Event()
            thread = threading.Thread(target=release_thread.wait)
            thread.start()

            read_descriptor, write_descriptor = os.pipe()
            child = os.fork()
            if child == 0:
                os.close(read_descriptor)
                try:
                    importlib.import_module("mcp_console_fork_child_missing")
                except ModuleNotFoundError as error:
                    payload = f"fork-child name: {error.name}\\n{error}"
                else:
                    payload = "missing import unexpectedly succeeded"
                os.write(write_descriptor, payload.encode())
                os._exit(0)

            release_thread.set()
            thread.join()
            os.close(write_descriptor)
            chunks = []
            while True:
                readable, _, _ = select.select([read_descriptor], [], [], 10)
                if not readable:
                    os.kill(child, signal.SIGKILL)
                    os.waitpid(child, 0)
                    raise AssertionError("fork child did not finish its import")
                chunk = os.read(read_descriptor, 65536)
                if not chunk:
                    break
                chunks.append(chunk)
            payload = b"".join(chunks).decode()
            os.close(read_descriptor)
            _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0
            print(payload)
            """)
        result = client.send(python=python)
        output = last_result_text(client)
        for expected in (
            "fork-child name: mcp_console_fork_child_missing",
            "main worker process",
            "before",
            "child",
            "requirements.python",
        ):
            assert expected in output, (expected, output)
        normalized, count = re.subn(
            r"(This process \(pid=)\d+(\) is multi-threaded)",
            r"\1<pid>\2",
            output,
        )
        assert count == 1, output
        result["content"][0]["text"] = normalized
        assert len(uv_tool_run_requirements(record)) == baseline

        client.send(python="6 * 7")
        assert last_result_text(client) == "42\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_times_out_and_polls_automatic_python_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, started, release = checkpoint_uv_environment(
            directory,
            "py-yaml12",
        )
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        resolver_released = False
        finished = False
        begin = FifoCheckpoint.create(
            Path(client.temporary_directory.name) / "automatic-import-release"
        )
        try:
            client.initialize_and_list_tools()
            client.send(python="None")
            assert last_result_text(client) == "[done]"
            # fmt: python
            python = code("""
                with open("automatic-import-release", "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                automatic_timeout_attempts = (
                    globals().get(
                        "automatic_timeout_attempts",
                        0,
                    )
                    + 1
                )
                import yaml12

                (yaml12.__name__, automatic_timeout_attempts)
                """)
            evaluation = client.start_send(python=python, timeout_ms=1)
            client.receive(evaluation)
            assert (
                without_elapsed(entry_result_text(evaluation))
                == "\n[running; poll with an empty send]"
            )
            assert "phase:" not in entry_result_text(evaluation), evaluation

            begin.release()
            started.wait("automatic Python resolver")
            client.send(timeout_ms=0)
            assert without_elapsed(last_result_text(client)) == (
                "\n[running; poll with an empty send]"
            )
            assert phase_progress(last_result_text(client)) == "dependency preparation"

            release.release()
            resolver_released = True
            client.send(timeout_ms=30_000)
            assert last_result_text(client) == (
                "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
                "('yaml12', 1)\n"
            )
            transcript = client.finish()
            finished = True
            return transcript
        finally:
            begin.release()
            begin.close()
            if not resolver_released:
                release.release()
            started.close()
            release.close()
            if not finished:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, R)
def test_interrupts_automatic_python_resolver_and_preserves_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    return interrupts_automatic_python_resolver_and_preserves_worker(binary, execution)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_interrupts_no_r_automatic_python_resolver_and_preserves_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    # The recorded candidate includes the no-R SQL provider's Python dependency.
    return interrupts_automatic_python_resolver_and_preserves_worker(
        binary, execution, with_r=False
    )


def interrupts_automatic_python_resolver_and_preserves_worker(
    binary: Path,
    execution: Execution,
    *,
    with_r: bool = True,
) -> Transcript:
    requirement = "mcp_console_blocked_automatic_import"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, started, release = checkpoint_uv_environment(
            directory,
            requirement,
        )
        environment.pop("RETICULATE_PYTHON", None)
        if not with_r:
            uv = environment["RETICULATE_UV"]
            without_r(environment, directory)
            environment["RETICULATE_UV"] = uv
            commands = Path(environment["PATH"])
            wrapper = commands / "uv"
            wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(uv)} "$@"\n')
            wrapper.chmod(0o755)
            (commands / "python3").symlink_to(sys.executable)
        environment["RUST_LOG"] = "error"
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            client = McpClient(
                binary,
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                environment,
            )
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        passed = False
        begin = FifoCheckpoint.create(
            Path(client.temporary_directory.name) / "automatic-import-release"
        )
        try:
            client.initialize_and_list_tools()
            client.send(python="import sys; print(sys.executable)")
            executable = last_result_text(client).strip()
            client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
            server = capture_process_identity(client.process.pid)
            owner = local_resolver_owner(server, binary)
            existing_children = child_process_identities(owner)
            # fmt: python
            python = code(f"""
                import importlib
                import os

                with open("automatic-import-release", "rb", buffering=0) as gate:
                    assert gate.read(1) == b"1"
                automatic_interrupt_state = 41
                automatic_interrupt_pid = os.getpid()
                importlib.import_module("{requirement}")
                automatic_interrupt_cell_ran = True
                """)
            client.send(python=python, timeout_ms=0)
            assert (
                without_elapsed(last_result_text(client))
                == "\n[running; poll with an empty send]"
            )
            assert "phase:" not in last_result_text(client), client.transcript[-1]
            # Collect the admitted cell's response before it can enter preparation.
            begin.release()
            started.wait("automatic Python resolver")
            client.send(timeout_ms=0)
            assert without_elapsed(last_result_text(client)) == (
                "\n[running; poll with an empty send]"
            )
            assert phase_progress(last_result_text(client)) == "dependency preparation"
            resolver = [
                child
                for child in child_process_identities(owner)
                if child not in existing_children
            ]
            assert len(resolver) == 1, resolver
            interrupt = client.start_send(control="interrupt", timeout_ms=30_000)
            client.receive(interrupt)
            # Keep the FIFO blocked until interruption has reaped this resolver.
            assert live_processes(resolver) == [], (
                "interrupt did not reap the automatic Python resolver"
            )
            error = entry_result_text(interrupt)
            for expected in (
                "ModuleNotFoundError",
                requirement,
                "managed Python resolution",
                "KeyboardInterrupt",
                "requirements.python",
            ):
                assert expected in error, (expected, error)
            interrupt["result"]["content"][0]["text"] = (
                normalize_python_resolution_error(error, executable=executable)
            )

            client.send(
                python=(
                    "automatic_interrupt_state + "
                    "int('automatic_interrupt_cell_ran' not in globals()) + "
                    "int(__import__('os').getpid() == automatic_interrupt_pid) - 1"
                )
            )
            assert last_result_text(client) == "42\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            begin.release()
            begin.close()
            release.release()
            started.close()
            release.close()
            if not passed:
                stop_client(client)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(R)
def test_restart_discards_unactivated_automatic_python_candidate(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        replacement_requirement = "mcp-console-restart-fixture"
        environment, uv_started, uv_release = checkpoint_uv_environment(
            directory,
            replacement_requirement,
            reuse_resolved_python_for=("py-yaml12", replacement_requirement),
            provide_python_module=("py-yaml12", "yaml12"),
        )
        environment.pop("RETICULATE_PYTHON", None)
        environment["TMPDIR"] = temporary
        reuse_record = Path(environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"])
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        passed = False
        worker_checkpoints: list[FifoCheckpoint] = []
        try:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                config <- reticulate::py_config()
                activation_ready <- tempfile("mcp-console-activation-ready-")
                activation_release <- tempfile("mcp-console-activation-release-")
                activation_sent <- tempfile("mcp-console-activation-sent-")
                import_release <- tempfile("mcp-console-import-release-")
                cat(
                  activation_ready,
                  activation_release,
                  activation_sent,
                  import_release,
                  config$python,
                  sep = "\n"
                )
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 5, setup
            resolved_python = paths.pop()
            assert Path(resolved_python).is_file(), resolved_python
            Path(environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"]).write_text(
                resolved_python,
                encoding="utf-8",
            )
            setup["content"][0]["text"] = (
                "<activation ready>\n<activation release>\n<activation sent>\n<import release>"
            )
            activation_ready, activation_release, activation_sent, import_release = [
                FifoCheckpoint.create(Path(path)) for path in paths
            ]
            worker_checkpoints.extend(
                (activation_ready, activation_release, activation_sent, import_release)
            )

            # Pause after resolution and immediately before PythonActivated.
            # fmt: r
            r = code(r"""
                globals <- get(".globals", envir = asNamespace("reticulate"))
                original <- activeBindingFunction("python_requirements", globals)
                rm(list = "python_requirements", envir = globals)
                makeActiveBinding("python_requirements", function(value) {
                  if (missing(value)) {
                    return(original())
                  }
                  ready <- fifo(activation_ready, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), ready)
                  close(ready)
                  release <- fifo(activation_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(readBin(release, "raw", n = 1L), charToRaw("1")))
                  close(release)
                  original(value)
                  sent <- fifo(activation_sent, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), sent)
                  close(sent)
                }, globals)
                """)
            client.send(r=r)
            assert last_result_text(client) == "[done]"

            evaluation = client.start_send(
                # fmt: python
                python=code("""
                    with open(r.import_release, "rb", buffering=0) as gate:
                        assert gate.read(1) == b"1"
                    import yaml12
                    """),
                timeout_ms=0,
            )
            client.receive(evaluation)
            evaluation_result = evaluation["result"]
            assert without_elapsed_result(evaluation_result) == {
                "content": [
                    {
                        "type": "text",
                        "text": "\n[running; poll with an empty send]",
                    }
                ],
                "isError": False,
            }, evaluation_result
            import_release.release()
            activation_ready.wait("automatic managed Python activation")

            restart = client.start_send(
                control="restart",
                requirements={"python": [replacement_requirement]},
            )
            uv_started.wait("restart Python resolution")
            activation_release.release()
            activation_sent.wait("published automatic Python activation")
            uv_release.release()
            client.receive(restart)

            restart_result = restart["result"]
            assert restart_result.get("isError") is not True, restart_result
            assert restart_result["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[active evaluation stopped by session restart request]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ], restart_result

            # fmt: r
            r = code(f"""
                packages <- reticulate::py_require()$packages
                c("{replacement_requirement}" %in% packages, "py-yaml12" %in% packages)
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1]  TRUE FALSE\n"
            assert reuse_record.read_text(encoding="utf-8").splitlines() == [
                "py-yaml12",
                replacement_requirement,
            ]
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)
            for checkpoint in worker_checkpoints:
                checkpoint.close()
            uv_started.close()
            uv_release.close()


if __name__ == "__main__":
    run_this_suite(__file__)
