#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import RUNNING, without_elapsed, without_elapsed_result
from support.assertions import (
    collect_running_output,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_input_handler_client
from support.records import Transcript
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_routes_idle_and_timed_out_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: r
    direct_stdin = code(r"""
        local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        """)

    client.send(stdin="cold fd 0\n")
    assert last_tool_text(client) == "\n[idle]"
    client.send(r=direct_stdin)
    assert last_tool_text(client) == '[1] "cold fd 0"\n'

    # fmt: r
    r = code(r"""
        prompted <- readline("bundled> ")
        direct <- local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        paste(prompted, direct, sep = "|")
        """)
    # Observe the prompt before asserting that later polls have no new output.
    # Same-call stdin can be consumed before its prompt notice reaches the server.
    wait_for_evaluation_output(
        client,
        '[input requested: "bundled> "]\n' + RUNNING,
        "bundled console stdin before the direct fd 0 read",
        r=r,
        stdin="café\n",
        timeout_ms=50,
    )
    client.send(timeout_ms=0)
    assert (
        without_elapsed(last_tool_text(client))
        == "\n[running; poll with an empty send]"
    )
    client.send(stdin="timed out ", timeout_ms=50)
    assert (
        without_elapsed(last_tool_text(client))
        == "\n[running; poll with an empty send]"
    )
    wait_for_evaluation_output(
        client,
        '[1] "café|timed out fd 0"\n',
        "partial direct fd 0 input completion",
        stdin="fd 0\n",
        timeout_ms=3_000,
    )
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_routes_combined_and_followup_stdin(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["DBI"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        first <- readline("first> ")
        second <- readline("second> ")
        cat(paste(first, second, sep = "|"), "\n", sep = "")
        """)
    # Same-call stdin is transport-ordered, but fd 0 consumption can lag the
    # input-exposure response. Accumulate exact public output cuts in order.
    wait_for_evaluation_output(
        client,
        """[input requested: "first> "]
[input requested: "second> "]
Ada|Lovelace
""",
        "combined same-call stdin",
        # The first R cell includes cold interpreter startup; subsequent input
        # observations use the ordinary arrival budget after this completes.
        completion_timeout_seconds=client.response_timeout,
        r=r,
        stdin="Ada\nLovelace\n",
    )

    # fmt: r
    r = code(r"""
        direct <- local({
          connection <- suppressWarnings(file("/dev/stdin"))
          on.exit(close(connection))
          readLines(connection, n = 1)
        })
        prompted <- readline("after> ")
        cat(paste(direct, prompted, sep = "|"), "\n", sep = "")
        """)
    wait_for_evaluation_output(
        client,
        '[input requested: "after> "]\n[waiting for stdin]',
        "console read after direct stdin",
        r=r,
        stdin="direct\n",
        timeout_ms=1_000,
    )
    wait_for_evaluation_output(
        client,
        "direct|callback\n",
        "follow-up console stdin",
        stdin="callback\n",
    )

    # fmt: r
    r = code(r"""
        paste("color", readline("color> "))
        """)
    client.send(r=r)
    assert last_tool_text(client) == '[input requested: "color> "]\n[waiting for stdin]'
    client.send(stdin="bl", timeout_ms=50)
    assert last_tool_text(client) == "\n[waiting for stdin]"
    wait_for_evaluation_output(
        client,
        '[1] "color blue"\n',
        "partial console stdin completion",
        stdin="ue\n",
    )

    # fmt: r
    r = code(r"""
        prompt <- paste0('quoted "prompt"', "\n", "> ")
        invisible(readline(prompt))
        """)
    wait_for_evaluation_output(
        client,
        '[input requested: "quoted \\"prompt\\"\\n> "]\n',
        "quoted console prompt",
        r=r,
        stdin="accepted\n",
    )
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_fd0_order_between_readers(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    released = False
    finished = False
    checkpoints: list[FifoCheckpoint] = []
    try:
        client.initialize_and_list_tools()

        # Create the FIFOs inside the worker's private writable directory.
        # fmt: r
        r = code(r"""
            fd0_order_started <- tempfile("mcp-console-fd0-order-started-")
            fd0_order_release <- tempfile("mcp-console-fd0-order-release-")
            cat(fd0_order_started, fd0_order_release, sep = "\n")
            """)
        client.send(r=r)
        setup = client.transcript[-1]["result"]
        paths = setup["content"][0]["text"].splitlines()
        assert len(paths) == 2, setup
        setup["content"][0]["text"] = (
            "<fd 0 started checkpoint>\n<fd 0 release checkpoint>"
        )
        started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
        checkpoints.extend((started, release))

        # Hold the worker after same-call stdin has been queued but before
        # either reader can consume it. The following public poll then observes
        # both reads independent of process scheduling.
        # fmt: r
        r = code(r"""
            started <- fifo(fd0_order_started, open = "wb", blocking = TRUE)
            writeBin(charToRaw("1"), started)
            close(started)
            release <- fifo(fd0_order_release, open = "rb", blocking = TRUE)
            stopifnot(identical(
              readBin(release, "raw", n = 1L),
              charToRaw("1")
            ))
            close(release)

            prompted <- readline("callback> ")
            direct <- local({
              connection <- suppressWarnings(file("/dev/stdin"))
              on.exit(close(connection))
              readLines(connection, n = 1)
            })
            cat(paste(prompted, direct, sep = "|"), "\n", sep = "")
            """)
        evaluation = client.start_send(
            r=r,
            stdin="callback\ndirect\n",
            timeout_ms=0,
        )
        started.wait("ordered fd 0 readers")
        client.receive(evaluation)
        assert without_elapsed_result(evaluation["result"])["content"] == [
            {
                "type": "text",
                "text": "\n[running; poll with an empty send]",
            }
        ], evaluation
        release.release()
        released = True

        client.send()
        expected = '[input requested: "callback> "]\ncallback|direct\n'
        assert last_tool_text(client) == expected
        evaluation["result"] = client.transcript[-1]["result"]
        client.transcript[-2:] = [evaluation]

        transcript = client.finish()
        finished = True
        return transcript
    finally:
        if checkpoints and not released:
            release.release()
        for checkpoint in checkpoints:
            checkpoint.close()
        if not finished:
            stop_client(client)


@requires(POSIX)
@executions(RUNTIME)
def test_preserves_utf8_across_console_reads(
    binary: Path, execution: Execution
) -> Transcript:
    with r_input_handler_client(binary, execution) as (client, _):
        client.initialize_and_list_tools()

        # The four-byte native buffer splits the two-byte character across
        # callbacks without making thousands of single-byte reads.
        # fmt: r
        r = code(r"""
            dyn.load("./mcp_test_input_handler.so")
            first <- .Call("mcp_test_read_console_once", "short> ")
            second <- .Call("mcp_test_read_console_once", "short> ")
            bytes <- c(charToRaw(first), charToRaw(second))
            value <- rawToChar(bytes[bytes != as.raw(10)])
            Encoding(value) <- "UTF-8"
            cat(
              paste(nchar(value, type = "bytes"), endsWith(value, "é")),
              "\n",
              sep = ""
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            '[input requested: "short> "]\n[waiting for stdin]'
        )

        client.send(stdin="xx")
        assert last_tool_text(client) == "\n[waiting for stdin]"

        wait_for_evaluation_output(
            client,
            '[input requested: "short> "]\n[waiting for stdin]',
            "second UTF-8 console read",
            stdin="é",
        )

        wait_for_evaluation_output(
            client,
            "4 TRUE\n",
            "UTF-8 console input",
            stdin="\n",
            timeout_ms=3_000,
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_keeps_stdin_open_after_partial_payload(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["DBI"]})
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        cat("before\n")
        value <- readline("partial> ")
        value
        """)
    client.send(r=r, stdin="without newline", timeout_ms=0)
    assert (
        without_elapsed(last_tool_text(client))
        == "\n[running; poll with an empty send]"
    )

    cuts = collect_running_output(
        client,
        "partial stdin input request",
        timeouts_ms=(3_000,) * 5,
    )
    output = "".join(cuts)
    expected = 'before\n[input requested: "partial> "]\n[waiting for stdin]'
    assert output == expected, repr(output)

    wait_for_evaluation_output(
        client,
        '[1] "without newline"\n',
        "partial stdin completion",
        stdin="\n",
    )

    # fmt: r
    r = code(r"""
        readline("next> ")
        """)
    wait_for_evaluation_output(
        client,
        '[input requested: "next> "]\n[1] "next"\n',
        "same-call stdin completion",
        r=r,
        stdin="next\n",
    )
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
