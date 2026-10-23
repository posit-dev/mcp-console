"""Response gates, blocked I/O, and descendants retaining descriptors."""

import errno
import os
import select
import signal
import sys
import time
from pathlib import Path
from typing import TextIO

from .control import (
    close_test_cleanup_gate,
    close_test_control_channel,
    duplicate_test_cleanup_gate,
    emit_test_event,
    publish_marker,
    response_gate_completed,
    wait_for_test_control,
)
from .io import (
    LARGE_OUTPUT_SIZE,
    discard_stdin_bytes,
    emit_large_output,
    probe_pending_stdin,
    queued_stdin_bytes,
    write_all,
)
from .output import PNG_1X1
from .protocol import (
    close_sideband,
    send,
    send_output,
    wait_for_server_to_process_sideband,
)
from .state import WorkerContext


def check_response_gate(context: WorkerContext, source: str) -> None:
    operation = int(source.removeprefix("check response gate: "))
    gate_released = response_gate_completed(context)
    emit_test_event(
        context,
        operation,
        "worker_operation_started",
        response_gate_released=gate_released,
    )
    send_output(context.writer, "zod response-gated operation\n")
    emit_test_event(context, operation, "worker_operation_completed")
    send(context.writer, {"kind": "completed"})


def checkpoint(context: WorkerContext, source: str) -> None:
    operation = int(source.removeprefix("checkpoint "))
    emit_test_event(context, operation, "worker_operation_started")
    emit_test_event(context, operation, "worker_operation_completed")
    send(context.writer, {"kind": "completed"})


def stall_accepted_relay_shutdown(context: WorkerContext, source: str) -> None:
    relay_pid = os.getppid()
    helper = os.fork()
    if helper == 0:
        os.setsid()
        close_sideband(context.reader, context.writer)
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        publish_marker(
            context.root / "zod-relay-retirement-processes",
            f"{os.getpid()} {relay_pid}",
        )
        while True:
            signal.pause()
    assert os.read(0, 1) == b""
    # The relay's read checkpoint holds this output until reader join.
    # Keep the worker alive so shutdown must consume its grace period.
    write_all(1, b"zod output during relay retirement\n")
    while True:
        signal.pause()


def stall_with_stopped_relay(context: WorkerContext, source: str) -> None:
    relay_pid = int(os.environ["ZOD_RELAY_PID"])
    worker_pid = os.getpid()
    assert os.getppid() == relay_pid
    assert relay_pid != worker_pid
    assert os.getpgid(worker_pid) == os.getpgid(relay_pid) == os.getpgrp()
    helper = os.fork()
    if helper == 0:
        os.setsid()
        close_sideband(context.reader, context.writer)
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        publish_marker(
            context.root / "zod-relay-stop-helper",
            str(os.getpid()),
        )
        publish_marker(
            context.root / "zod-relay-stop-target",
            f"{relay_pid} {worker_pid}",
        )
        os.kill(relay_pid, signal.SIGSTOP)
        while True:
            signal.pause()
    while True:
        signal.pause()


def kill_relay_and_remain_live(context: WorkerContext, source: str) -> None:
    release = context.root / "zod-release-relay-exit"
    os.mkfifo(release)
    publish_marker(
        context.root / "zod-relay-exit-evaluation-started",
        f"{os.getpid()} {os.getppid()} {os.getpgrp()}",
    )
    with release.open("rb", buffering=0) as checkpoint:
        assert checkpoint.read(1) == b"1"
    worker_pid = os.getpid()
    relay_pid = int(os.environ["ZOD_RELAY_PID"])
    relay_group = os.getpgrp()
    assert worker_pid != relay_pid
    assert os.getppid() == relay_pid
    assert os.getpgid(relay_pid) == relay_group
    send_output(
        context.writer,
        (f"zod worker pid: {worker_pid}; relay process group: {relay_group}\n"),
    )
    wait_for_server_to_process_sideband(context.reader, context.writer)
    os.kill(relay_pid, signal.SIGKILL)
    while True:
        signal.pause()


def start_background_stderr(context: WorkerContext, source: str) -> None:
    child = os.fork()
    if child == 0:
        for descriptor in (0, 1):
            os.close(descriptor)
        emit_background_stderr(context.root, context.reader, context.writer)
        os._exit(0)
    publish_marker(
        context.root / "zod-background-stderr-pid",
        str(child),
    )
    publish_marker(context.root / "zod-background-stderr-started")
    send(context.writer, {"kind": "completed"})
    child_pid, status = os.waitpid(child, 0)
    assert child_pid == child
    assert os.waitstatus_to_exitcode(status) == 0


