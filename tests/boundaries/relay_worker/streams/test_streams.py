#!/usr/bin/env -S uv run --script

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.relay_worker._harness import RelayWorkerClient
from support.assertions import tool_text as _tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(R)
def test_routes_python_output(binary: Path, execution: Execution) -> Transcript:
    client = RelayWorkerClient(binary, execution=execution)
    # fmt: r
    r = code(r"""
        suppressWarnings(
          invisible(reticulate::py_run_string("initialized_from_r = True"))
        )
        """)
    assert _tool_text(client.send(r=r)) == "[done]"

    # fmt: python
    python = code(r"""
        import sys

        assert initialized_from_r


        def reject_r_console(frame, event, function):
            if event == "c_call" and getattr(function, "__module__", None) == "rpycall":
                raise AssertionError("Python console output entered R")


        sys.setprofile(reject_r_console)
        print("Python stdout")
        assert sys.stdout.write("") == 0
        assert sys.stderr.write("Python stderr\n") == 14
        assert sys.stderr.write("") == 0
        assert sys.stdout.write("🐍\n") == 2
        assert sys.stderr.write("🐍\n") == 2
        sys.setprofile(None)
        raise ValueError("boom")
        """)
    output = _tool_text(client.send(python=python))
    assert output.startswith("""Python stdout
Python stderr
🐍
🐍
Traceback"""), output
    assert output.endswith("ValueError: boom\n"), output

    # fmt: python
    descendant = code(r"""
        import sys

        print("exec descendant stdout")
        sys.stderr.write("exec descendant stderr\n")
        """)
    # fmt: python
    python = code(rf"""
        import os
        import subprocess
        import sys

        buffer_stdout = sys.stdout.buffer.write(b"buffer stdout\n")
        sys.stdout.buffer.flush()
        buffer_stderr = sys.stderr.buffer.write(b"buffer stderr\n")
        sys.stderr.buffer.flush()
        direct_stdout = os.write(1, b"direct stdout\n")
        direct_stderr = os.write(2, b"direct stderr\n")
        descendant_source = {descendant!r}
        exec_descendant = subprocess.run(
            [sys.executable, "-c", descendant_source],
            check=True,
        )
        """)
    expected = [
        "buffer stdout",
        "buffer stderr",
        "direct stdout",
        "direct stderr",
        "exec descendant stdout",
        "exec descendant stderr",
    ]
    output = _tool_text(client.send(python=python))
    output = client._collect_output(output, sum(len(line) + 1 for line in expected))
    assert sorted(output.splitlines()) == sorted(expected), repr(output)
    transcript = client.finish()
    assert all(
        event["worker"]["data"]
        for event in transcript
        if event.get("worker", {}).get("kind")
        in {"console_output", "console_diagnostic"}
    )
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_routes_background_python_text_to_raw_streams(
    binary: Path, execution: Execution
) -> Transcript:
    client = RelayWorkerClient(binary, execution=execution)
    # fmt: python
    python = code(r"""
        import logging
        import sys
        import threading

        saved_stdout, saved_stderr = sys.stdout, sys.stderr
        logger = logging.Logger("thread-output")
        logger.addHandler(logging.StreamHandler(saved_stderr))


        def background():
            saved_stdout.write("thread stdout\n")
            saved_stdout.flush()
            saved_stderr.write("thread stderr\n")
            saved_stderr.flush()
            logger.warning("thread log")


        thread = threading.Thread(target=background)
        thread.start()
        thread.join()
        """)
    output = _tool_text(client.send(python=python))
    expected = """thread stdout
thread stderr
thread log
"""
    output = client._collect_output(output, len(expected))
    assert sorted(output.splitlines()) == sorted(expected.splitlines()), output
    transcript = client.finish()
    assert "".join(event.get("stdout", "") for event in transcript) == "thread stdout\n"
    assert (
        "".join(event.get("stderr", "") for event in transcript)
        == "thread stderr\nthread log\n"
    )
    assert all(
        event["worker"]["kind"]
        in {"ready", "r_initialization", "cell_started", "completed"}
        for event in transcript
        if "worker" in event
    )
    assert [
        event["worker"]["complete"]
        for event in transcript
        if event.get("worker", {}).get("kind") == "r_initialization"
    ] == [False, True]
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(R)
def test_routes_r_console_channels(binary: Path, execution: Execution) -> Transcript:
    client = RelayWorkerClient(binary, execution=execution)
    # fmt: r
    r = code(r"""
        cat("R output\n")
        message("R diagnostic")
        utils::file.edit(
          c("/dev/null", "/dev/null"),
          editor = Sys.which("true")
        )
        """)
    assert _tool_text(client.send(r=r)) == (
        """R output
R diagnostic
WARNING: Only editing the first in the list of files
"""
    )
    return client.finish()


