#!/usr/bin/env -S uv run --script

import json
import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed, without_elapsed_result
from support.assertions import last_result_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.native import SHARED_LIBRARY_FLAG
from support.python import runtime_source_line
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, requires
from support.resolvers import checkpoint_uv_environment, named_requirement_error
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_python_preparation_while_evaluation_is_running(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        uv_record = temporary / "uv-record.jsonl"
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["UV_TOOL_DIR"] = str(temporary)
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            runtime_generation_marker = "original runtime retained"
            preparation_gate = input("preparation gate> ")
            """)
        client.send(python=python)
        assert last_result_text(client) == (
            '[input requested: "preparation gate> "]\n[waiting for stdin]'
        )
        uv_record.write_text("", encoding="utf-8")

        preparation_returned = threading.Event()

        def stop_blocked_preparation() -> None:
            # This is only a deadlock guard. The blocked input, not elapsed
            # time, proves that a successful preparation response was prompt.
            if not preparation_returned.wait(30):
                client.process.kill()

        watchdog = threading.Thread(target=stop_blocked_preparation)
        watchdog.start()
        try:
            client.send(
                requirements={"python": ["py-yaml12"]},
            )
        finally:
            preparation_returned.set()
            watchdog.join()
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "worker is already evaluating a cell; poll it before preparing requirements"
        )
        assert uv_record.read_text(encoding="utf-8") == ""

        combined = client.send(
            python="combined_cell_ran = True",
            requirements={"python": ["py-yaml12"]},
        )
        assert combined["isError"] is True, combined
        assert combined["content"][0]["text"] == (
            "worker is already evaluating a cell; poll it before preparing requirements"
        )
        assert uv_record.read_text(encoding="utf-8") == ""

        wait_for_evaluation_output(
            client,
            "[done]",
            "Python input completion",
            stdin="continue\n",
        )
        client.send(
            python=("runtime_generation_marker, 'combined_cell_ran' not in globals()")
        )
        assert last_result_text(client) == "('original runtime retained', True)\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_interrupts_running_python_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        library = temporary_path / "python-interrupt-checkpoint.dylib"
        source = (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "native"
            / "python_probe_checkpoint.c"
        )
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                library,
                source,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_PYTHON_INTERRUPT_LIBRARY"] = str(library)
        client = McpClient(binary, execution.serve(), environment)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                python_interrupt_started <- tempfile("python-interrupt-started-")
                python_interrupt_release <- tempfile("python-interrupt-release-")
                # Initialize from R before the Python evaluation checkpoint.
                invisible(reticulate::py_config())
                cat(python_interrupt_started, python_interrupt_release, sep = "\n")
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = last_result_text(client).splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = "<interrupt started>\n<interrupt release>"
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            # fmt: python
            python = code("""
                import ctypes
                import os
                import signal

                checkpoint_library = ctypes.PyDLL(os.environ["MCP_CONSOLE_PYTHON_INTERRUPT_LIBRARY"])
                wait_for_interrupt = checkpoint_library.wait_for_probe_interrupt
                wait_for_interrupt.argtypes = (
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_void_p,
                )
                wait_for_interrupt.restype = ctypes.c_int
                python_interrupt_state = 41
                wakeup_read, wakeup_write = os.pipe()
                os.set_blocking(wakeup_write, False)
                previous_wakeup = signal.set_wakeup_fd(wakeup_write)
                try:
                    with (
                        open(
                            r.python_interrupt_started,
                            "wb",
                            buffering=0,
                        ) as started,
                        open(
                            r.python_interrupt_release,
                            "rb",
                            buffering=0,
                        ) as release,
                    ):
                        assert (
                            wait_for_interrupt(
                                started.fileno(),
                                release.fileno(),
                                wakeup_read,
                                ctypes.pythonapi.PyErr_CheckSignals,
                            )
                            == 0
                        )
                finally:
                    signal.set_wakeup_fd(previous_wakeup)
                    os.close(wakeup_read)
                    os.close(wakeup_write)
                """)
            client.send(python=python, timeout_ms=0)
            assert (
                without_elapsed(last_result_text(client))
                == "\n[running; poll with an empty send]"
            )
            started.wait("Python evaluation entered native interrupt checkpoint")

            client.send(control="interrupt", timeout_ms=0)
            assert (
                without_elapsed(last_result_text(client))
                == "\n[running; poll with an empty send]"
            )
            release.release()
            release = None
            client.send()
            assert "KeyboardInterrupt" in last_result_text(client)

            client.send(
                # fmt: python
                python=code("""
                    python_interrupt_state + 1
                    """)
            )
            assert last_result_text(client) == "42\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(POSIX)
def test_interrupts_raw_python_stdin(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Complete startup before observing the blocking syscall.
        # fmt: r
        r = code("""
            raw_event_wait <- function() Sys.sleep(0.001)
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        # fmt: python
        python = code("""
            raw_state = object()
            original_raw_state = raw_state
            # Complete bridge attachment before the blocking-read checkpoint.
            r.raw_event_wait()
            """)
        client.send(python=python)
        assert last_result_text(client) == "[done]"
        # fmt: python
        python = code("""
            import os

            # Exercise R's temporary SIGINT handler inside the same Python cell.
            r.raw_event_wait()
            print("reading raw stdin", flush=True)
            try:
                os.read(0, 1)
            except KeyboardInterrupt:
                print("raw read interrupted")
            else:
                raise AssertionError("raw read completed without an interrupt")
            """)
        wait_for_evaluation_output(
            client,
            "reading raw stdin\n\n[running; poll with an empty send]",
            "Python raw stdin read",
            python=python,
            timeout_ms=0,
        )
        wait_for_evaluation_output(
            client,
            "raw read interrupted\n",
            "Python raw stdin interrupt",
            control="interrupt",
            timeout_ms=0,
        )
        client.send(python="raw_state is original_raw_state")
        assert last_result_text(client) == "True\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_nested_language_calls_once(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # Initialize Python from R and keep objects in both runtimes throughout.
        # fmt: r
        r = code(r"""
            nested_r_state <- new.env()
            nested_r_original <- nested_r_state
            nested_input <- function() readline("nested R> ")
            invisible(reticulate::py_config())
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        # fmt: python
        python = code(r"""
            import os
            import signal

            nested_state = object()
            nested_original = nested_state


            def signal_while_suspended():
                os.kill(os.getpid(), signal.SIGINT)
                print("Python completed while suspended")


            def read_while_suspended():
                global suspended_line
                suspended_line = input("suspended Python> ")
                print(suspended_line)


            try:
                r.nested_input()
            except KeyboardInterrupt:
                print("Python caught R interrupt")
            print("Python continued")
            """)
        client.send(python=python)
        assert last_result_text(client) == (
            '[input requested: "nested R> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "Python caught R interrupt\nPython continued\n",
            "Python-to-R interrupt acknowledged once",
            control="interrupt",
        )
        # fmt: r
        r = code(r"""
            tryCatch(
              reticulate::py_run_string("input('nested Python> ')"),
              interrupt = function(condition) cat("R caught Python interrupt\n")
            )
            cat("R continued\n")
            """)
        client.send(r=r)
        assert last_result_text(client) == (
            '[input requested: "nested Python> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "R caught Python interrupt\nR continued\n",
            "R-to-Python interrupt acknowledged once",
            control="interrupt",
        )
        # A signal raised while R suspends interrupts must let Python finish
        # its bytecode. Re-arming in the Python handler would spin here.
        # fmt: r
        r = code(r"""
            tryCatch(
              {
                suspendInterrupts(reticulate::py_run_string(
                  "signal_while_suspended()"
                ))
                Sys.sleep(0) # Explicitly check R interrupts inside the handler.
              },
              interrupt = function(condition) cat("R accepted deferred interrupt\n")
            )
            """)
        client.send(r=r)
        assert last_result_text(client) == (
            "Python completed while suspended\nR accepted deferred interrupt\n"
        ), repr(last_result_text(client))
        # Managed input also remains usable inside that suspended state.
        # fmt: r
        r = code(r"""
            tryCatch(
              {
                suspendInterrupts(reticulate::py_run_string(
                  "read_while_suspended()"
                ))
                Sys.sleep(0)
              },
              interrupt = function(condition) cat("R accepted input interrupt\n")
            )
            """)
        client.send(r=r)
        assert last_result_text(client) == (
            '[input requested: "suspended Python> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt", timeout_ms=0)
        assert last_result_text(client) == "\n[waiting for stdin]"
        wait_for_evaluation_output(
            client,
            "accepted\nR accepted input interrupt\n",
            "suspended Python input",
            stdin="accepted\n",
        )
        client.send(python="nested_state is nested_original")
        assert last_result_text(client) == "True\n"
        client.send(r="identical(nested_r_state, nested_r_original)")
        assert last_result_text(client) == "[1] TRUE\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_releases_python_threads_during_managed_input(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            input_release <- tempfile("input-thread-release-")
            input_completed <- tempfile("input-thread-completed-")
            cat(input_release, input_completed, sep = "\n")
            """)
        client.send(r=r)
        paths = last_result_text(client).splitlines()
        assert len(paths) == 2, paths
        client.transcript[-1]["result"]["content"][0]["text"] = (
            "<thread release>\n<thread completed>"
        )
        release, completed = [FifoCheckpoint.create(Path(path)) for path in paths]
        try:
            # fmt: python
            python = code(r"""
                import threading

                release_path = r.input_release
                completed_path = r.input_completed


                def input_thread():
                    with open(release_path, "rb", buffering=0) as gate:
                        assert gate.read(1) == b"1"
                    with open(completed_path, "wb", buffering=0) as receipt:
                        receipt.write(b"1")


                background = threading.Thread(target=input_thread, daemon=True)
                background.start()
                line = input("thread progress> ")
                background.join()
                print(line)
                """)
            client.send(python=python)
            assert last_result_text(client) == (
                '[input requested: "thread progress> "]\n[waiting for stdin]'
            )
            release.release()
            completed.wait("background Python progressed during managed input")
            wait_for_evaluation_output(
                client, "finished\n", "managed input completion", stdin="finished\n"
            )
            return client.finish()
        finally:
            release.close()
            completed.close()


@executions(DIRECT, SANDBOXED)
def test_initializes_private_runtime_once_on_first_python_cell(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # The shared runtime owns module setup. Observe the public initialization
    # hook instead of counting the removed R-side Matplotlib callback.
    # fmt: r
    r = code(r"""
        stopifnot(!reticulate::py_available(initialize = FALSE))
        initialization_count <- 0L
        setHook("reticulate.onPyInit", function() {
          initialization_count <<- initialization_count + 1L
        }, action = "append")
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]"
    client.send(
        python="runtime_identity = object(); runtime_identity_id = id(runtime_identity); 42"
    )
    assert last_result_text(client) == "42\n"
    client.send(python="assert id(runtime_identity) == runtime_identity_id; 43")
    assert last_result_text(client) == "43\n"
    client.send(r="initialization_count")
    assert last_result_text(client) == "[1] 0\n"
    client.send(r="invisible(reticulate::py_config()); initialization_count")
    assert last_result_text(client) == "[1] 1\n"
    client.send(python="assert id(runtime_identity) == runtime_identity_id; 44")
    assert last_result_text(client) == "44\n"
    client.send(r="invisible(reticulate::py_config()); initialization_count")
    assert last_result_text(client) == "[1] 1\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_python_runtime_initialization_after_interrupt(
    binary: Path,
    execution: Execution,
) -> Transcript:
    configuration_line = runtime_source_line("_defaults.apply(name)")
    numpy_line = runtime_source_line('if module.get_printoptions()["linewidth"] == 75:')
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        modules = Path(temporary_directory) / "modules"
        modules.mkdir()
        # Interrupt the shared setup boundary, independently of bridge hooks.
        # Restore the function before blocking so retry has no second gate.
        checkpoint = code("""
            import __main__
            import numpy as np

            __main__.runtime_identity = object()
            __main__.runtime_identity_id = id(__main__.runtime_identity)
            original_get_printoptions = np.get_printoptions
            def configuration_checkpoint():
                np.get_printoptions = original_get_printoptions
                input('python runtime configuring> ')
                return original_get_printoptions()
            np.get_printoptions = configuration_checkpoint
            """)
        (modules / "sitecustomize.py").write_text(
            f"exec(compile({json.dumps(checkpoint)}, '<runtime setup checkpoint>', 'exec'))"
        )
        environment["RETICULATE_PYTHONPATH"] = str(modules)
        client = McpClient(
            binary,
            execution.serve("-c", "python.managed.resolution=implicit"),
            environment,
        )
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(python="42")
            assert last_result_text(client) == (
                '[input requested: "python runtime configuring> "]\n[waiting for stdin]'
            ), last_result_text(client)

            client.send(control="interrupt", timeout_ms=0)
            result = client.transcript[-1]["result"]
            assert result["isError"] is False, result
            output = last_result_text(client)
            assert output == (
                "Traceback (most recent call last):\n"
                f'  File "<string>", line {configuration_line}, in _mcp_console_configure_module_defaults\n'
                f'  File "<string>", line {numpy_line}, in apply\n'
                '  File "<runtime setup checkpoint>", line 9, in configuration_checkpoint\n'
                '  File "<string>", line 50, in _console_input\n'
                "KeyboardInterrupt\n"
            ), repr(output)

            client.send(python="assert id(runtime_identity) == runtime_identity_id; 42")
            output = last_result_text(client)
            assert output == "42\n", repr(output)
            client.send(python="import yaml12; yaml12.__name__")
            output = last_result_text(client)
            assert output == (
                "[resolved PyPI distribution 'py-yaml12' "
                "for Python import 'yaml12']\n"
                "'yaml12'\n"
            ), repr(output)
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
def test_dispatches_cells_without_reticulate_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(python="direct_state = [41]")
        assert last_result_text(client) == "[done]"
        # Ordinary Python setup and evaluation do not call reticulate's
        # R evaluation entry points. This instrumentation is confined to R.
        # fmt: r
        r = code(r"""
            for (name in c("py_eval", "py_run_string")) {
              invisible(suppressMessages(base::trace(
                name,
                tracer = quote(stop("R-mediated Python cell dispatch")),
                print = FALSE,
                where = asNamespace("reticulate")
              )))
            }
            invisible(suppressMessages(base::trace(
              "readline",
              tracer = quote(stop("R-mediated Python input")),
              print = FALSE,
              where = baseenv()
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]"
        client.send(python="direct_state.append(42); direct_state")
        assert last_result_text(client) == "[41, 42]\n"
        client.send(python='input("direct> ")', stdin="still live\n")
        assert last_result_text(client) == (
            "[input requested: \"direct> \"]\n'still live'\n"
        )
        client.send(python='raise ValueError("direct exception")')
        assert last_result_text(client).endswith("ValueError: direct exception\n")
        client.send(python="direct_state")
        assert last_result_text(client) == "[41, 42]\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_dispatch_does_not_mutate_python_globals(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import threading

        globals_iteration_started = threading.Event()
        globals_iteration_continue = threading.Event()
        globals_iteration_result = []


        def iterate_globals():
            iterator = iter(globals())
            next(iterator)
            globals_iteration_started.set()
            globals_iteration_continue.wait()
            try:
                tuple(iterator)
                globals_iteration_result.append("stable")
            except BaseException as error:
                globals_iteration_result.append(repr(error))


        globals_iteration_thread = threading.Thread(target=iterate_globals)
        globals_iteration_thread.start()
        globals_iteration_started.wait()
        None
        """)
    client.send(python=python)
    assert last_result_text(client) == "[done]"
    # fmt: python
    python = code("""
        globals_iteration_continue.set()
        globals_iteration_thread.join()
        globals_iteration_result
        """)
    client.send(python=python)
    assert last_result_text(client) == "['stable']\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_live_python_resolver(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "mcp-console-blocked-live-preparation"
        )
        uv_interrupted = FifoCheckpoint.create(temporary / "uv-interrupted")
        uv_interrupt_release = FifoCheckpoint.create(temporary / "uv-interrupt-release")
        environment["MCP_CONSOLE_TEST_UV_INTERRUPTED"] = str(uv_interrupted.path)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPT_RELEASE"] = str(
            uv_interrupt_release.path
        )
        environment["RUST_LOG"] = "error"
        previous_handler = signal.signal(signal.SIGINT, signal.SIG_IGN)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        try:
            client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        finally:
            signal.signal(signal.SIGINT, previous_handler)
            signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        passed = False
        interrupt_released = False
        try:
            client.initialize_and_list_tools()
            client.send(r="resolver_interrupt_state <- 41L")
            assert last_result_text(client) == "[done]"

            preparation = client.start_send(
                r="resolver_interrupt_cell_ran <- TRUE",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            interrupt = client.start_send(control="interrupt", timeout_ms=0)
            uv_interrupted.wait("live Python resolver interrupt")
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert client.stdout in readable, (
                "control-only interrupt waited for Python preparation to settle"
            )
            client.receive(interrupt)
            assert without_elapsed_result(interrupt["result"]) == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "\n[phase: dependency preparation]"
                            "\n[running; poll with an empty send]"
                        ),
                    }
                ],
                "isError": False,
            }, interrupt

            uv_interrupt_release.release()
            interrupt_released = True
            client.receive(preparation)
            assert preparation["result"]["isError"] is True, preparation
            error = preparation["result"]["content"][0]["text"]
            assert "managed Python resolution" in error, error
            preparation["result"]["content"][0]["text"] = (
                "managed Python resolution cancelled by interrupt"
            )

            client.send()
            assert last_result_text(client) == "\n[idle]"

            client.send(
                r=(
                    "resolver_interrupt_state + "
                    "as.integer(!exists('resolver_interrupt_cell_ran'))"
                )
            )
            assert last_result_text(client) == "[1] 42\n"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not interrupt_released:
                uv_interrupt_release.release()
            uv_release.release()
            uv_started.close()
            uv_release.close()
            uv_interrupted.close()
            uv_interrupt_release.close()
            if not passed:
                stop_client(client)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_cancels_live_python_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "mcp-console-blocked-live-preparation"
        )
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
        )
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(r="restart_marker <- 42L")
            assert last_result_text(client) == "[done]"

            preparation = client.start_send(
                r="stop('cancelled requirements cell ran')",
                requirements={"python": ["mcp-console-blocked-live-preparation"]},
            )
            uv_started.wait("live Python preparation")

            calls_returned = threading.Event()
            forced_release = threading.Event()

            def release_if_calls_block() -> None:
                if not calls_returned.wait(2):
                    forced_release.set()
                    uv_release.release()

            watchdog = threading.Thread(target=release_if_calls_block)
            watchdog.start()
            poll = client.start_send()
            second_prepare = client.start_send(
                requirements={"python": ["py-yaml12"]},
            )
            client.receive_many([poll, second_prepare])
            calls_returned.set()
            watchdog.join()
            assert not forced_release.is_set(), (
                "another tool call waited for live preparation"
            )
            assert poll["result"] == {
                "content": [
                    {"type": "text", "text": "[session is preparing requirements]"}
                ],
                "isError": True,
            }, poll
            assert second_prepare["result"] == {
                "content": [
                    {"type": "text", "text": "session is preparing requirements"}
                ],
                "isError": True,
            }, second_prepare

            restart = client.start_send(control="restart")
            client.receive_many([preparation, restart])

            preparation_result = preparation["result"]
            assert preparation_result == {
                "content": [
                    {
                        "type": "text",
                        "text": "Python preparation cancelled by restart",
                    }
                ],
                "isError": True,
            }, preparation_result
            assert restart["result"]["content"] == [
                {
                    "type": "text",
                    "text": (
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n[idle]"
                    ),
                }
            ], restart

            # fmt: r
            r = code(r"""
                requirements <- reticulate::py_require()
                stopifnot(
                  !exists("restart_marker", inherits = FALSE),
                  !"mcp-console-blocked-live-preparation" %in% requirements$packages
                )
                """)
            client.send(r=r)
            assert last_result_text(client) == "[done]"
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            uv_release.release()
            uv_started.close()
            uv_release.close()
            if not passed:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
def test_does_not_parse_requirements_as_rscript_options(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        marker = Path(temporary_directory) / "host-r-code-ran"
        expression = (
            "base::writeLines('executed', base::Sys.getenv('MCP_CONSOLE_HOST_MARKER'))"
        )
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        environment["MCP_CONSOLE_HOST_MARKER"] = str(marker)
        client = McpClient(binary, execution.serve(), environment)
        client.initialize_and_list_tools()
        client.send(
            requirements={"python": ["-e", expression]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert not marker.exists(), "requirement executed as unsandboxed R code"
        assert result["content"][0]["text"] == named_requirement_error("-e")
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_forces_uv_offline_in_builtin_worker(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment.pop("RETICULATE_PYTHON", None)
    environment["UV_OFFLINE"] = "0"
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("UV_OFFLINE", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "1"\n'
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
