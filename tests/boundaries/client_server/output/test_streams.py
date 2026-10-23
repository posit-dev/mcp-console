#!/usr/bin/env -S uv run --script

import base64
import json
import os
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.previews import cell_output_path
from support.snapshots import execution_snapshots
from support.progress import without_elapsed, without_elapsed_result
from support.assertions import (
    LARGE_OUTPUT_SIZE,
    large_output,
    last_tool_text,
    remove_length_marker,
)
from support.previews import (
    OMISSION,
    TEXT_BUDGET,
    assert_preview,
    cell_text,
    compact_previews,
    normalize_pipe_counts,
    normalize_preview_paths,
    session_directory,
)
from support.client import McpClient, stop_client
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.checkpoints import (
    FifoCheckpoint,
    release_fixture_checkpoint,
    wait_for_checkpoint,
)
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, PROCESS_EVENTS, R, requires
from support.suites import run_this_suite

TEST_GATED_RESPONSE_SIZE = 128 * 1024
PNG_1X1 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42Y"
    "AAAAASUVORK5CYII="
)
AnsiFixture = dict[str, str | list[dict[str, str]]]

from boundaries.client_server._harness import (
    ResponseGateObserver,
    SocketGateMcpClient,
    ZodFixtureControl,
    wait_for_marker,
)