def _python_fork_client(binary: Path, execution: Execution) -> RelayWorkerClient:
    client = RelayWorkerClient(binary, execution=execution)
    # fmt: r
    r = code(r"""
        python <- Sys.which("python3")
        stopifnot(nzchar(python))
        reticulate::use_python(python, required = TRUE)
        suppressWarnings(invisible(reticulate::py_run_string("fork_ready = True")))
        """)
    assert _tool_text(client.send(r=r)) == "[done]"

    # fmt: python
    python = code(r"""
        import logging
        import os
        import select
        import signal
        import sys
        import traceback
        import warnings

        assert fork_ready
        saved_stdout, saved_stderr = sys.stdout, sys.stderr
        parent_tty = saved_stdout.isatty(), saved_stderr.isatty()
        worker_pid = os.getpid()
        logger = logging.Logger("fork-output")
        logger.propagate = False
        handler = logging.StreamHandler()
        logger.addHandler(handler)


        def reject_runtime_callback(frame, event, function):
            if (
                os.getpid() != worker_pid
                and event == "c_call"
                and getattr(function, "__module__", None)
                in {"rpycall", "_mcp_console_services"}
            ):
                raise AssertionError("fork child called back into Console or R")


        def run_child(action):
            read_fd, write_fd = os.pipe()
            # Only this known CPython diagnostic is filtered, only around fork.
            # The automatic-resolution lifecycle test records it in full.
            previous_profile = sys.getprofile()
            sys.setprofile(reject_runtime_callback)
            try:
                with warnings.catch_warnings():
                    warnings.filterwarnings(
                        "ignore",
                        message=r"This process \(pid=\d+\) is multi-threaded, use of fork\(\) may lead to deadlocks in the child\.$",
                        category=DeprecationWarning,
                    )
                    child = os.fork()
            finally:
                if os.getpid() == worker_pid:
                    sys.setprofile(previous_profile)
            if child == 0:
                os.close(read_fd)
                try:
                    action()
                except BaseException:
                    os.write(2, traceback.format_exc().encode())
                    os._exit(1)
                # Keep write_fd open until exit so EOF bounds the parent's wait.
                os._exit(0)

            os.close(write_fd)
            exited = False
            try:
                readable, _, _ = select.select([read_fd], [], [], 10)
                assert readable, "fork child did not exit"
                assert os.read(read_fd, 1) == b""
                exited = True
            finally:
                os.close(read_fd)
                if not exited:
                    os.kill(child, signal.SIGKILL)
                _, status = os.waitpid(child, 0)
            assert os.waitstatus_to_exitcode(status) == 0, status
        """)
    assert _tool_text(client.send(python=python)) == "[done]"
    return client


