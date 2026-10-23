"""Fixture FIFOs, markers, and causal test gates owned by the worker."""

import json
import os
from pathlib import Path

from .state import WorkerContext

TEST_FIXTURE_CONTROL_ENV = "ZOD_TEST_FIXTURE_CONTROL"
TEST_EVENT_FIFO_NAME = "zod-test-events"
TEST_CONTROL_FIFO_NAME = "zod-test-control"
TEST_CLEANUP_FIFO_NAME = "zod-test-cleanup"
TEST_RESPONSE_QUERY_FIFO_NAME = "zod-test-response-query"
TEST_RESPONSE_RESULT_FIFO_NAME = "zod-test-response-result"
TEST_CONTROL_READY_NAME = "zod-test-control-ready"


def configure_test_fixture_control(context: WorkerContext) -> None:
    if os.environ.get(TEST_FIXTURE_CONTROL_ENV) != "1":
        return
    event_path = context.root / TEST_EVENT_FIFO_NAME
    control_path = context.root / TEST_CONTROL_FIFO_NAME
    cleanup_path = context.root / TEST_CLEANUP_FIFO_NAME
    paths = [event_path, control_path, cleanup_path]
    response_gate_enabled = "ZOD_TEST_RESPONSE_GATE_RELEASED" in os.environ
    if response_gate_enabled:
        paths.extend(
            [
                context.root / TEST_RESPONSE_QUERY_FIFO_NAME,
                context.root / TEST_RESPONSE_RESULT_FIFO_NAME,
            ]
        )
    for path in paths:
        os.mkfifo(path)
    context.test_event_descriptor = os.open(event_path, os.O_RDWR)
    context.test_control_descriptor = os.open(control_path, os.O_RDWR)
    context.test_cleanup_descriptor = os.open(cleanup_path, os.O_RDONLY | os.O_NONBLOCK)
    if response_gate_enabled:
        context.test_response_query_descriptor = os.open(
            context.root / TEST_RESPONSE_QUERY_FIFO_NAME,
            os.O_RDWR,
        )
        context.test_response_result_descriptor = os.open(
            context.root / TEST_RESPONSE_RESULT_FIFO_NAME,
            os.O_RDWR,
        )
    publish_marker(context.root / TEST_CONTROL_READY_NAME)


def emit_test_event(
    context: WorkerContext, operation: int, kind: str, **details: object
) -> None:
    if context.test_event_descriptor is None:
        return
    event = {
        "operation": operation,
        "kind": kind,
        "component": "fixture",
        **details,
    }
    payload = json.dumps(event, separators=(",", ":")).encode() + b"\n"
    assert len(payload) <= 512
    assert os.write(context.test_event_descriptor, payload) == len(payload)


def publish_marker(path: Path, contents: str | None = None) -> None:
    pending = path.with_name(f".{path.name}.{os.getpid()}.pending")
    if contents is None:
        pending.touch()
    else:
        pending.write_text(contents, encoding="utf-8")
    pending.replace(path)


def wait_for_test_control(
    context: WorkerContext, operation: int, kind: str
) -> dict[str, object]:
    assert context.test_control_descriptor is not None
    payload = bytearray()
    while not payload.endswith(b"\n"):
        chunk = os.read(context.test_control_descriptor, 1)
        assert chunk, f"test control closed before {kind!r} for request {operation}"
        payload.extend(chunk)
    command = json.loads(payload)
    assert command.get("operation") == operation, command
    assert command.get("kind") == kind, command
    return command


def duplicate_test_cleanup_gate(context: WorkerContext) -> int:
    assert context.test_cleanup_descriptor is not None
    duplicate = os.dup(context.test_cleanup_descriptor)
    os.set_blocking(duplicate, True)
    return duplicate


def close_test_cleanup_gate(context: WorkerContext) -> None:
    assert context.test_cleanup_descriptor is not None
    os.close(context.test_cleanup_descriptor)
    context.test_cleanup_descriptor = None


def close_test_control_channel(context: WorkerContext) -> None:
    assert context.test_control_descriptor is not None
    os.close(context.test_control_descriptor)
    context.test_control_descriptor = None


def response_gate_completed(context: WorkerContext) -> bool:
    assert context.test_response_query_descriptor is not None
    assert context.test_response_result_descriptor is not None
    assert os.write(context.test_response_query_descriptor, b"1") == 1
    completed = os.read(context.test_response_result_descriptor, 1)
    assert completed in {b"0", b"1"}, completed
    return completed == b"1"