@contextmanager
def ansi_client(
    binary: Path, execution: Execution, inputs: dict[str, AnsiFixture]
) -> Iterator[McpClient]:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        # Fixture transport stays separate from the named command in send.r.
        input_path = Path(temporary) / "ansi-inputs.json"
        input_path.write_text(json.dumps(inputs), encoding="utf-8")
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "server_relay/scripted_relay.py"),
            ),
            {
                **os.environ,
                "TMPDIR": temporary,
                "MCP_CONSOLE_TEST_ANSI_INPUTS": str(input_path),
                "MCP_CONSOLE_TEST_RELAY_SCENARIO": "ansi_projection",
            },
        ) as client:
            yield client


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_projects_ansi_across_every_byte_split(
    binary: Path, execution: Execution
) -> Transcript:
    samples = (
        ("plain \x1b[1;31mred\x1b[0m €🙂\n", "plain red €🙂\n"),
        ("old\r\x1b[32mnew\x1b[0m\n", "new\n"),
        ("keep\r\x1b[0m\n", "keep\r\n"),
        ("erase\x1b[2K\n", "\n"),
        ("erase\r\x1b[2K\n", "\r\n"),
        ("erase\r\x1b[K\n", "\r\n"),
        ("old\r\x1b[Knew\n", "new\n"),
        ("old\r\x1b[0Knew\n", "new\n"),
        ("keep\x1b[K plus\n", "keep plus\n"),
        ("old\x1b[1Knew\n", "oldnew\n"),
        ("before\x1b[2A\x1b[2Jafter\n", "beforeafter\n"),
        ("charset\x1b(Btext\n", "charsettext\n"),
        ("€🙂\b\x1b[38:2::10:20:30m!\x1b[m\n", "€!\n"),
        ("before\x1b]0;title\x07after\n", "beforeafter\n"),
        ("\x1b]52;c;clipboard\x1b\\safe\n", "safe\n"),
        (
            "\x1b]8;;https://example.invalid\x1b\\label\x1b]8;;\x1b\\\n",
            "label\n",
        ),
        ("before\x1bPprivate\x07payload\x1b\\after\n", "beforeafter\n"),
        ("a\x1bXhidden\x1b\\b\x1b^hidden\x1b\\c\x1b_hidden\x1b\\d\n", "abcd\n"),
        ("\u009b31mC1\u009b0m\u009d0;title\u009csafe\n", "C1safe\n"),
        (
            "a\u0090hidden\u009cb\u0098hidden\u009cc\u009ehidden\u009cd\u009fhidden\u009ce\n",
            "abcde\n",
        ),
        ("a\x1b]hidden\n\r\x1b[2Kpayload\x1b\\b\n", "ab\n"),
        ("old\x1b[31\x1b[2Knew\n", "new\n"),
        ("a\x1b[31🙂b\n", "a🙂b\n"),
        ("old\x1b[31\rnew\n", "new\n"),
        ("a\x1b[31\nb\n", "a\nb\n"),
        ("a\x1b(🙂b\n", "a🙂b\n"),
        ("old\r\b\x1b[0mnew\n", "new\n"),
        ("old\x1b[" + "0;" * 80 + "2Ksafe\n", "oldsafe\n"),
        ("old\x1b" + "(" * 140 + "Bsafe\n", "oldsafe\n"),
    )
    inputs = {
        f"project_ansi_sample_{call_id:02}": {"sample": sample}
        for call_id, (sample, _) in enumerate(samples, 1)
    }
    with ansi_client(binary, execution, inputs) as client:
        client.initialize_and_list_tools()
        for call_id, (sample, projected) in enumerate(samples, 1):
            # One unsplit run, every two-chunk split, and byte-wise fragmentation.
            repetitions = len(sample.encode()) + 3
            operation = f"project_ansi_sample_{call_id:02}"
            client.transcript.append(
                {"fixture": {"operation": operation, **inputs[operation]}}
            )
            result = client.send(r=operation)
            assert result == {
                "content": [{"type": "text", "text": projected * repetitions}],
                "isError": False,
            }, (sample, result)
            assert len(last_tool_text(client).encode()) <= TEXT_BUDGET
            assert (
                cell_output_path(session_directory(client), call_id)
            ).read_bytes() == sample.encode() * repetitions
        compact_previews(client, *(projected for _, projected in samples))
        assert [
            entry["fixture"]["sample"]
            for entry in client.transcript
            if "fixture" in entry
        ] == [sample for sample, _ in samples]
        assert [
            entry["send"]["r"] for entry in client.transcript if "send" in entry
        ] == list(inputs)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_finishes_ansi_at_producer_image_and_cell_boundaries(
    binary: Path, execution: Execution
) -> Transcript:
    events = [
        {"kind": "console_output", "data": "out\x1b["},
        {"kind": "console_diagnostic", "data": "31mdiag\n"},
        {"kind": "stdout_bytes", "data": base64.b64encode(b"\x9b31mraw\n").decode()},
        {"kind": "stdout", "data": "raw\x1b]0;discard"},
        {"kind": "stderr", "data": "error\n"},
        {"kind": "stderr", "data": "stderr \x1b[3"},
        {"kind": "stdout_closed"},
        {"kind": "stderr", "data": "1mred\x1b[0m\n"},
        {"kind": "stderr_closed"},
        {"kind": "console_output", "data": "before image\x1b]unterminated"},
        {"kind": "image", "data": PNG_1X1, "mime_type": "image/png"},
        {"kind": "console_output", "data": "after \x1b[32mimage\x1b[m\n"},
    ]
    cell_samples = (
        ("visible\x1b[31", "visible"),
        ("mnext\n", "mnext\n"),
        ("visible\x1b]hidden", "visible"),
        ("next\n", "next\n"),
        ("visible\x1b[2K", "[done]"),
    )
    inputs = {
        "emit_ansi_boundary_events": {"events": events},
        **{
            f"emit_ansi_cell_{call_id:02}": {
                "events": [{"kind": "console_output", "data": sample}]
            }
            for call_id, (sample, _) in enumerate(cell_samples, 2)
        },
    }
    with ansi_client(binary, execution, inputs) as client:
        client.initialize_and_list_tools()
        operation = "emit_ansi_boundary_events"
        client.transcript.append(
            {"fixture": {"operation": operation, **inputs[operation]}}
        )
        result = client.send(r=operation)
        assert [block["type"] for block in result["content"]] == [
            "text",
            "image",
            "text",
        ]
        assert result["content"][0]["text"] == (
            "out31mdiag\n�31mraw\nrawerror\nstderr red\nbefore image"
        ), result
        assert result["content"][-1]["text"] == "after image\n", result
        assert (cell_output_path(session_directory(client), 1)).read_bytes() == (
            b"".join(
                base64.b64decode(event["data"])
                if event["kind"].endswith("_bytes")
                else event["data"].encode()
                for event in events
                if event["kind"] != "image" and "data" in event
            )
        )
        for call_id, (sample, expected) in enumerate(cell_samples, 2):
            operation = f"emit_ansi_cell_{call_id:02}"
            client.transcript.append(
                {"fixture": {"operation": operation, **inputs[operation]}}
            )
            result = client.send(r=operation)
            assert last_tool_text(client) == expected, result
            assert (
                cell_output_path(session_directory(client), call_id)
            ).read_bytes() == sample.encode()
        assert [
            entry["fixture"]["events"]
            for entry in client.transcript
            if "fixture" in entry
        ] == [
            events,
            *(
                [{"kind": "console_output", "data": sample}]
                for sample, _ in cell_samples
            ),
        ]
        assert [
            entry["send"]["r"] for entry in client.transcript if "send" in entry
        ] == list(inputs)
        return client.finish()