def _finish_python_fork_output(
    client: RelayWorkerClient, stdout: str, stderr: str
) -> Transcript:
    # A later evaluation must retain the original streams and logging handler.
    # fmt: python
    python = code(r"""
        assert sys.stdout is saved_stdout
        assert sys.stderr is saved_stderr
        assert handler.stream is saved_stderr
        assert (saved_stdout.isatty(), saved_stderr.isatty()) == parent_tty
        parent_stdout = sys.stdout.write("parent stdout\n")
        parent_stderr = sys.stderr.write("parent stderr\n")
        logger.warning("parent log")
        """)
    assert _tool_text(client.send(python=python)) == (
        """parent stdout
parent stderr
parent log
"""
    )
    assert _tool_text(client.send(r="6 * 7")) == "[1] 42\n"
    transcript = client.finish()
    # These are the actual worker pipes, not sideband console events.
    assert "".join(event.get("stdout", "") for event in transcript) == stdout
    assert "".join(event.get("stderr", "") for event in transcript) == stderr
    worker = [event["worker"] for event in transcript if "worker" in event]
    assert all(
        event["kind"]
        in {
            "ready",
            "cell_started",
            "r_initialization",
            "completed",
            "console_output",
            "console_diagnostic",
        }
        for event in worker
    ), worker
    assert sum(event["kind"] == "cell_started" for event in worker) == sum(
        event["kind"] == "completed" for event in worker
    )
    assert [
        event["complete"] for event in worker if event["kind"] == "r_initialization"
    ] == [False, True]
    for kind, expected in (
        ("console_output", "parent stdout\n[1] 42\n"),
        ("console_diagnostic", "parent stderr\nparent log\n"),
    ):
        actual = "".join(event["data"] for event in worker if event["kind"] == kind)
        assert actual == expected, (kind, actual)
    return transcript


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("python3"))
@requires(R)
def test_preserves_python_output_from_fork_children(
    binary: Path, execution: Execution
) -> Transcript:
    client = _python_fork_client(binary, execution)
    # fmt: python
    python = code(r"""
        def child_output():
            print("fork child stdout", flush=True)
            sys.stderr.write("fork child stderr\n")
            sys.stderr.flush()


        run_child(child_output)
        """)
    expected = "fork child stdout\nfork child stderr\n"
    output = _tool_text(client.send(python=python))
    assert "Traceback" not in output, output
    output = client._collect_output(output, len(expected))
    assert sorted(output.splitlines()) == sorted(expected.splitlines()), repr(output)
    return _finish_python_fork_output(
        client, "fork child stdout\n", "fork child stderr\n"
    )


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("python3"))
@requires(R)
def test_preserves_cached_python_streams_from_fork_children(
    binary: Path, execution: Execution
) -> Transcript:
    client = _python_fork_client(binary, execution)
    # fmt: python
    python = code(r"""
        def child_output():
            assert saved_stdout.isatty() == sys.stdout.isatty() == os.isatty(1)
            assert saved_stderr.isatty() == sys.stderr.isatty() == os.isatty(2)
            saved_stdout.write("cached child stdout\n")
            saved_stdout.flush()
            saved_stderr.write("cached child stderr\n")
            saved_stderr.flush()


        run_child(child_output)
        """)
    expected = "cached child stdout\ncached child stderr\n"
    output = _tool_text(client.send(python=python))
    assert "Traceback" not in output, output
    output = client._collect_output(output, len(expected))
    assert sorted(output.splitlines()) == sorted(expected.splitlines()), repr(output)
    return _finish_python_fork_output(
        client, "cached child stdout\n", "cached child stderr\n"
    )


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("python3"))
@requires(R)
def test_preserves_cached_python_logging_from_fork_children(
    binary: Path, execution: Execution
) -> Transcript:
    client = _python_fork_client(binary, execution)
    # fmt: python
    python = code(r"""
        run_child(lambda: logger.warning("cached child log"))
        """)
    expected = "cached child log\n"
    output = _tool_text(client.send(python=python))
    assert "Traceback" not in output, output
    output = client._collect_output(output, len(expected))
    assert output == expected, repr(output)
    return _finish_python_fork_output(client, "", expected)


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("python3"))
@requires(R)
def test_preserves_fork_stderr_after_stdout_is_closed(
    binary: Path, execution: Execution
) -> Transcript:
    client = _python_fork_client(binary, execution)
    # fmt: python
    python = code(r"""
        saved_stdout.buffer.close()


        def child_output():
            try:
                saved_stdout.write("closed child stdout\n")
            except ValueError:
                pass
            else:
                raise AssertionError("closed stdout accepted a write")
            saved_stderr.write("child stderr after stdout closed\n")
            saved_stderr.flush()
            logger.warning("child log after stdout closed")


        run_child(child_output)
        """)
    expected = "child stderr after stdout closed\nchild log after stdout closed\n"
    output = _tool_text(client.send(python=python))
    assert "Traceback" not in output, output
    output = client._collect_output(output, len(expected))
    assert output == expected, repr(output)
    return _finish_python_fork_output(client, "", expected)


