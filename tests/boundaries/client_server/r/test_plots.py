#!/usr/bin/env -S uv run --script

import sys
import subprocess
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_tool_text,
    release_worker_callback_gate,
    wait_for_idle_output,
)
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment, reference_plots
from support.records import Transcript
from support.requirements import R_EVENT_LOOP, requires
from support.suites import run_this_suite


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
def test_routes_input_to_idle_later_callback(
    binary: Path,
    execution: Execution,
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
          collected_answer <<- readline("later> ")
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "collected input callback")
    wait_for_idle_output(
        client,
        '[input requested: "later> "]\n[waiting for stdin]',
        "idle callback input request",
    )
    poll_start = len(client.transcript)
    client.send(stdin="yes\n")
    deadline = time.monotonic() + 3
    while last_tool_text(client) != "\n[idle]":
        assert last_tool_text(client) == "\n[waiting for stdin]"
        if time.monotonic() >= deadline:
            raise AssertionError("idle callback did not receive submitted stdin")
        time.sleep(0.01)
        client.send()
    polls = client.transcript[poll_start:]
    final_poll = polls[-1]
    final_poll["send"] = polls[0]["send"]
    client.transcript[poll_start:] = [final_poll]
    client.send(r="collected_answer")
    assert last_tool_text(client) == '[1] "yes"\n'
    return client.finish()


@executions(RUNTIME)
def test_uses_native_width_default(binary: Path, execution: Execution) -> Transcript:
    environment, rscript = r_test_environment()
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            cat("width: ", getOption("width"), "\n", sep = "")
            1:45
            """)
        client.send(r=r)
        output = last_tool_text(client)
        reference = subprocess.check_output(
            [rscript, "--vanilla", "-"], input=r, env=environment, text=True
        )
        assert output == reference, repr(output)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_returns_cell_scoped_plots(binary: Path, execution: Execution) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        options(
          console.plot.width_in = 4,
          console.plot.height_in = 3,
          console.plot.dpi = 100
        )
        cat("before plots\n")
        local({
          plot(1:3)
          cat("after first plot\n")
          lines(3:1, col = "red")
          plot(3:1)
          cat("after second plot\n")
        })
        """)
    expected_plots = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=2,
    )
    client.send(r=r)
    assert_result_content(
        client,
        [
            "before plots\nafter first plot\n",
            expected_plots[0],
            "after second plot\n",
            expected_plots[1],
        ],
    )

    client.send(r="lines(1:3)")
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    assert len(result["content"]) == 1, result
    output = result["content"][0]["text"]
    assert "plot.new has not been called yet" in output

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)
    return client.finish()


@executions(RUNTIME)
def test_preserves_large_plot_dimensions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code(r"""
                local({
                  # Observe the requested dimensions without allocating a large image.
                  invisible(suppressMessages(trace(
                    "png",
                    where = asNamespace("grDevices"),
                    tracer = quote({
                      stopifnot(identical(units, "in"))
                      stop(
                        sprintf("%g x %g inches at %g DPI", width, height, res),
                        call. = FALSE
                      )
                    }),
                    print = FALSE
                  )))
                  on.exit(suppressMessages(untrace(
                    "png",
                    where = asNamespace("grDevices")
                  )))
                  for (size in list(c(8, 6, 600), c(32, 1, 600))) {
                    options(
                      console.plot.width_in = size[[1]],
                      console.plot.height_in = size[[2]],
                      console.plot.dpi = size[[3]]
                    )
                    message <- tryCatch(plot(1:3), error = conditionMessage)
                    cat(message, "\n", sep = "")
                    stopifnot(identical(
                      message,
                      sprintf("%g x %g inches at %g DPI", size[[1]], size[[2]], size[[3]])
                    ))
                  }
                })
                """),
        )
        assert last_tool_text(client) == (
            "8 x 6 inches at 600 DPI\n32 x 1 inches at 600 DPI\n"
        ), last_tool_text(client)
        return client.finish()