@execution_snapshots
@requires(POSIX, PROCESS_EVENTS)
@executions(DIRECT, SANDBOXED)
def test_separates_native_diagnostics_from_worker_stderr(
    binary: Path, execution: Execution
) -> Transcript:
    chunks = (
        ("native_stderr", b"native \x1b[3"),
        ("native_stderr", b"1m\xe2"),
        ("native_stderr", b"\x82\xacred\x1b[0m\n"),
        ("native_stderr", b"\x1b]0;hidden"),
        ("native_stderr", b"\x07native string ended\n"),
        ("native_stderr", b"native before \x1b]0;hidden"),
        ("stderr_bytes", b"worker diagnostic\n"),
        ("native_stderr", b"\x1b\\native after\n"),
        ("stderr_bytes", b"worker before \x1b]0;hidden"),
        ("native_stderr", b"native diagnostic\n"),
        ("stderr_bytes", b"\x1b\\worker after\n"),
    )
    inputs = {
        "emit_ansi_diagnostics": {
            "events": [
                {"kind": kind, "data": base64.b64encode(data).decode()}
                for kind, data in chunks
            ]
        }
    }
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        input_path = directory / "ansi-inputs.json"
        input_path.write_text(json.dumps(inputs), encoding="utf-8")
        roots = ("--writable-root", temporary) if execution == SANDBOXED else ()
        with (
            closing(FifoCheckpoint.create(directory / "diagnostic-release")) as release,
            McpClient(
                binary,
                execution.serve(
                    "--worker",
                    str(fixtures / "zod"),
                    "--relay",
                    str(fixtures / "server_relay/scripted_relay.py"),
                    *roots,
                ),
                {
                    **os.environ,
                    "TMPDIR": temporary,
                    "MCP_CONSOLE_TEST_ANSI_INPUTS": str(input_path),
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "ansi_diagnostics",
                },
            ) as client,
            Events() as events,
        ):
            try:
                client.initialize_and_list_tools()
                operation = "emit_ansi_diagnostics"
                client.transcript.append(
                    {"fixture": {"operation": operation, **inputs[operation]}}
                )
                request = client.start_send(
                    r=operation,
                    timeout_ms=600_000,
                )
                raw = b""
                sessions = (
                    Path(client.temporary_directory.name) / ".agents/console/sessions"
                )

                def recorded() -> Path | None:
                    for path in sessions.glob("*/capture/sessions/001/cell-001.txt"):
                        events.watch_file(path)
                        if path.read_bytes() == raw:
                            return path
                    return None

                # Native stderr and CellStarted use independent relay streams.
                # Establish the cell interval before releasing its first bytes.
                wait_for_checkpoint(
                    recorded,
                    "executing cell capture ready before native diagnostics",
                    root=sessions,
                    recursive=True,
                    client=client,
                    events=events,
                )
                for _, data in chunks:
                    release.release()
                    raw += data
                    # Recording occurs under the tape lock. Observing these bytes
                    # before releasing the next writer proves ingestion order,
                    # without cutting/resetting the unfinished ANSI sequence.
                    log = wait_for_checkpoint(
                        recorded,
                        "diagnostic chunk retained before producer switch",
                        root=sessions,
                        recursive=True,
                        client=client,
                        events=events,
                    )
                release.release()
                client.receive(request)
                assert log.read_bytes() == raw
                assert last_tool_text(client) == (
                    "native €red\nnative string ended\n"
                    "native before worker diagnostic\nnative after\n"
                    "worker before native diagnostic\nworker after\n"
                ), request
                assert [
                    entry["fixture"]["events"]
                    for entry in client.transcript
                    if "fixture" in entry
                ] == [
                    [
                        {"kind": kind, "data": base64.b64encode(data).decode()}
                        for kind, data in chunks
                    ]
                ]
                assert request["send"]["r"] == "emit_ansi_diagnostics"
                return client.finish()
            finally:
                for _ in range(len(chunks) + 1):
                    release.release()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_projects_builtin_ansi_and_preserves_raw_bytes(
    binary: Path, execution: Execution
) -> Transcript:
    raw = b"\x1b[31mstyled\x1b[0m\nold\r\x1b[2Kshort\n"
    # fmt: python
    python = code(r"""
        import sys

        _ = sys.stdout.write("\x1b[31mstyled\x1b[0m\nold\r\x1b[2Kshort\n")
        """)
    # fmt: r
    r = code(r"""
        cat("\033[31mstyled\033[0m\nold\r\033[2Kshort\n")
        """)
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        for call_id, arguments in enumerate(
            ({"python": python}, {"r": r}),
            1,
        ):
            result = client.send(**arguments)
            assert last_tool_text(client) == "styled\nshort\n", result
            assert (
                cell_output_path(session_directory(client), call_id)
            ).read_bytes() == raw
        client.expect(
            'before\n[input requested: "prompt> "]\n[waiting for stdin]',
            # fmt: python
            python=code(r"""
                _ = sys.stdout.write("before\x1b]unterminated")
                answer = input("prompt> ")
                print("after")
                """),
        )
        client.expect("after\n", stdin="answer\n")
        assert (
            cell_output_path(session_directory(client), 3)
        ).read_bytes() == b"before\x1b]unterminatedafter\n"
        return client.finish()


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_finishes_ansi_at_polls_but_preserves_split_utf8(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        roots = ("--writable-root", temporary) if execution == SANDBOXED else ()
        with (
            closing(FifoCheckpoint.create(directory / "partial-release")) as release,
            closing(
                FifoCheckpoint.create(directory / "partial-processed")
            ) as processed,
            McpClient(
                binary,
                execution.serve(
                    "--worker",
                    str(fixtures / "zod"),
                    "--relay",
                    str(fixtures / "server_relay/scripted_relay.py"),
                    *roots,
                ),
                {
                    **os.environ,
                    "TMPDIR": temporary,
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "ansi_polls",
                },
            ) as client,
        ):
            try:
                client.initialize_and_list_tools()
                # Finish shared discovery and lazy relay startup before polling.
                client.send(requirements={"action": "get"})
                client.send(control="restart")
                assert last_tool_text(client) == "[starting new worker]\n[idle]"
                running = "\n[running; poll with an empty send]"
                assert without_elapsed_result(client.send(r="42", timeout_ms=0))[
                    "content"
                ] == [{"type": "text", "text": running}]
                raw = b""
                for data, expected in (
                    (b"first\x1b[31", "first"),
                    (b"msecond\x1b]0;hidden", "msecond"),
                    (b"third\x1b[32m:\xe2", "third:"),
                    (b"\x82\xac\x1b[0m\n", "€\n"),
                ):
                    release.release()
                    processed.wait("ANSI bytes reached the output tape")
                    raw += data
                    result = client.send(timeout_ms=0)
                    assert without_elapsed_result(result) == {
                        "content": [{"type": "text", "text": expected + running}],
                        "isError": False,
                    }, result
                    assert (
                        cell_output_path(session_directory(client), 3)
                    ).read_bytes() == raw
                release.release()
                assert without_elapsed(last_tool_text(client)) == "€\n" + running
                client.send()
                assert last_tool_text(client) == "[done]", client.transcript[-1]
                return client.finish()
            finally:
                for _ in range(5):
                    release.release()


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_keeps_partial_utf8_across_polls_and_orders_stream_switches(
    binary: Path, execution: Execution
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        roots = ("--writable-root", temporary) if execution == SANDBOXED else ()
        with (
            closing(FifoCheckpoint.create(directory / "partial-release")) as release,
            closing(
                FifoCheckpoint.create(directory / "partial-processed")
            ) as processed,
            McpClient(
                binary,
                execution.serve(
                    "--worker",
                    str(fixtures / "zod"),
                    "--relay",
                    str(fixtures / "server_relay/scripted_relay.py"),
                    *roots,
                ),
                {
                    **os.environ,
                    "TMPDIR": temporary,
                    "MCP_CONSOLE_TEST_PREVIEW_DIRECTORY": temporary,
                    "MCP_CONSOLE_TEST_RELAY_SCENARIO": "partial_utf8_polls",
                },
                current_directory=directory,
            ) as client,
        ):
            try:
                client.initialize_and_list_tools()
                # Finish shared discovery and lazy relay startup before polling.
                client.send(requirements={"action": "get"})
                client.send(control="restart")
                assert last_tool_text(client) == "[starting new worker]\n[idle]"
                running = "\n[running; poll with an empty send]"
                assert without_elapsed_result(client.send(r="42", timeout_ms=0))[
                    "content"
                ] == [{"type": "text", "text": running}]
                session = next((directory / ".agents/console/sessions").iterdir())
                raw = b""
                for data, expected in (
                    (b"A\xe2", "A"),
                    (b"\x82\xacB\xe2", "€B"),
                    (b"C\xf0\x9f", "�C"),
                    (b" D\xe2", "� D"),
                ):
                    release.release()
                    processed.wait("direct bytes reached the output tape")
                    raw += data
                    result = client.send(timeout_ms=0)
                    assert without_elapsed_result(result) == {
                        "content": [{"type": "text", "text": expected + running}],
                        "isError": False,
                    }, result
                    assert without_elapsed_result(client.send(timeout_ms=0))[
                        "content"
                    ] == [{"type": "text", "text": running}]
                    assert (cell_output_path(session, 3)).read_bytes() == raw
                release.release()
                assert client.send()["content"] == [{"type": "text", "text": "�"}]
                assert client.send(r="42")["content"] == [
                    {"type": "text", "text": "��"}
                ]
                assert (cell_output_path(session, 13)).read_bytes() == b"\x82\xac"
                assert client.send()["content"] == [
                    {"type": "text", "text": "\n[idle]"}
                ]
                return client.finish()
            finally:
                for _ in range(5):
                    release.release()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_captures_worker_stdout(binary: Path, execution: Execution) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with (
        tempfile.TemporaryDirectory() as temporary,
        McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            {**os.environ, "TMPDIR": temporary},
        ) as client,
    ):
        client.initialize_and_list_tools()
        request = client.start_send(r="emit stdout")
        release = wait_for_marker(
            Path(temporary), "zod-release-stdout-completion", client
        )
        expected = large_output("zod stdout 👩🏽‍💻\n")
        recorded = cell_output_path(session_directory(client), 1)
        # stdout and completion use independent transports. A completed write
        # does not prove the server has captured the pipe's remaining bytes.
        deadline = time.monotonic() + 10
        with Events() as events:
            events.watch_file(recorded)
            events.watch_process(client.process.pid)
            while recorded.stat().st_size < len(expected.encode()):
                assert client.process.poll() is None, "server exited before capture"
                remaining = deadline - time.monotonic()
                assert remaining > 0 and events.wait(remaining), (
                    "server did not capture the complete stdout payload"
                )
        assert recorded.read_bytes() == expected.encode()
        release_fixture_checkpoint(release, client=client)
        client.receive(request)
        assert_preview(last_tool_text(client), expected)
        normalize_preview_paths(client)
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(POSIX, PROCESS_EVENTS)
def test_compacts_each_polled_output_segment(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

        # Finish shared discovery and lazy worker startup before polling.
        client.send(requirements={"action": "get"})
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"
        client.send(r="redraw across polls", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        release = wait_for_marker(temporary_path, "zod-release-redraw", client)
        release_fixture_checkpoint(release, client=client)
        wait_for_marker(
            temporary_path,
            "zod-redraw-ready",
            client,
        )

        client.send(timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == (
            "output 10%\n[running; poll with an empty send]"
        )
        client.send(timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )

        release = wait_for_marker(
            temporary_path, "zod-release-redraw-completion", client
        )
        release_fixture_checkpoint(release, client=client)
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "output 100%\n"
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_compacts_many_redraws_in_one_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.send(r="stress redraws")
    assert last_tool_text(client) == "stress final\nuseful output\n"
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_invalid_raw_output_when_worker_exits(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    for stream in ("stdout", "stderr"):
        client.send(r=f"exit after invalid {stream}")
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = (
            "\n[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 86]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n"
            "[idle]"
        )
        output = result["content"][0]["text"]
        assert output.endswith(failure), output[-200:]
        prefix = f"zod invalid {stream}: � trailing: �"
        raw_output = output.removesuffix(failure)
        marker_prefix = f"zod expected {stream} crash tail: "
        raw_output, tail_size = remove_length_marker(raw_output, marker_prefix)
        raw, recorded_tail = remove_length_marker(
            cell_text(client, 1 if stream == "stdout" else 2), marker_prefix
        )
        assert recorded_tail == tail_size
        assert raw == large_output(prefix) + ("z" * tail_size), (
            f"worker crash lost {stream} bytes"
        )
        assert_preview(raw_output, raw)
        normalize_pipe_counts(client)

    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_preserves_raw_output_during_malformed_sideband_failure(
    binary: Path,
    execution: Execution,
) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as temporary_directory:
        interposer = build_interposer(
            Path(temporary_directory), "relay_stdout_read_interposer"
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_RELAY_BINARY"] = str(binary)
        environment["MCP_CONSOLE_TEST_RELAY_READ_DYLIB"] = str(interposer)
        environment["MCP_CONSOLE_TEST_RELAY_READ_MATCH"] = "zod malformed output read\n"
        with McpClient(
            binary,
            execution.serve(
                "--worker",
                str(fixtures / "zod"),
                "--relay",
                str(fixtures / "retirement_read_relay"),
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()

            for stream in ("stdout", "stderr"):
                client.send(r=f"malformed sideband after {stream}")
                result = client.transcript[-1]["result"]
                assert result["isError"] is True, result
                output = result["content"][0]["text"]
                marker_prefix = f"zod expected {stream} malformed tail: "
                output, tail_size = remove_length_marker(output, marker_prefix)
                prefix = f"zod malformed {stream}: "
                raw = (
                    large_output(prefix)
                    + ("z" * tail_size)
                    + "zod malformed output read\n"
                )
                failure_start = output.find("[worker sideband read failed: ")
                assert failure_start >= 0, output[-200:]
                failure_end = output.find("\n", failure_start)
                assert failure_end >= 0, output[-200:]
                failure = output[failure_start:failure_end]
                notices = [
                    failure,
                    "[worker terminated by signal 9]",
                    "[worker stopped: in-memory state lost]",
                    "[starting new worker]",
                    "[idle]",
                ]
                recorded, recorded_tail = remove_length_marker(
                    cell_text(client, 1 if stream == "stdout" else 2), marker_prefix
                )
                assert recorded_tail == tail_size
                assert recorded == raw, f"malformed frame lost {stream} bytes"
                assert all(output.count(notice) == 1 for notice in notices), repr(
                    output
                )
                assert [output.index(notice) for notice in notices] == sorted(
                    output.index(notice) for notice in notices
                )
                assert output.endswith("\n".join(notices)), output[-500:]
                assert_preview(output.removesuffix("\n".join(notices)), raw)
                normalize_pipe_counts(client)

            compact_previews(client, "x", "y", "z", "s", "p", "ab")
            transcript, standard_error = client.finish_with_standard_error()
            diagnostics = standard_error.splitlines()
            # Relay stderr is diagnostic-only and can be cut off when the server's
            # fail-safe stops a failed generation. The framed failure above is authoritative.
            assert len(diagnostics) <= 2, standard_error
            assert all(
                diagnostic.startswith("worker sideband read failed: ")
                for diagnostic in diagnostics
            ), standard_error
            return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_raw_output_during_semantically_invalid_sideband_message(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()

    client.send(r="unexpected input receipt after stdout")
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    output = result["content"][0]["text"]
    marker_prefix = "zod expected semantic tail: "
    output, tail_size = remove_length_marker(output, marker_prefix)
    prefix = "zod unexpected input receipt: "
    raw = large_output(prefix) + ("z" * tail_size)
    notices = [
        "[worker reported received input without requesting it]",
        "[worker terminated by signal 9]",
        "[worker stopped: in-memory state lost]",
        "[starting new worker]",
        "[idle]",
    ]
    recorded, recorded_tail = remove_length_marker(cell_text(client, 1), marker_prefix)
    assert recorded_tail == tail_size
    assert recorded == raw, "semantic failure lost raw stdout bytes"
    assert all(output.count(notice) == 1 for notice in notices), repr(output)
    assert [output.index(notice) for notice in notices] == sorted(
        output.index(notice) for notice in notices
    )
    assert output.endswith("\n".join(notices)), output[-500:]
    # The builder terminates the raw line before its failure notices.
    assert_preview(output.removesuffix("\n".join(notices)).removesuffix("\n"), raw)
    normalize_pipe_counts(client)
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_drains_background_stderr_while_idle(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()
        client.send(r="start background stderr")
        assert last_tool_text(client) == "[done]"
        started = wait_for_marker(
            temporary_path,
            "zod-background-stderr-started",
            client,
        )
        (started.parent / "zod-release-background-stderr").touch()
        wait_for_marker(
            temporary_path,
            "zod-background-stderr-emitted",
            client,
        )

        # The sideband round trip does not order the independent stderr stream.
        # Wait for all known bytes to reach the public log before observing it.
        expected = large_output("zod background stderr\n") + ("y" * LARGE_OUTPUT_SIZE)
        log = session_directory(client) / "capture/sessions/001/cell-001-post.txt"
        wait_for_checkpoint(
            lambda: (
                log if log.exists() and log.stat().st_size == len(expected) else None
            ),
            "complete background stderr log",
            root=log,
            client=client,
        )
        client.send(timeout_ms=0)
        output = last_tool_text(client)
        assert output.endswith("\n[idle]"), output[-100:]
        assert len(output.encode()) <= TEXT_BUDGET
        assert "capture/sessions/001/cell-001-post.txt" in output
        assert "cell-001.txt" not in output
        assert cell_text(client, 1) == ""
        preview = output.removesuffix("\n[idle]")
        (marker,) = list(OMISSION.finditer(preview))
        observed = (
            len(preview[: marker.start()].encode())
            + int(marker[1])
            + len(preview[marker.end() :].encode())
        )
        assert observed == len(expected), observed
        assert log.read_text() == expected
        assert_preview(preview, expected)
        normalize_pipe_counts(client)
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_times_out_and_polls_running_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    client = McpClient(
        binary,
        execution.serve("--worker", str(zod)),
    )
    client.initialize_and_list_tools()
    client.send(r="echo echo")
    client.send(
        r="complete after timeout",
        timeout_ms=10,
    )
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert without_elapsed(output) == "\n[running; poll with an empty send]", output
    client.send(timeout_ms=3_000)
    output = client.transcript[-1]["result"]["content"][0]["text"]
    assert output == "zod: complete after timeout\n", output
    client.send(r="echo echo")
    compact_previews(client, "x", "y", "z", "s", "p", "ab")
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_drains_pending_sideband_output_while_running(
    binary: Path, execution: Execution
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
        )
        client.initialize_and_list_tools()

        # Finish shared discovery and lazy worker startup before polling.
        client.send(requirements={"action": "get"})
        client.send(control="restart")
        assert last_tool_text(client) == "[starting new worker]\n[idle]"
        client.send(r="emit output and image before completion", timeout_ms=0)
        assert (
            without_elapsed(last_tool_text(client))
            == "\n[running; poll with an empty send]"
        )
        image_started = wait_for_marker(
            temporary_path,
            "zod-image-evaluation-started",
            client,
        )
        (image_started.parent / "zod-release-image").touch()
        wait_for_marker(temporary_path, "zod-image-processed", client)

        client.send(timeout_ms=0)
        result = client.transcript[-1]["result"]
        assert without_elapsed_result(result) == {
            "content": [
                {"type": "text", "text": "before pending image\n"},
                {"type": "image", "data": PNG_1X1, "mimeType": "image/png"},
                {
                    "type": "text",
                    "text": "after pending image\n\n[running; poll with an empty send]",
                },
            ],
            "isError": False,
        }, result
        assert client.temporary_directory is not None
        workspace = Path(client.temporary_directory.name)
        session = next((workspace / ".agents/console" / "sessions").iterdir())
        assert (cell_output_path(session, 3)).read_text(
            encoding="utf-8"
        ) == "before pending image\nafter pending image\n"

        (image_started.parent / "zod-release-image-completion").touch()
        client.send(timeout_ms=3_000)
        assert last_tool_text(client) == "[done]"
        compact_previews(client, "x", "y", "z", "s", "p", "ab")
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_orders_queued_cancellation_behind_incomplete_response(
    binary: Path,
    execution: Execution,
) -> Transcript:
    zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"
    environment = os.environ.copy()
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        write_reached = FifoCheckpoint.create(temporary / "response-write-reached")
        write_release = FifoCheckpoint.create(temporary / "response-write-release")
        environment[LOADER_VARIABLE] = str(
            build_interposer(temporary, "response_write_interposer")
        )
        environment["MCP_CONSOLE_TEST_RESPONSE_WRITE_REACHED"] = str(write_reached.path)
        environment["MCP_CONSOLE_TEST_RESPONSE_WRITE_RELEASE"] = str(write_release.path)
        release = temporary / "response-gate-released"
        environment["TMPDIR"] = temporary_directory
        environment["ZOD_TEST_RESPONSE_GATE_RELEASED"] = str(release)
        with ZodFixtureControl(temporary) as control:
            control.configure(environment)
            client = SocketGateMcpClient(
                binary,
                execution.serve("--worker", str(zod)),
                environment,
                temporary,
            )
            observer: ResponseGateObserver | None = None
            finished = False
            try:
                client.initialize_and_list_tools()
                observer = ResponseGateObserver(
                    temporary,
                    client.stdout.stream,
                    release,
                )

                invalid_requirement = (
                    "https://invalid.example/" + "x" * TEST_GATED_RESPONSE_SIZE
                )
                first_id = client._next_request_id
                first = client.start_send(
                    requirements={"python": [invalid_requirement]}
                )
                assert first["id"] == first_id, first
                write_reached.wait("response prefix written")
                buffered = client.stdout.wait_for_incomplete_response(
                    first_id,
                    len(invalid_requirement),
                    control.diagnostics(),
                )
                control.record_client_event(
                    first_id,
                    "response_writer_reached_gate",
                    buffered_bytes=buffered,
                )

                cancelled_id = client._next_request_id
                cancelled = client.start_send(r=f"check response gate: {cancelled_id}")
                assert cancelled["id"] == cancelled_id, cancelled
                client.wait_until_input_is_read(
                    f"cancelled request {cancelled_id}", control
                )

                client.notify(
                    "notifications/cancelled",
                    requestId=cancelled_id,
                    reason="cancel before worker admission",
                )
                client.wait_until_input_is_read(
                    f"cancellation for request {cancelled_id}", control
                )
                control.record_client_event(cancelled_id, "operation_accepted")

                live_id = client._next_request_id
                live = client.start_send(r=f"check response gate: {live_id}")
                assert live["id"] == live_id, live
                client.wait_until_input_is_read(f"live request {live_id}", control)
                control.record_client_event(
                    cancelled_id,
                    "cancellation_observed_before_worker_admission",
                )

                barrier_target = 1_000_000
                client.notify(
                    "notifications/cancelled",
                    requestId=barrier_target,
                    reason="staged receive barrier",
                )
                client.wait_until_input_is_read("staged receive barrier", control)
                control.record_client_event(live_id, "operation_accepted")

                write_release.release()
                client.stdout.release_completed_response(
                    first_id,
                    release,
                    control.diagnostics(),
                )
                observer.finish()
                control.record_client_event(first_id, "response_write_completed")
                client.receive(first)
                assert first["result"]["isError"] is True
                error = first["result"]["content"][0]["text"]
                assert len(error.encode()) <= TEXT_BUDGET
                assert error.startswith("Python requirement `https://invalid.example/")
                assert error.endswith(
                    "host-side managed resolution accepts named package requirements only"
                )
                first["send"]["requirements"]["python"] = [
                    "<large invalid Python requirement>"
                ]

                control.connect(client)
                started = control.wait_for(live_id, "worker_operation_started")
                assert started["response_gate_released"] is True, control.diagnostics()
                control.wait_for(live_id, "worker_operation_completed")
                client.receive(live)
                assert live["result"] == {
                    "content": [
                        {
                            "type": "text",
                            "text": "zod response-gated operation\n",
                        }
                    ],
                    "isError": False,
                }, live

                ping = client.request("ping")
                assert ping["result"] == {}, ping
                client.close_input_observer()
                transcript = client.finish()
                control.wait_for_eof()
                cancelled_events = [
                    event
                    for event in control.events
                    if event.get("operation") == cancelled_id
                    and event.get("component") == "fixture"
                ]
                assert not cancelled_events, control.diagnostics()
                control.assert_before(
                    (cancelled_id, "cancellation_observed_before_worker_admission"),
                    (live_id, "worker_operation_started"),
                )
                finished = True
                return transcript
            finally:
                write_release.release()
                write_reached.close()
                write_release.close()
                if not finished:
                    stop_client(client)
                if observer is not None:
                    observer.close()
                client.close_test_stdio()


if __name__ == "__main__":
    run_this_suite(__file__)