@executions(DIRECT, SANDBOXED)
@requires(POSIX, command("python3"))
@requires(R)
def test_preserves_redirected_python_streams_from_fork_children(
    binary: Path, execution: Execution
) -> Transcript:
    client = _python_fork_client(binary, execution)
    # fmt: python
    python = code(r"""
        import contextlib
        import tempfile

        with (
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stdout_file,
            tempfile.TemporaryFile(mode="w+", encoding="utf-8") as stderr_file,
            contextlib.redirect_stdout(stdout_file),
            contextlib.redirect_stderr(stderr_file),
        ):
            redirected_handler = logging.StreamHandler()

            def child_output():
                assert sys.stdout is stdout_file
                assert sys.stderr is stderr_file
                print("redirected child stdout", flush=True)
                sys.stderr.write("redirected child stderr\n")
                sys.stderr.flush()
                redirected_handler.handle(
                    logging.makeLogRecord({"msg": "redirected child log"})
                )
                saved_stdout.write("cached stdout during redirection\n")
                saved_stdout.flush()
                logger.warning("cached log during redirection")

            run_child(child_output)
            assert sys.stdout is stdout_file
            assert sys.stderr is stderr_file
            assert redirected_handler.stream is stderr_file
            print("redirected parent stdout", flush=True)
            sys.stderr.write("redirected parent stderr\n")
            sys.stderr.flush()
            redirected_handler.handle(logging.makeLogRecord({"msg": "redirected parent log"}))
            stdout_file.seek(0)
            stderr_file.seek(0)
            assert stdout_file.read() == "redirected child stdout\nredirected parent stdout\n"
            assert stderr_file.read() == (
                "redirected child stderr\nredirected child log\n"
                "redirected parent stderr\nredirected parent log\n"
            )
        """)
    expected = "cached stdout during redirection\ncached log during redirection\n"
    output = _tool_text(client.send(python=python))
    assert "Traceback" not in output, output
    output = client._collect_output(output, len(expected))
    assert sorted(output.splitlines()) == sorted(expected.splitlines()), repr(output)
    return _finish_python_fork_output(
        client, "cached stdout during redirection\n", "cached log during redirection\n"
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_drains_standard_streams_while_evaluating(
    binary: Path, execution: Execution
) -> Transcript:
    client = RelayWorkerClient(binary, execution=execution)
    size = 4 * 1024 * 1024
    # fmt: python
    python = code(rf"""
        import os


        def write_all(file_descriptor, data):
            data = memoryview(data)
            while data:
                data = data[os.write(file_descriptor, data) :]


        write_all(1, b"x" * {size})
        write_all(2, b"y" * {size})
        """)
    result = client.send(python=python)
    output = _tool_text(result)
    assert not result["isError"]
    assert len(output.encode()) <= 8 * 1024
    assert "output omitted:" in output
    assert "raw log:" in output

    # The wire capture verifies every emitted byte independently of the preview.
    transcript = client.finish()
    assert transcript[-2] == {"stdout": "x" * size, "stderr": "y" * size}
    assert transcript[-1] == {"worker": {"kind": "completed"}}
    transcript[-2]["stdout"] = f"<{size} bytes>"
    transcript[-2]["stderr"] = f"<{size} bytes>"
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
