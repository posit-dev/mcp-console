#!/usr/bin/env -S uv run --script

import base64
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.server_relay._harness import (
    CHECKPOINT_NAME,
    DONE_NAME,
    RELEASE_NAME,
    ServerRelayClient,
)
from support.assertions import tool_text as _tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.requirements import POSIX, requires
from support.records import Transcript
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_forwards_raw_stdout_and_stderr(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "raw_output", execution=execution)
    assert _tool_text(client.send(r="42")) == (
        """stdout text 👩🏽‍💻
stderr text
�stdout bytes
�stderr bytes
"""
    )
    transcript = client.finish_active()

    output = [
        entry["relay"]
        for entry in transcript
        if entry.keys() == {"relay"}
        and entry["relay"].get("kind")
        in {"stdout", "stderr", "stdout_bytes", "stderr_bytes"}
    ]
    assert output[:2] == [
        {"kind": "stdout", "data": "stdout text 👩🏽‍💻\n"},
        {"kind": "stderr", "data": "stderr text\n"},
    ]
    assert [base64.b64decode(event["data"], validate=True) for event in output[2:]] == [
        b"\xffstdout bytes\n",
        b"\xfestderr bytes\n",
    ]
    return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_finishes_partial_utf8_with_its_cell(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "partial_utf8_completion", execution=execution)
    for expected in ("�", "��", "�", "��"):
        output = _tool_text(client.send(r="42"))
        assert output == expected, repr(output)
        assert _tool_text(client.send()) == "\n[idle]"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_compacts_split_terminal_redraws(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "split_terminal_redraws", execution=execution)
    assert _tool_text(client.send(r="42")) == "ordinary stdout\r\nol\nnew\nnew\n"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_compacts_stdout_and_stderr_independently(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(
        binary, "independent_stream_redraws", execution=execution
    )
    assert _tool_text(client.send(r="42")) == "stdout final\nstderr final\n"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interleaved_stream_ends_prior_redraw_run(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "interleaved_stream_redraws", execution=execution
    )
    assert _tool_text(client.send(r="42")) == ("stderr oldstdout final\nstderr final\n")
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_malformed_byte_completes_pending_redraw(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "raw_malformed_redraw", execution=execution)
    assert _tool_text(client.send(r="42")) == "�\n"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_empty_raw_close_does_not_split_console_redraw(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(
        binary, "empty_raw_close_between_redraws", execution=execution
    )
    assert _tool_text(client.send(r="42")) == "new\n"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_stdout_close_preserves_pending_stderr_utf8(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(
        binary, "stdout_close_between_utf8_fragments", execution=execution
    )
    output = _tool_text(client.send(r="42"))
    assert output == "€\n", repr(output)
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_stderr_close_preserves_pending_stdout_utf8(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(
        binary, "stderr_close_between_utf8_fragments", execution=execution
    )
    output = _tool_text(client.send(r="42"))
    assert output == "€\n", repr(output)
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_forwards_stdin(binary: Path, execution: Execution) -> Transcript:
    client = ServerRelayClient(binary, "stdin", execution=execution)
    assert _tool_text(client.send(r="42", stdin="answer\n")) == "[done]"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_empty_stdin_sends_no_relay_command(
    binary: Path, execution: Execution
) -> Transcript:
    client = ServerRelayClient(binary, "evaluate", execution=execution)
    assert _tool_text(client.send(r="42", stdin="")) == "[done]"
    return client.finish_active()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_orders_cross_source_output_by_serialized_observation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = ServerRelayClient(
        binary, "serialized_cross_source_order", execution=execution
    )
    evaluation = client.client.start_send(r="42")
    checkpoint = client._wait_for(CHECKPOINT_NAME)
    try:
        client.client.receive(evaluation)
        assert _tool_text(evaluation["result"]) == (
            "stdout before completion\n"
            "stderr before completion\n"
            "stdout after completion\n"
            "stderr after completion\n"
            "idle callback after completion\n"
        )
    finally:
        checkpoint.with_name(RELEASE_NAME).touch()
    client._wait_for(DONE_NAME)
    idle_output = _tool_text(client.send())
    assert idle_output == "stdout after grace\n\n[idle]", repr(idle_output)
    transcript = client.finish_active()

    completed = next(
        index
        for index, entry in enumerate(transcript)
        if entry == {"relay": {"kind": "completed"}}
    )
    first_callback = next(
        index
        for index, entry in enumerate(transcript)
        if entry.keys() == {"relay"} and entry["relay"].get("kind") == "resolve_r"
    )
    assert all(
        entry.keys() == {"relay"} for entry in transcript[completed:first_callback]
    )
    r_failures = [
        entry["server"]
        for entry in transcript
        if entry.keys() == {"server"}
        and entry["server"].get("kind") == "r_resolution_failed"
    ]
    assert r_failures == [
        {
            "kind": "r_resolution_failed",
            "failure": "host",
            "message": (
                "automatic R package name `github::owner/repo` is not accepted: "
                "names must start with an ASCII letter, end with an ASCII letter "
                "or digit, and contain only ASCII letters, digits, and dots"
            ),
        }
    ]
    return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