def start_background_sideband(context: WorkerContext, source: str) -> None:
    marker = source.removeprefix("start background sideband")
    marker = marker.removeprefix(": ")
    suffix = f"-{marker}" if marker else ""
    child = os.fork()
    if child == 0:
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        release = context.root / f"zod-release-background-sideband{suffix}"
        while not release.exists():
            time.sleep(0.01)
        if marker == "combined-requirements-failure":
            send_output(context.writer, "idle before failure image\n")
            send(
                context.writer,
                {
                    "kind": "image",
                    "data": PNG_1X1,
                    "mime_type": "image/png",
                },
            )
            send_output(context.writer, "idle after failure image\n")
        else:
            send_output(context.writer, "zod background sideband\n")
        wait_for_server_to_process_sideband(context.reader, context.writer)
        publish_marker(context.root / f"zod-background-sideband-emitted{suffix}")
        os._exit(0)
    send(context.writer, {"kind": "completed"})
    publish_marker(context.root / f"zod-background-sideband-started{suffix}")
    child_pid, status = os.waitpid(child, 0)
    assert child_pid == child
    assert os.waitstatus_to_exitcode(status) == 0


def partial_sideband_descendant(context: WorkerContext, source: str) -> None:
    release = context.root / "zod-release-partial-sideband"
    os.mkfifo(release)
    child = os.fork()
    if child == 0:
        os.setsid()
        context.reader.close()
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        publish_marker(
            context.root / "zod-sideband-descendant-pid",
            str(os.getpid()),
        )
        while True:
            signal.pause()
    with release.open("rb", buffering=0) as stream:
        assert stream.read(1) == b"x"
    context.writer.write('{"kind":"console_output"')
    context.writer.flush()
    emit_test_event(context, 0, "partial_sideband_written")
    if source == "start partial sideband descendant":
        # Requested retirement closes stdin before the worker exits.
        assert os.read(0, 1) == b""
    os._exit(86)


def wait_after_readable_frame_and_partial_tail(
    context: WorkerContext, source: str
) -> None:
    cleanup_source = duplicate_test_cleanup_gate(context)
    child = os.fork()
    if child == 0:
        os.setsid()
        cleanup = os.dup(cleanup_source)
        os.close(cleanup_source)
        context.reader.close()
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        publish_marker(
            context.root / "zod-sideband-descendant-pid",
            str(os.getpid()),
        )
        assert os.read(cleanup, 1) in {b"", b"1"}
        os.close(cleanup)
        context.writer.close()
        os._exit(0)
    os.close(cleanup_source)
    close_test_cleanup_gate(context)
    send_output(context.writer, "zod readable retirement frame\n")
    context.writer.write('{"kind":"console_output"')
    context.writer.flush()
    publish_marker(context.root / "zod-sideband-partial-tail-written")
    assert os.read(0, 1) == b""
    os._exit(86)


def complete_before_partial_sideband_descendant(
    context: WorkerContext, source: str
) -> None:
    child = os.fork()
    if child == 0:
        os.setsid()
        context.reader.close()
        for descriptor in (0, 1, 2):
            os.close(descriptor)
        publish_marker(
            context.root / "zod-sideband-descendant-pid",
            str(os.getpid()),
        )
        while True:
            signal.pause()
    send(context.writer, {"kind": "completed"})
    context.writer.write('{"kind":"console_output"')
    context.writer.flush()
    publish_marker(context.root / "zod-sideband-partial-tail-written")


