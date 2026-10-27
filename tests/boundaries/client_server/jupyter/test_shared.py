#!/usr/bin/env -S uv run --script

import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.checkpoints import wait_for_path
from support.jupyter import jupyter_client, normalize_jupyter_paths
from support.normalization import code, normalize_recording
from support.progress import without_elapsed
from support.processes import capture_process_identity, kill_processes, live_processes
from support.records import Transcript
from support.requirements import (
    ARK,
    JUPYTER,
    JUPYTER_RETICULATE,
    POSIX,
    PROCESS_EVENTS,
    R,
    requires,
)
from support.suites import run_this_suite


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE)
def test_shared_namespaces_callbacks_and_rich_output(binary: Path) -> Transcript:
    from PIL import Image

    with jupyter_client(
        binary, "ark", reticulate=True, reticulate_ipykernel=True, transport="ipc"
    ) as (client, _):
        client.expect(
            "R ready\n",
            # fmt: r
            r=code("""
                shared <- 40L
                r_pid <- Sys.getpid()
                call_python <- function(f, x) f(x)
                reticulate::py_run_string("before_python_cell = 2")
                cat("R ready\\n")
                """),
        )
        client.expect(
            "same process: True\n42\n",
            # fmt: python
            python=code("""
                import os

                print("same process:", os.getpid() == r.r_pid)
                from_python = r.shared + before_python_cell
                from_python
                """),
        )
        client.expect("42\n", python="r.call_python(lambda value: value + 1, 41)")
        client.expect(
            "R-driven Python\n",
            # fmt: r
            r=code("""
                stopifnot(reticulate::py$from_python == 42L)
                reticulate::py_run_string(
                  "after_python_cell = from_python + 1; print('R-driven Python', flush=True)"
                )
                """),
        )
        client.expect("[1] 42\n", r="print(reticulate::py$from_python)")
        client.expect("43\n", python="after_python_cell")
        encoded = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+aSuoAAAAASUVORK5CYII="
        )
        client.send(
            # fmt: python
            python=code(f"""
                import base64
                from IPython.display import Image, display

                display(Image(data=base64.b64decode("{encoded}")))
                """),
        )
        assert_result_content(
            client,
            [base64.b64decode(encoded)],
            image_reference="the submitted one-pixel PNG",
        )
        result = client.send(
            # fmt: r
            r=code("""
                par(mar = rep(0, 4), bg = "white")
                plot.new()
                rect(0.25, 0.25, 0.75, 0.75, col = "black", border = NA)
                """),
        )
        assert not result.get("isError"), result
        assert len(result["content"]) == 1, result
        block = result["content"][0]
        assert block.keys() == {"type", "data", "mimeType"}, block
        assert block["type"] == "image" and block["mimeType"] == "image/png", block
        with Image.open(
            io.BytesIO(base64.b64decode(block["data"], validate=True))
        ) as image:
            assert image.format == "PNG" and image.size == (800, 600)
            rgb = image.convert("RGB")
            assert rgb.getpixel((400, 300)) == (0, 0, 0), rgb.getpixel((400, 300))
            assert rgb.getpixel((0, 0)) == (255, 255, 255)
        block["data"] = "<PNG: 800 x 600, black center, white corner>"
        client.expect("42\n", code='cat(shared + 2L, "\\n", sep = "")')
        return client.finish()


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE)
def test_shared_async_state_errors_and_both_input_channels(binary: Path) -> Transcript:
    with jupyter_client(
        binary, "ark", reticulate=True, reticulate_ipykernel=True, transport="ipc"
    ) as (client, _):
        client.expect(
            "False\n",
            # fmt: python
            python=code("""
                import asyncio

                event = asyncio.Event()
                task = asyncio.create_task(event.wait())
                await asyncio.sleep(0)
                print(task.done())
                """),
        )
        client.expect("True\n", python="event.set()\nawait task")
        wait_for_evaluation_output(
            client,
            lambda output: "ValueError: shared kernel error" in output,
            "shared ipykernel error reply",
            expected_error=False,
            completion_timeout_seconds=30,
            python='raise ValueError("shared kernel error")',
        )
        client.expect("42\n", python="6 * 7")
        wait_for_evaluation_output(
            client,
            '[input requested: "Python name: "]\n[waiting for stdin]',
            "shared Python input request",
            completion_timeout_seconds=30,
            python='name = input("Python name: ")\nprint("hello", name)',
        )
        client.expect("hello Ada\n", stdin="Ada\n")
        wait_for_evaluation_output(
            client,
            '[input requested: "R name: "]\n[waiting for stdin]',
            "R input request after shared Python initialization",
            completion_timeout_seconds=30,
            r='r_name <- readline("R name: "); cat("hello ", r_name, "\\n", sep = "")',
        )
        client.expect("hello Grace\n", stdin="Grace\n")
        client.expect("'Ada Grace'\n", python="name + ' ' + r.r_name")
        client.expect("R still ready\n", r='cat("R still ready\\n")')
        return client.finish()


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE)
def test_shared_interrupts_and_restart(binary: Path) -> Transcript:
    with jupyter_client(
        binary, "ark", reticulate=True, reticulate_ipykernel=True, transport="ipc"
    ) as (client, workspace):
        client.expect("ready\n", python='answer = 42\nprint("ready")')
        client.send(
            # fmt: python
            python=code("""
                from pathlib import Path

                try:
                    Path("cpu-ready").touch()
                    while True:
                        pass
                except KeyboardInterrupt:
                    print("CPU interrupted")
                """),
            timeout_ms=0,
        )
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        wait_for_path(
            workspace / "cpu-ready", "shared Python reached its CPU loop", client=client
        )
        client.expect("CPU interrupted\n", control="interrupt")
        client.expect("42\n", python="answer")
        client.expect("[1] 42\n", r="reticulate::py$answer")
        client.send(
            # fmt: python
            python=code("""
                import asyncio

                try:
                    Path("async-ready").touch()
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    print("async cancelled")
                """),
            timeout_ms=0,
        )
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        wait_for_path(
            workspace / "async-ready",
            "shared Python reached its awaited gate",
            client=client,
        )
        client.expect("async cancelled\n", control="interrupt")
        client.expect("42\n", python="answer")
        client.expect("[1] 42\n", r="reticulate::py$answer")
        client.send(
            # fmt: python
            python=code("""
                Path("uncaught-cpu-ready").touch()
                while True:
                    pass
                """),
            timeout_ms=0,
        )
        wait_for_path(
            workspace / "uncaught-cpu-ready",
            "uncaught shared Python CPU loop",
            client=client,
        )
        wait_for_evaluation_output(
            client,
            lambda output: "KeyboardInterrupt" in output,
            "native shared Python KeyboardInterrupt",
            expected_error=False,
            completion_timeout_seconds=30,
            control="interrupt",
        )
        client.expect("42\n", python="answer")
        client.expect("[1] 42\n", r="reticulate::py$answer")
        client.send(
            # fmt: python
            python=code("""
                Path("uncaught-async-ready").touch()
                await asyncio.Event().wait()
                """),
            timeout_ms=0,
        )
        wait_for_path(
            workspace / "uncaught-async-ready",
            "uncaught shared Python awaited gate",
            client=client,
        )
        wait_for_evaluation_output(
            client,
            lambda output: "CancelledError" in output,
            "native shared Python CancelledError",
            expected_error=False,
            completion_timeout_seconds=30,
            control="interrupt",
        )
        client.expect("42\n", python="answer")
        client.expect("[1] 42\n", r="reticulate::py$answer")
        result = client.send(control="restart")
        assert not result.get("isError"), result
        assert (
            last_tool_text(client)
            == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.expect("False\n", python='"answer" in globals()')
        client.expect(
            "[1] FALSE\n", r='reticulate::py_has_attr(reticulate::py, "answer")'
        )
        client.expect("42\n", python="6 * 7")
        return normalize_jupyter_paths(normalize_recording(client.finish(), workspace))


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE, POSIX, PROCESS_EVENTS)
def test_shared_kernel_death_retires_both_endpoints(binary: Path) -> Transcript:
    identities = []
    try:
        with jupyter_client(
            binary, "ark", reticulate=True, reticulate_ipykernel=True, transport="ipc"
        ) as (client, workspace):
            client.send(python="import os\nprint(os.getpid())")
            original = capture_process_identity(int(last_tool_text(client).strip()))
            identities.append(original)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<shared kernel PID>\n"
            )
            result = client.send(python="os._exit(7)")
            assert result["isError"] is True, result
            assert (
                "Jupyter kernel exited with status 7" in result["content"][0]["text"]
            ), result
            assert live_processes((original,)) == [], "shared kernel was not reaped"
            result = client.send(control="restart")
            assert not result.get("isError"), result
            assert (
                last_tool_text(client)
                == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            client.expect("[1] 42\n", r="6L * 7L")
            client.expect("42\n", python="6 * 7")
            client.send(python="import os\nprint(os.getpid())")
            replacement = capture_process_identity(int(last_tool_text(client).strip()))
            identities.append(replacement)
            assert replacement != original
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<replacement shared kernel PID>\n"
            )
            transcript = normalize_jupyter_paths(
                normalize_recording(client.finish(), workspace)
            )
            assert live_processes(identities) == [], (
                "server shutdown leaked the shared kernel"
            )
            return transcript
    finally:
        kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
