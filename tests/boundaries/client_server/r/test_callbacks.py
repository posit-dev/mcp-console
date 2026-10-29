#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R_EVENT_LOOP, requires
from support.assertions import (
    assert_result_content,
    last_tool_text,
    release_worker_callback_gate,
    wait_for_evaluation_output,
    wait_for_idle_output,
)
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_input_handler_client, r_test_environment, reference_plots
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_evaluates_a_complete_cell(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        answer <- 40
        answer + 1
        answer + 2
        cat("done\n")
        invisible(99)
        """)
    client.send(r=r)

    # fmt: r
    r = code(r"""
        identical(
          as.vector(splines::splineDesign(
            knots = c(0, 0, 0, 0, 1, 1, 1, 1),
            x = 0.5
          )),
          c(0.125, 0.375, 0.375, 0.125)
        )
        """)
    client.send(r=r)
    client.send(r='stop("boom")')
    client.send(r="answer")
    client.send(r="silent <- 1")
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_services_r_input_handlers_at_cell_boundaries(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, directory):
        client.initialize_and_list_tools()

        # Make the handler ready before the worker's final boundary turn.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "cell-end-handler-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() cat("cell end callback\n")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            """)
        client.send(r=r)
        assert last_tool_text(client) == "cell end callback\n"

        # Register an input handler while its FIFO is empty, then make the
        # descriptor readable before submitting the next cell. Whether the
        # handler runs while idle or during the initial boundary turn, the
        # submitted source must observe its state change.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            cell_start_callback_ran <- FALSE
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "cell-start-handler-fifo"),
              function() cell_start_callback_ran <<- TRUE
            ))
            """)
        client.send(r=r)
        output = last_tool_text(client)
        assert output == "[done]", repr(output)
        fifo = wait_for_worker_file(
            directory,
            "cell-start-handler-fifo",
            client,
        )
        fifo.write_bytes(b"x")
        client.send(r='cat(cell_start_callback_ran, "\\ncell body\\n", sep = "")')
        assert last_tool_text(client) == "TRUE\ncell body\n"
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_services_later_callbacks_while_idle(
    binary: Path, execution: Execution
) -> Transcript:
    return services_later_callbacks_while_idle(binary, execution, "r")


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_services_later_callbacks_without_another_r_cell(
    binary: Path, execution: Execution
) -> Transcript:
    records = None
    for language in ("python", "sql"):
        current = services_later_callbacks_while_idle(binary, execution, language)
        if records is None:
            records = current
    assert records is not None
    return records


def services_later_callbacks_while_idle(
    binary: Path, execution: Execution, language: str
) -> Transcript:
    relay = Path(__file__).resolve().parents[3] / "fixtures" / "idle_callback_relay"
    with tempfile.TemporaryDirectory() as temporary_directory:
        directory = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_BINARY"] = str(binary)
        with McpClient(
            binary,
            execution.serve("--worker", str(binary), "--relay", str(relay)),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            packages = ["later"]
            if language == "sql":
                # A custom worker does not receive the built-in runtime's
                # infrastructure packages. Declare the SQL adapter's imports.
                packages += [
                    "DBI",
                    "duckdb",
                    "nanoarrow",
                    "pillar",
                    "tibble",
                    "utf8",
                ]
            client.send(requirements={"r": packages})
            assert last_tool_text(client) == "[prepared]", client.transcript[-1]

            # fmt: r
            r = code(r"""
                callback_gate <- tempfile("mcp-console-callback-gate-")
                callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
                run_callback <- function() {
                  if (!file.exists(callback_gate)) {
                    later::later(run_callback, delay = 0.01)
                    return(invisible(NULL))
                  }
                  idle_value <<- 42
                  cat("idle callback\n")
                  stopifnot(file.create(callback_checkpoint))
                }
                later::later(run_callback, delay = 0.01)
                cat(callback_gate, callback_checkpoint, sep = "\n")
                """)
            client.send(r=r)
            checkpoint = FifoCheckpoint.attach(
                wait_for_worker_file(
                    directory, "idle-callback-output-processed", client
                )
            )
            try:
                release_worker_callback_gate(client, "idle callback")
                # A worker-local file does not prove the server has received
                # its output. Wait for the relay's server round trip as well.
                checkpoint.wait()
                source = {
                    "r": "idle_value",
                    "python": "print('python after callback')",
                    "sql": "SELECT 42 AS answer",
                }[language]
                client.send(**{language: source})
                output = last_tool_text(client)
                prefix = "idle callback\n[output produced while idle]\n"
                assert output.startswith(prefix), repr(output)
                result = output.removeprefix(prefix)
                if language == "r":
                    assert result == "[1] 42\n", repr(output)
                elif language == "python":
                    assert result == "python after callback\n", repr(output)
                else:
                    assert (
                        result == "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n"
                    ), repr(output)
                return client.finish()
            finally:
                checkpoint.close()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_collects_idle_later_callbacks_with_empty_send(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})
    client.send(session="default")
    assert last_tool_text(client) == "\n[idle]"

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          collected_value <<- 84
          cat("collected callback")
          stopifnot(file.create(callback_checkpoint))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "collected callback")
    wait_for_idle_output(
        client,
        "collected callback\n[idle]",
        "collected callback output",
    )
    client.send(r="collected_value")
    assert last_tool_text(client) == "[1] 84\n"
    return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_snapshots_output_while_idle_later_callback_runs(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        callback_release <- tempfile("mcp-console-callback-release-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          while (!file.exists(callback_release)) {
            Sys.sleep(0.01)
          }
          cat("long callback")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, callback_release, sep = "\n")
        """)
    client.send(r=r)
    (callback_release,) = release_worker_callback_gate(
        client,
        "long idle callback",
        ("release",),
    )
    client.send(session="default", timeout_ms=10)
    assert last_tool_text(client) == "\n[idle]"
    callback_release.touch()
    deadline = time.monotonic() + 3
    poll_start = len(client.transcript)
    while True:
        client.send(session="default")
        if last_tool_text(client) == "long callback\n[idle]":
            break
        assert last_tool_text(client) == "\n[idle]"
        if time.monotonic() >= deadline:
            raise AssertionError("idle callback output was not collected")
    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    client.transcript[poll_start:] = [final_poll]
    return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_restarts_while_idle_callback_runs(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          cat("callback started")
          stopifnot(file.create(callback_checkpoint))
          repeat {
            Sys.sleep(1)
          }
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "restarted idle callback")
    wait_for_idle_output(
        client,
        "callback started\n[idle]",
        "idle callback output",
        timeout_ms=10,
    )
    client.send(control="restart")
    assert last_tool_text(client) == (
        "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    )
    return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_returns_plots_from_idle_later_callbacks(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          cat("idle plot\n")
          plot(1:3)
          stopifnot(file.create(callback_checkpoint))
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle plot callback")
    expected_plot = reference_plots(
        rscript,
        environment,
        "plot(1:3)",
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(session="default")
    assert_result_content(
        client,
        ["idle plot\n", expected_plot[0], "\n[idle]"],
    )
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_stops_cell_after_boundary_callback_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, directory):
        client.initialize_and_list_tools()

        # Create the finalized page without read permissions. The PNG device
        # can write through its open descriptor, but publication cannot reopen
        # it.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            invisible(.Call(
              "mcp_test_register_input_handler",
              file.path(tempdir(), "failing-handler-fifo"),
              function() {
                plot(1)
                Sys.chmod(list.files(
                  file.path(tempdir(), "mcp-console-plots"),
                  full.names = TRUE
                ), "0000")
                old_umask <- Sys.umask("0777")
                on.exit(Sys.umask(old_umask), add = TRUE)
                grDevices::dev.off()
                Sys.umask(old_umask)
                on.exit(NULL)
              }
            ))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]"
        fifo = wait_for_worker_file(directory, "failing-handler-fifo", client)
        fifo.write_bytes(b"x")

        client.send(r='system("printf boundary-cell-ran")')
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        assert "boundary-cell-ran" not in output, output
        assert "failed to read managed plot" in output, output
        assert output.endswith(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        ), output
        result["content"][0]["text"] = (
            "[failed to read managed plot `<worker plot>`: permission denied]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_skips_final_boundary_callbacks_after_cell_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, _directory):
        client.initialize_and_list_tools()

        # Record a plot publication failure during the cell after making an
        # input handler ready for the final boundary turn. The handler writes
        # directly to stdout so its execution would remain observable after
        # sideband publication fails.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            callback_fifo <- file.path(tempdir(), "skipped-handler-fifo")
            invisible(.Call(
              "mcp_test_register_input_handler",
              callback_fifo,
              function() system("printf final-boundary-callback-ran")
            ))
            writer <- fifo(callback_fifo, open = "wb")
            writeBin(as.raw(1), writer)
            close(writer)
            plot(1)
            Sys.chmod(list.files(
              file.path(tempdir(), "mcp-console-plots"),
              full.names = TRUE
            ), "0000")
            old_umask <- Sys.umask("0777")
            on.exit(Sys.umask(old_umask), add = TRUE)
            grDevices::dev.off()
            Sys.umask(old_umask)
            on.exit(NULL)
            "cell completed"
            """)
        client.send(r=r)
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        output = result["content"][0]["text"]
        assert "final-boundary-callback-ran" not in output, output
        assert "failed to read managed plot" in output, output
        assert output.endswith(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        ), output
        result["content"][0]["text"] = (
            "[failed to read managed plot `<worker plot>`: permission denied]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_routes_input_to_idle_later_callbacks_before_a_cell(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          stopifnot(file.create(callback_checkpoint))
          idle_answer <<- readline("later> ")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle input callback")
    wait_for_idle_output(
        client,
        '[input requested: "later> "]\n[waiting for stdin]',
        "idle callback input request",
    )
    wait_for_evaluation_output(
        client,
        "cell: yes\n",
        "idle callback input before cell",
        r='cat("cell: ", idle_answer, "\\n", sep = "")',
        stdin="yes\n",
    )
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
