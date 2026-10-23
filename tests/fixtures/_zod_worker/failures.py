"""Malformed bytes and semantically invalid sideband scenarios."""

import json
import os
import signal

from .control import wait_for_test_control
from .io import (
    LARGE_OUTPUT_SIZE,
    emit_large_output,
    fill_nonblocking,
    write_all,
)
from .protocol import (
    send,
    send_output,
    wait_for_server_to_process_sideband,
)
from .state import WorkerContext


def fail_sideband_during_shutdown(context: WorkerContext, source: str) -> None:
    send(context.writer, {"kind": "completed"})
    # The client receives completion before releasing this write, so the
    # relay's gated read cannot also hold the completed frame.
    wait_for_test_control(context, 0, "emit_shutdown_failure")
    context.writer.write('{"kind":"console_output","data":}\n')
    context.writer.flush()
    while True:
        signal.pause()


def violate_protocol(context: WorkerContext, source: str) -> None:
    send_output(context.writer, "zod output before protocol failure")
    send(context.writer, {"kind": "ready"})


def violate_protocol_after_stdout(context: WorkerContext, source: str) -> None:
    wait_for_server_to_process_sideband(context.reader, context.writer)
    observed = os.open(os.environ["MCP_CONSOLE_TEST_RELAY_READ_BLOCKED"], os.O_RDWR)
    emit_large_output(1, b"zod old stdout\n")
    write_all(1, b"zod stdout tail\n")
    # The relay has read the complete payload, but the interposer holds
    # its final read until retirement joins the stdout reader.
    assert os.read(observed, 1) == b"1"
    os.close(observed)
    send(context.writer, {"kind": "ready"})


def unexpected_input_receipt_after_stdout(context: WorkerContext, source: str) -> None:
    wait_for_server_to_process_sideband(context.reader, context.writer)
    emit_large_output(1, b"zod unexpected input receipt: ")
    tail_size = fill_nonblocking(1, b"z" * LARGE_OUTPUT_SIZE)
    write_all(1, f"zod expected semantic tail: {tail_size:010d}\n".encode())
    send(context.writer, {"kind": "input_received"})
    while True:
        signal.pause()


def malformed_sideband_after_raw_output(context: WorkerContext, source: str) -> None:
    # Cell entry and raw streams travel independently. Establish the observed
    # cell interval before testing retention through a protocol failure.
    wait_for_server_to_process_sideband(context.reader, context.writer)
    stream = source.removeprefix("malformed sideband after ")
    descriptor = 1 if stream == "stdout" else 2
    observed = os.open(os.environ["MCP_CONSOLE_TEST_RELAY_READ_BLOCKED"], os.O_RDWR)
    emit_large_output(descriptor, f"zod malformed {stream}: ".encode())
    tail_size = fill_nonblocking(descriptor, b"z" * LARGE_OUTPUT_SIZE)
    write_all(
        descriptor,
        f"zod expected {stream} malformed tail: {tail_size:010d}\n".encode(),
    )
    write_all(descriptor, b"zod malformed output read\n")
    # Hold the complete tail in the raw reader until retirement joins it.
    assert os.read(observed, 1) == b"1"
    os.close(observed)
    context.writer.write('{"kind":"console_output","data":}\n')
    context.writer.flush()
    while True:
        signal.pause()


def force_stop_after_raw_output(context: WorkerContext, source: str) -> None:
    wait_for_server_to_process_sideband(context.reader, context.writer)
    stream = source.removeprefix("force stop after raw ")
    descriptor = 1 if stream == "stdout" else 2
    write_all(
        descriptor,
        f"zod retiring {stream}: ".encode() + b"\xe2\x82",
    )
    send(context.writer, {"kind": "ready"})
    while True:
        signal.pause()


def preview_invalid_oversized_image(context: WorkerContext, source: str) -> None:
    send_output(context.writer, "before invalid image\n")
    send(
        context.writer,
        {
            "kind": "image",
            "data": "A" * (8 * 1024 * 1024) + "AA?=",
            "mime_type": "image/png",
        },
    )
    # If validation is skipped, completion returns a successful result
    # that the public test rejects. Fatal validation instead requests
    # shutdown; remain alive for the test's forced-retirement assertion.
    send(context.writer, {"kind": "completed"})
    response = json.loads(context.reader.readline())
    assert response == {"kind": "shutdown"}, response
    while True:
        signal.pause()


def exit_after_invalid_raw_output(context: WorkerContext, source: str) -> None:
    wait_for_server_to_process_sideband(context.reader, context.writer)
    stream = source.removeprefix("exit after invalid ")
    descriptor = 1 if stream == "stdout" else 2
    emit_large_output(
        descriptor,
        f"zod invalid {stream}: ".encode() + b"\xff trailing: \xe2\x82",
    )
    tail_size = fill_nonblocking(descriptor, b"z" * LARGE_OUTPUT_SIZE)
    write_all(
        descriptor,
        f"zod expected {stream} crash tail: {tail_size:010d}\n".encode(),
    )
    os._exit(86)