@executions(RUNTIME)
def test_emits_managed_plots_when_pages_finalize(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        options(
          console.plot.width_in = 4,
          console.plot.height_in = 3,
          console.plot.dpi = 100
        )
        local({
          plot(1:3)
          plot(3:1)
          cat("after first page finalized\n")
        })
        """)
    expected_plots = reference_plots(
        rscript,
        environment,
        r,
        width=4,
        height=3,
        dpi=100,
        pages=2,
    )
    client.send(r=r)
    assert_result_content(
        client,
        [
            expected_plots[0],
            "after first page finalized\n",
            expected_plots[1],
        ],
    )
    return client.finish()


@executions(RUNTIME)
def test_returns_plots_after_r_errors(binary: Path, execution: Execution) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        local({
          plot(1:3)
          stop("boom")
        })
        """)
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
        expected_error="boom",
    )
    client.send(r=r)
    result = client.transcript[-1]["result"]
    text_items = [item for item in result["content"] if item["type"] == "text"]
    assert len(text_items) == 1 and "boom" in text_items[0]["text"], result
    assert_result_content(client, [text_items[0]["text"], expected_plot[0]])

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_leaves_explicit_plot_devices_user_controlled(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        explicit_plot <- tempfile(fileext = ".png")
        grDevices::png(explicit_plot, width = 4, height = 3, units = "in", res = 100)
        explicit_device <- grDevices::dev.cur()
        plot(1:3)
        cat("explicit current: ", names(grDevices::dev.cur()), "\n", sep = "")
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    assert result["content"][0]["text"].startswith("explicit current: "), result
    result["content"][0]["text"] = "explicit current: <device>\n"
    assert result["content"] == [
        {"type": "text", "text": "explicit current: <device>\n"}
    ], result

    # fmt: r
    r = code(r"""
        cat(
          "explicit still current: ",
          identical(grDevices::dev.cur(), explicit_device),
          "\n",
          sep = ""
        )
        invisible(grDevices::dev.off(which = explicit_device))
        cat("explicit complete: ", file.exists(explicit_plot), "\n", sep = "")
        unlink(explicit_plot)
        """)
    client.send(r=r)
    result = client.transcript[-1]["result"]
    assert result == {
        "content": [
            {
                "type": "text",
                "text": "explicit still current: TRUE\nexplicit complete: TRUE\n",
            }
        ],
        "isError": False,
    }, result

    r = "plot(3:1)"
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(client, expected_plot)

    # fmt: r
    r = code(r"""
        first_explicit_plot <- tempfile(fileext = ".png")
        second_explicit_plot <- tempfile(fileext = ".png")
        grDevices::png(first_explicit_plot)
        plot(1:3)
        grDevices::png(second_explicit_plot)
        plot(3:1)
        grDevices::graphics.off()
        cat(
          "all explicit complete: ",
          all(file.exists(first_explicit_plot, second_explicit_plot)),
          "\n",
          sep = ""
        )
        unlink(c(first_explicit_plot, second_explicit_plot))
        plot(2:4)
        """)
    expected_plot = reference_plots(
        rscript,
        environment,
        r,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    client.send(r=r)
    assert_result_content(
        client,
        ["all explicit complete: TRUE\n", expected_plot[0]],
    )
    return client.finish()


@executions(RUNTIME)
def test_preserves_reused_device_with_distinct_filename(
    binary: Path, execution: Execution
) -> Transcript:
    return reused_managed_device(binary, execution, same_filename=False)


@executions(RUNTIME)
def test_preserves_reused_device_with_managed_filename(
    binary: Path, execution: Execution
) -> Transcript:
    return reused_managed_device(binary, execution, same_filename=True)


def reused_managed_device(
    binary: Path, execution: Execution, *, same_filename: bool
) -> Transcript:
    environment, rscript = r_test_environment()
    managed_plot = "plot(1:3)"
    expected_plot = reference_plots(
        rscript,
        environment,
        managed_plot,
        width=800 / 96,
        height=600 / 96,
        dpi=96,
        pages=1,
    )
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        filename = (
            "managed_filename"
            if same_filename
            else 'tempfile("user-plot-", fileext = "-%06d.png")'
        )
        # fmt: r
        r = code(r"""
            plot(1:3)
            managed_device <- grDevices::dev.cur()
            managed_filename <- attr(.Devices[[managed_device]], "filepath")
            stopifnot(is.character(managed_filename), length(managed_filename) == 1L)
            invisible(grDevices::dev.off())
            explicit_filename <- USER_FILENAME
            grDevices::png(explicit_filename)
            explicit_device <- grDevices::dev.cur()
            stopifnot(identical(explicit_device, managed_device))
            plot(3:1)
            cat("user device reused the managed slot\n")
            """)
        client.send(r=r.replace("USER_FILENAME", filename))
        assert_result_content(
            client,
            [expected_plot[0], "user device reused the managed slot\n"],
        )
        client.send(
            # fmt: r
            r=code(r"""
                stopifnot(identical(grDevices::dev.cur(), explicit_device))
                invisible(grDevices::dev.new())
                next_managed_filename <- attr(.Devices[[grDevices::dev.cur()]], "filepath")
                stopifnot(
                  !identical(next_managed_filename, managed_filename),
                  !identical(next_managed_filename, explicit_filename)
                )
                plot(1:3)
                invisible(grDevices::dev.set(explicit_device))
                cat("managed plot used a distinct filename while the user device stayed open\n")
                """),
        )
        assert_result_content(
            client,
            [
                "managed plot used a distinct filename while the user device stayed open\n",
                expected_plot[0],
            ],
        )
        client.send(
            # fmt: r
            r=code(r"""
                stopifnot(identical(grDevices::dev.cur(), explicit_device))
                lines(1:3, col = "red")
                invisible(grDevices::dev.off(which = explicit_device))
                explicit_file <- sprintf(explicit_filename, 1L)
                stopifnot(file.exists(explicit_file), file.info(explicit_file)$size > 0)
                unlink(explicit_file)
                cat("user device remained open and completed explicitly\n")
                """),
        )
        assert_result_content(
            client, ["user device remained open and completed explicitly\n"]
        )
        client.send(
            # fmt: r
            r=code(r"""
                plot(1:3)
                invisible(grDevices::dev.off())
                plot(1:3)
                """),
        )
        assert_result_content(client, [expected_plot[0], expected_plot[0]])
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
