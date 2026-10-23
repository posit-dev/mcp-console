"""Python cell exits follow the ordinary REPL and worker replacement contracts."""

import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

from boundaries.client_server.server.test_no_r import no_r_client
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, requires


@contextmanager
def python_client(binary: Path, execution: Execution, with_r: bool):
    if with_r:
        environment, _ = r_test_environment()
        with McpClient(binary, execution.serve(), environment) as client:
            yield client
    else:
        with no_r_client(binary, execution) as client:
            yield client


def exit_transcript(
    binary: Path, execution: Execution, with_r: bool, sources: tuple[str, ...]
) -> Transcript:
    with python_client(binary, execution, with_r) as client:
        client.initialize_and_list_tools()
        result = client.send(
            python="import threading\nassert threading.current_thread() is threading.main_thread()"
        )
        assert result["isError"] is False, result
        assert last_result_text(client) == "[done]", result
        for source in sources:
            if with_r:
                result = client.send(r="r_marker <- 42L")
                assert result["isError"] is False, result
                assert last_result_text(client) == "[done]", result
            source = "answer = 42\n" + source
            reference = subprocess.run(
                [sys.executable, "-I", "-c", source],
                capture_output=True,
                text=True,
                check=False,
            )
            assert reference.stdout == "", reference
            result = client.send(python=source)
            assert result["isError"] is True, result
            assert last_result_text(client) == (
                reference.stderr
                + "[worker sideband read failed: worker sideband closed]\n"
                + f"[worker exited with status {reference.returncode}]\n"
                + "[worker stopped: in-memory state lost]\n"
                + "[starting new worker]\n"
                + "[idle]"
            ), result
            client.send(python='("answer" in globals(), 1 + 1)')
            assert last_result_text(client) == "(False, 2)\n"
            if with_r:
                client.send(r='stopifnot(!exists("r_marker", inherits = FALSE)); 1 + 1')
                assert last_result_text(client) == "[1] 2\n"
        return client.finish()


EXIT_SOURCES = (
    "import sys\nsys.exit()",
    "import sys\nsys.exit(None)",
    "import sys\nsys.exit(0)",
    'import sys\nsys.exit("shutdown message")',
    "import sys\nsys.exit(1.5)",
    "raise SystemExit(33)",
    "raise SystemExit",
    'import argparse\nargparse.ArgumentParser().exit(33, "package shutdown\\n")',
)


@executions(DIRECT, SANDBOXED)
def test_system_exit_replaces_worker(binary: Path, execution: Execution) -> Transcript:
    return exit_transcript(binary, execution, False, ("import sys\nsys.exit(33)",))


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_system_exit_replaces_worker(
    binary: Path, execution: Execution
) -> Transcript:
    return exit_transcript(binary, execution, True, ("import sys\nsys.exit(33)",))


@executions(DIRECT, SANDBOXED)
def test_exit_codes_and_messages_match_python(
    binary: Path, execution: Execution
) -> Transcript:
    return exit_transcript(binary, execution, False, EXIT_SOURCES)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_exit_codes_and_messages_match_python(
    binary: Path, execution: Execution
) -> Transcript:
    return exit_transcript(binary, execution, True, EXIT_SOURCES)


def live_state_transcript(
    binary: Path, execution: Execution, with_r: bool, source: str
) -> Transcript:
    with python_client(binary, execution, with_r) as client:
        client.initialize_and_list_tools()
        if with_r:
            client.send(r="r_marker <- 42L")
        client.send(python="answer = 42\n" + source)
        assert client.transcript[-1]["result"]["isError"] is False
        assert last_result_text(client) == "[done]"
        client.send(python="answer")
        assert last_result_text(client) == "42\n"
        if with_r:
            client.send(r="r_marker")
            assert last_result_text(client) == "[1] 42\n"
        return client.finish()


# fmt: python
CAUGHT_EXIT = code("""
    import sys

    try:
        sys.exit(33)
    except SystemExit as error:
        assert error.code == 33
    try:
        raise SystemExit("caught message")
    except SystemExit as error:
        assert error.code == "caught message"
    """)

# fmt: python
BACKGROUND_EXIT = code("""
    import sys
    import threading


    def explicit_exit():
        raise SystemExit(33)


    for terminate in (lambda: sys.exit(33), explicit_exit):
        thread = threading.Thread(target=terminate)
        thread.start()
        thread.join()
        assert not thread.is_alive()
    """)


@executions(DIRECT, SANDBOXED)
def test_caught_exit_preserves_state(binary: Path, execution: Execution) -> Transcript:
    return live_state_transcript(binary, execution, False, CAUGHT_EXIT)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_caught_exit_preserves_state(
    binary: Path, execution: Execution
) -> Transcript:
    return live_state_transcript(binary, execution, True, CAUGHT_EXIT)


@executions(DIRECT, SANDBOXED)
def test_background_exit_preserves_state(
    binary: Path, execution: Execution
) -> Transcript:
    return live_state_transcript(binary, execution, False, BACKGROUND_EXIT)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_background_exit_preserves_state(
    binary: Path, execution: Execution
) -> Transcript:
    return live_state_transcript(binary, execution, True, BACKGROUND_EXIT)


def cleanup_transcript(binary: Path, execution: Execution, with_r: bool) -> Transcript:
    with python_client(binary, execution, with_r) as client:
        client.initialize_and_list_tools()
        if with_r:
            client.send(r="r_marker <- 42L")
        # fmt: python
        source = code("""
            answer = 42
            import matplotlib.pyplot as plt

            original_close = plt.close


            def exit_during_cleanup(*args, **kwargs):
                raise SystemExit("plot cleanup exit")


            plt.close = exit_during_cleanup
            """)
        result = client.send(python=source)
        assert result["isError"] is False, result
        assert last_result_text(client).endswith("SystemExit: plot cleanup exit\n"), (
            result
        )
        client.send(python="plt.close = original_close\nanswer")
        assert last_result_text(client) == "42\n"
        if with_r:
            client.send(r="r_marker")
            assert last_result_text(client) == "[1] 42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_handled_plot_cleanup_exit_preserves_state(
    binary: Path, execution: Execution
) -> Transcript:
    return cleanup_transcript(binary, execution, False)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_r_handled_plot_cleanup_exit_preserves_state(
    binary: Path, execution: Execution
) -> Transcript:
    return cleanup_transcript(binary, execution, True)


@executions(DIRECT, SANDBOXED)
def test_reports_process_exit_status(binary: Path, execution: Execution) -> Transcript:
    with no_r_client(binary, execution) as client:
        client.initialize_and_list_tools()
        client.send(python="import os\nanswer = 42\nos._exit(33)")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert last_result_text(client) == (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 33]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        client.send(python='"answer" in globals()')
        assert last_result_text(client) == "False\n"
        client.send(python="1 + 1")
        assert last_result_text(client) == "2\n"
        return client.finish()
