#!/usr/bin/env -S uv run --script

import os
import re
import sys
import tempfile
import time
from contextlib import ExitStack, closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import execution_snapshots
from support.progress import without_elapsed
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import build_r_input_handler, r_input_handler_client, r_test_environment
from support.native import build_interposer
from support.requirements import (
    NATIVE_FIXTURES,
    NULL_FAULT_ACCERR,
    NULL_FAULT_MAPERR,
    POSIX,
    requires,
)
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_evaluates_source_without_final_newline(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 2
        """).removesuffix("\n")
    assert not r.endswith("\n")
    client.send(r=r)
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_recoverable_language_errors(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="answer <- 41")
    # fmt: r
    r = code(r"""
        g <- function() stop("boom")
        f <- function() g()
        f()
        """)
    client.send(r=r)
    # fmt: r
    r = code(r"""
        traceback()
        """)
    client.send(r=r)
    # Trigger an error after evaluation while R auto-prints the visible result.
    # fmt: r
    r = code(r"""
        print.auto_print_failure <- function(...) {
          stop("print failed")
        }
        structure(1, class = "auto_print_failure")
        """)
    client.send(r=r)
    client.send(r="answer")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, NULL_FAULT_MAPERR)
def test_restarts_after_r_worker_segfault(
    binary: Path, execution: Execution
) -> Transcript:
    return r_segfault_transcript(binary, execution, "memory not mapped")


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES, NULL_FAULT_ACCERR)
def test_restarts_after_r_worker_segfault_with_permission_diagnostic(
    binary: Path, execution: Execution
) -> Transcript:
    return r_segfault_transcript(binary, execution, "invalid permissions")


def r_segfault_transcript(binary: Path, execution: Execution, cause: str) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        environment["MCP_CONSOLE_TEST_SEGFAULT_LIBRARY"] = str(
            build_interposer(Path(directory), "r_segfault")
        )
        with McpClient(binary, execution.serve(), environment) as client:
            return restart_after_r_segfault(client, cause)


def restart_after_r_segfault(client: McpClient, cause: str) -> Transcript:
    client.initialize_and_list_tools()
    client.send(r="r_worker_marker <- TRUE")

    # Observe R's fatal-signal menu before choosing to abort.
    # fmt: r
    r = code(r"""
        dyn.load(Sys.getenv("MCP_CONSOLE_TEST_SEGFAULT_LIBRARY"))
        .C("mcp_test_segfault")
        """)
    client.send(r=r)
    fatal_output = last_tool_text(client)
    assert "Possible actions:\n1: abort (with core dump, if enabled)\n" in fatal_output
    assert fatal_output.endswith(
        '[input requested: "Selection: "]\n[waiting for stdin]'
    ), repr(fatal_output)

    # libc formats null as either (nil) or 0x0; preserve the cause and traceback.
    normalized, count = re.subn(
        rf"(?m)^address (?:\(nil\)|0x0), cause '{cause}'$",
        f"address 0x0, cause '{cause}'",
        fatal_output,
    )
    assert count == 1, fatal_output
    assert normalized.startswith(
        "\n *** caught segfault ***\n"
        f"address 0x0, cause '{cause}'\n"
        """
Traceback:
 1: .C("mcp_test_segfault")
"""
    ), repr(fatal_output)
    client.transcript[-1]["result"]["content"][0]["text"] = normalized
    wait_for_evaluation_output(
        client,
        "R is aborting now ...\n"
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker terminated by signal 11]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]",
        "R fatal-signal abort and replacement",
        expected_error=True,
        completion_timeout_seconds=60,
        stdin="1\n",
    )

    client.send(r='exists("r_worker_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    client.send(r="1 + 1")
    assert last_tool_text(client) == "[1] 2\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_r_worker_exit_status(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        quit(save = "no", status = 33L, runLast = FALSE)
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker exited with status 33]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    client.send(r="1 + 1")
    assert last_tool_text(client) == "[1] 2\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_reports_r_worker_restart_with_idle_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="invisible(NULL)")

    # fmt: r
    r = code(r"""
        tools::pskill(Sys.getpid(), signal = 9L)
        """).removesuffix("\n")
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result["isError"] is True
    assert result["content"][0]["text"] == (
        "[worker sideband read failed: worker sideband closed]\n"
        "[worker terminated by signal 9]\n"
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    )

    client.send(stdin="replacement\n")
    assert last_tool_text(client) == "\n[idle]"

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)
    client.send(r=direct_stdin)
    assert last_tool_text(client) == '[1] "replacement"\n'
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restarts_and_evaluates_r_cell_in_one_send(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="inline_restart_marker <- TRUE")

    client.send(
        control="restart",
        r='exists("inline_restart_marker", inherits = FALSE)',
    )
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n"
        "[1] FALSE\n[done]"
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restart_while_r_waits_for_input(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        restart_marker <- TRUE
        readline("restart> ")
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        '[input requested: "restart> "]\n[waiting for stdin]'
    )

    client.send(control="restart")
    output = last_tool_text(client)
    assert output == (
        '[1] ""\n[active evaluation stopped by session restart request]\n'
        "[worker stopped: in-memory state lost]\n"
        "[starting new worker]\n"
        "[idle]"
    ), repr(output)

    client.send(r='exists("restart_marker", inherits = FALSE)')
    assert last_tool_text(client) == "[1] FALSE\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_skips_cell_boundary_callbacks(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, directory):
        client.initialize_and_list_tools()

        # Leave a callback ready for the initial boundary turn. Restart after
        # it requests input, and verify that the submitted cell is never
        # dispatched.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "initial-boundary-fifo"),
              function() readline("callback> ")
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(directory, "initial-boundary-fifo", client)
        fifo.write_bytes(b"x")
        client.send(r='cat("cell body ran\\n")')
        assert last_tool_text(client) == (
            '[input requested: "callback> "]\n[waiting for stdin]'
        )
        client.send(control="restart")
        assert "cell body ran" not in last_tool_text(client)

        # Make the next callback ready before the cell blocks, then restart
        # without letting the worker run its final boundary turn.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "final-boundary-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() cat("post-cell callback ran\n")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            readline("cell> ")
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "cell> "]\n[waiting for stdin]'
        )
        client.send(control="restart")
        assert "post-cell callback ran" not in last_tool_text(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_skips_direct_stdin_boundary_callback(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, directory):
        client.initialize_and_list_tools()

        # A direct fd-0 read bypasses the worker's ReadConsole callback.
        # Restart still must prevent the submitted cell from running after EOF
        # releases it.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "direct-stdin-boundary-fifo"),
              function() {
                connection <- suppressWarnings(file("/dev/stdin"))
                on.exit(close(connection))
                stopifnot(file.create(file.path(
                  tempdir(),
                  "direct-stdin-boundary-checkpoint"
                )))
                readLines(connection, n = 1)
                cat("direct callback released\n")
              }
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(
            directory,
            "direct-stdin-boundary-fifo",
            client,
        )
        fifo.write_bytes(b"x")

        # The callback can start while the worker is idle, so its checkpoint
        # does not prove that the server has admitted this evaluation.
        # Receive the running response before requesting restart.
        client.send(
            r='cat("direct stdin cell ran\\n")',
            timeout_ms=0,
        )
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        wait_for_worker_file(
            directory,
            "direct-stdin-boundary-checkpoint",
            client,
        )

        client.send(control="restart")
        output = last_tool_text(client)
        assert output == (
            "direct callback released\n"
            "[active evaluation stopped by session restart request]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        ), output
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_browser_input(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        step <- function() {
          value <- 1
          browser()
          value <- value + 1
          value <- value + 1
          value
        }
        step()
        """)
    client.expect(
        "Called from: step()\n"
        '[input requested: "Browse[1]> "]\n'
        "debug at #4: value <- value + 1\n"
        '[input requested: "Browse[1]> "]\n'
        "debug at #5: value <- value + 1\n"
        '[input requested: "Browse[1]> "]\n'
        "debug at #6: value\n"
        '[input requested: "Browse[1]> "]\n'
        "[waiting for stdin]",
        r=r,
        stdin="n\nn\nn\n",
    )
    client.send(r="1")
    assert client.transcript[-1]["result"]["isError"] is True
    wait_for_evaluation_output(
        client,
        "[1] 3\n",
        "R browser input",
        stdin="c\n",
    )
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_times_out_and_polls_running_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="invisible(NULL)")
    # fmt: r
    r = code(r"""
        Sys.sleep(0.25)
        answer <- 42
        answer
        """)
    client.send(r=r, timeout_ms=10)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert without_elapsed(output) == "\n[running; poll with an empty send]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "[1] 42\n", output
    client.send(r="answer + 1")
    return client.finish()


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupts_running_r_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory, ExitStack() as fixtures:
        temporary_path = Path(temporary_directory)
        environment, rscript = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        build_r_input_handler(temporary_path, environment, rscript)
        started = fixtures.enter_context(
            closing(FifoCheckpoint.create(temporary_path / "r-interrupt-started"))
        )
        boundary_started = fixtures.enter_context(
            closing(
                FifoCheckpoint.create(temporary_path / "r-boundary-interrupt-started")
            )
        )
        # fmt: python
        launcher = code("""
            import os
            import signal
            import sys

            signal.signal(signal.SIGINT, signal.SIG_IGN)
            signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        arguments = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        client = McpClient(
            Path(sys.executable),
            ("-c", launcher, str(binary), *arguments),
            environment,
            current_directory=temporary_path,
        )
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(requirements={"r": ["DBI"]})
            assert last_tool_text(client) == "[prepared]"
            client.expect(r="invisible(NULL)")
            # fmt: r
            r = code(r"""
                interrupt_state <- 41L
                checkpoint <- fifo("r-interrupt-started", open = "wb")
                writeBin(charToRaw("1"), checkpoint)
                close(checkpoint)
                repeat {
                  Sys.sleep(60)
                }
                """)
            client.send(r=r, timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            started.wait("R evaluation reached its interruptible wait")

            # An interrupt can leave R unwinding after the 100 ms grace.
            # Collect the interrupted evaluation before submitting another cell.
            wait_for_evaluation_output(
                client,
                "\n",
                "running R interruption",
                control="interrupt",
                timeout_ms=3_000,
            )
            client.send(r="interrupt_state + 1L")
            assert last_tool_text(client) == "[1] 42\n"

            # fmt: r
            r = code(r"""
                dyn.load("./mcp_test_input_handler.so")
                invisible(.Call(
                  "mcp_test_register_input_handler",
                  file.path(tempdir(), "input-handler-fifo"),
                  function() {
                    # Install cleanup before the checkpoint permits interruption.
                    on.exit(boundary_interrupt_cleanup <<- TRUE)
                    checkpoint <- fifo("r-boundary-interrupt-started", open = "wb")
                    writeBin(charToRaw("1"), checkpoint)
                    close(checkpoint)
                    repeat {
                      Sys.sleep(60)
                    }
                  }
                ))
                boundary_interrupt_state <- 42L
                writer <- fifo(
                  file.path(tempdir(), "input-handler-fifo"),
                  open = "wb"
                )
                writeBin(as.raw(1), writer)
                close(writer)
                """)
            client.send(r=r, timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            boundary_started.wait("R input handler reached its interruptible wait")

            wait_for_evaluation_output(
                client,
                "\n",
                "R input-handler interruption",
                control="interrupt",
                timeout_ms=0,
            )
            client.send(r="c(boundary_interrupt_state, boundary_interrupt_cleanup)")
            output = last_tool_text(client)
            assert output == "[1] 42  1\n", output

            client.send(control="interrupt", timeout_ms=0)
            assert last_tool_text(client) == "\n\n[idle]"
            # The controlled send reports idle state after the interrupt
            # grace. The following calls verify that later source still runs.
            client.send(r="idle_interrupt_state <- 42L")
            assert last_tool_text(client) == "[done]"
            client.send(r="idle_interrupt_state")
            assert last_tool_text(client) == "[1] 42\n"

            # Boundary checks must not process elapsed-time limits when no
            # interrupt is pending; R resets the limit when the cell begins.
            # fmt: r
            r = code(r"""
                setTimeLimit(elapsed = 10, transient = FALSE)
                invisible(NULL)
                setTimeLimit(elapsed = 0.05, transient = FALSE)
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"
            time.sleep(0.1)
            client.send(r='"idle time does not consume a cell limit"')
            assert last_tool_text(client) == (
                '[1] "idle time does not consume a cell limit"\n'
            )
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
def test_interrupts_managed_console_input(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    passed = False
    try:
        client.initialize_and_list_tools()

        # fmt: r
        r = code(r"""
            r_input_state <- 41L
            tryCatch(
              readline("R interrupt> "),
              interrupt = function(condition) cat("R input interrupted\n")
            )
            """)
        client.send(r=r, stdin="R partial")
        assert last_tool_text(client) == (
            '[input requested: "R interrupt> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "R input interrupted\n",
            "interrupted R console input",
            control="interrupt",
            timeout_ms=0,
        )
        wait_for_evaluation_output(
            client,
            '[input requested: "R replay> "]\n[1] "R partial!"\n',
            "interrupted R input replay",
            r='readline("R replay> ")',
            stdin="!\n",
        )

        # fmt: r
        r = code(r"""
            suspended_input <- "not read"
            suspendInterrupts({
              suspended_input <- readline("R suspended> ")
              cat("suspended input accepted")
            })
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "R suspended> "]\n[waiting for stdin]'
        )
        client.send(control="interrupt", timeout_ms=0)
        assert last_tool_text(client) == "\n[waiting for stdin]"
        wait_for_evaluation_output(
            client,
            "suspended input accepted\n",
            "suspended R console input",
            stdin="accepted\n",
            timeout_ms=3_000,
        )
        client.send(r="suspended_input")
        assert last_tool_text(client) == '[1] "accepted"\n'

        # fmt: python
        python = code("""
            python_input_state = 41
            try:
                input("Python interrupt> ")
            except KeyboardInterrupt:
                print("Python input interrupted")
            """)
        client.send(python=python, stdin="Python partial")
        assert last_tool_text(client) == (
            '[input requested: "Python interrupt> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "Python input interrupted\n",
            "interrupted Python console input",
            control="interrupt",
            timeout_ms=0,
        )
        wait_for_evaluation_output(
            client,
            "[input requested: \"Python replay> \"]\n'Python partial!'\n",
            "interrupted Python input replay",
            python='input("Python replay> ")',
            stdin="!\n",
        )

        client.send(r="r_input_state + 1L")
        assert last_tool_text(client) == "[1] 42\n"
        client.send(python="python_input_state + 1")
        assert last_tool_text(client) == "42\n"
        transcript = client.finish()
        passed = True
        return transcript
    finally:
        if not passed:
            stop_client(client)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_replays_console_prefix_after_operation_boundary_interrupt(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, directory):
        client.initialize_and_list_tools()

        # A small native buffer makes a later managed callback deterministic
        # without relying on the server's 10 millisecond request grace.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            tryCatch(
              .Call(
                "mcp_test_read_console_line",
                "small callbacks> "
              ),
              interrupt = function(condition) {
                cat("caught later-callback interrupt\n")
              }
            )
            """)
        client.send(r=r, stdin="part")
        assert last_tool_text(client) == (
            '[input requested: "small callbacks> "]\n'
            '[input requested: "small callbacks> "]\n'
            "[waiting for stdin]"
        )

        wait_for_evaluation_output(
            client,
            "caught later-callback interrupt\n",
            "later console callback interrupt",
            control="interrupt",
            timeout_ms=0,
        )

        client.send(r='readline("after later callback> ")', stdin="!\n")
        assert last_tool_text(client) == (
            '[input requested: "after later callback> "]\n[1] "part!"\n'
        )

        # Return one full console buffer, then interrupt before another
        # callback can complete the logical line.
        # fmt: r
        r = code(r"""
            tryCatch(
              {
                checkpoint <- fifo(
                  file.path(tempdir(), "between-console-callbacks-started"),
                  open = "w+b"
                )
                partial <- .Call(
                  "mcp_test_read_console_once",
                  "between callbacks> "
                )
                stopifnot(nchar(partial, type = "bytes") == 3L)
                writeBin(as.raw(49), checkpoint)
                close(checkpoint)
                repeat {
                  Sys.sleep(60)
                }
              },
              interrupt = function(condition) {
                cat("caught between-callback interrupt\n")
              }
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "between callbacks> "]\n[waiting for stdin]'
        )
        checkpoint = wait_for_worker_file(
            directory, "between-console-callbacks-started", client
        )
        with closing(FifoCheckpoint.attach(checkpoint)) as started:
            # Writing stdin does not acknowledge its consumption. Collect through
            # the worker's running state, then wait for the completed callback.
            wait_for_evaluation_output(
                client,
                "\n[running; poll with an empty send]",
                "console callback to consume stdin",
                stdin="x" * 6,
                timeout_ms=50,
            )
            started.wait()

        wait_for_evaluation_output(
            client,
            "caught between-callback interrupt\n",
            "between-callback interrupt",
            control="interrupt",
            timeout_ms=0,
        )

        client.send(
            r='identical(readline("after boundary> "), paste0(strrep("x", 6), "!"))',
            stdin="!\n",
        )
        output = last_tool_text(client)
        assert output == ('[input requested: "after boundary> "]\n[1] TRUE\n'), repr(
            output
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