def stall_with_detached_stdin(context: WorkerContext, source: str) -> None:
    operation = int(source.removeprefix("stall with detached stdin: "))
    emit_test_event(context, operation, "worker_operation_started")
    acknowledged, acknowledge = os.pipe()
    child = os.fork()
    if child == 0:
        os.setsid()
        os.close(acknowledged)
        retained_stdin = os.dup(0)
        assert os.fstat(retained_stdin) == os.fstat(0)
        os.close(0)
        cleanup = duplicate_test_cleanup_gate(context)
        close_test_cleanup_gate(context)
        close_test_control_channel(context)
        close_sideband(context.reader, context.writer)
        os.close(1)
        os.close(2)
        emit_test_event(
            context,
            operation,
            "detached_descendant_created",
            pid=os.getpid(),
            process_group=os.getpgrp(),
            inherited_fd=0,
            retained_fd=retained_stdin,
        )
        assert os.write(acknowledge, b"1") == 1
        os.close(acknowledge)
        assert os.read(cleanup, 1) in {b"", b"1"}
        os.close(cleanup)
        os.close(retained_stdin)
        os._exit(0)
    os.close(acknowledge)
    assert os.read(acknowledged, 1) == b"1"
    os.close(acknowledged)
    close_test_cleanup_gate(context)
    consumed_bytes = 0
    emit_test_event(context, operation, "parent_waiting_for_stdin")
    while True:
        command = wait_for_test_control(context, operation, "probe_stdin")
        request = command["request"]
        expected_bytes = command["expected_bytes"]
        assert isinstance(request, int), command
        assert isinstance(expected_bytes, int), command
        assert expected_bytes > consumed_bytes, command
        consumed_bytes, queued_bytes = probe_pending_stdin(
            expected_bytes,
            consumed_bytes,
        )
        details = {
            "target_operation": operation,
            "expected_bytes": expected_bytes,
            "consumed_bytes": consumed_bytes,
            "queued_bytes": queued_bytes,
        }
        if consumed_bytes + queued_bytes < expected_bytes:
            emit_test_event(context, request, "stdin_write_pending", **details)
            break
        emit_test_event(context, request, "stdin_write_buffered", **details)
    emit_test_event(
        context,
        operation,
        "parent_operation_stalled",
        pid=os.getpid(),
        process_group=os.getpgrp(),
    )
    ownership = wait_for_test_control(context, operation, "observe_poll_ownership")
    request = ownership["request"]
    prior_bytes = ownership["prior_bytes"]
    submitted_bytes = ownership["submitted_bytes"]
    sentinel = ownership["sentinel"]
    assert isinstance(request, int), ownership
    assert isinstance(prior_bytes, int), ownership
    assert isinstance(submitted_bytes, int), ownership
    assert isinstance(sentinel, str), ownership
    assert prior_bytes == expected_bytes, ownership
    assert submitted_bytes > 1, ownership
    sentinel_bytes = sentinel.encode()
    assert len(sentinel_bytes) == 1, ownership

    discard_stdin_bytes(prior_bytes - consumed_bytes)
    consumed_bytes = prior_bytes
    assert os.read(0, 1) == sentinel_bytes
    consumed_bytes += 1
    total_bytes = prior_bytes + submitted_bytes
    readable, _, _ = select.select([0], [], [], 15)
    assert readable, "poll ownership stdin tail did not reach Zod"
    queued_bytes = queued_stdin_bytes()
    assert 0 < queued_bytes < total_bytes - consumed_bytes, (
        queued_bytes,
        consumed_bytes,
        total_bytes,
    )
    emit_test_event(
        context,
        request,
        "poll_ownership_observed",
        target_operation=operation,
        consumed_bytes=consumed_bytes,
        queued_bytes=queued_bytes,
        submitted_bytes=submitted_bytes,
    )
    while True:
        signal.pause()


def probe_sandbox(context: WorkerContext, source: str) -> None:
    try:
        Path(os.environ["ZOD_SANDBOX_PROBE_PATH"]).write_text(
            "escaped",
            encoding="utf-8",
        )
    except OSError as error:
        assert error.errno == (errno.EROFS if sys.platform == "linux" else errno.EPERM)
        output = "sandbox blocked host write\n"
    else:
        output = "worker escaped sandbox\n"
    send_output(context.writer, output)
    send(context.writer, {"kind": "completed"})


def emit_background_stderr(root: Path, reader: TextIO, writer: TextIO) -> None:
    while not (root / "zod-release-background-stderr").exists():
        time.sleep(0.01)
    emit_large_output(2, b"zod background stderr\n")
    # Writing can finish with one pipe of bytes still unread. A further suffix
    # makes the intended payload reach the relay before the sideband round trip.
    write_all(2, b"y" * LARGE_OUTPUT_SIZE)
    wait_for_server_to_process_sideband(reader, writer)
    publish_marker(root / "zod-background-stderr-emitted")


def stall_sideband_reader(context: WorkerContext) -> None:
    assert context.reader.read(1) != ""
    if context.retain_blocked_sideband:
        holder = os.fork()
        if holder == 0:
            os.setsid()
            for descriptor in (0, 1, 2):
                os.close(descriptor)
            publish_marker(
                context.root / "zod-blocked-sideband-holder-pid",
                str(os.getpid()),
            )
            while True:
                signal.pause()
    emit_test_event(
        context,
        context.block_next_sideband_write,
        "sideband_reader_stalled",
        pid=os.getpid(),
        process_group=os.getpgrp(),
    )
    while True:
        signal.pause()


def configure_blocked_sideband(context: WorkerContext) -> None:
    if os.environ.get("ZOD_BLOCK_NEXT_SIDEBAND_WRITE") == "1":
        command = wait_for_test_control(context, 0, "block_next_sideband_write")
        target_operation = command.get("target_operation")
        assert isinstance(target_operation, int), command
        context.block_next_sideband_write = target_operation
        context.retain_blocked_sideband = (
            os.environ.get("ZOD_RETAIN_BLOCKED_SIDEBAND") == "1"
        )
