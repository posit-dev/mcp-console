"""R preparation and worker-originated resolver callbacks."""

import json
import os
from pathlib import Path
from typing import Any

from .output import PNG_1X1
from .protocol import (
    send,
    send_batch,
    send_output,
)
from .state import WorkerContext


def resolve_python_while_idle(context: WorkerContext, source: str) -> None:
    send_batch(
        context.writer,
        [
            {"kind": "completed"},
            {
                "kind": "resolve_python_version",
                "request": {"constraints": [">=3.11"]},
            },
        ],
    )
    receive_resolver_response(context, "python_version_resolution_failed")
    requirements = {"packages": ["numpy", "pandas", "idle-package"]}
    send(
        context.writer,
        {
            "kind": "resolve_python",
            "request": {
                "requirements": requirements,
                "retained_requirements": requirements,
            },
        },
    )
    receive_resolver_response(context, "python_resolution_failed")


def report_runtime_r_resolution_failure(context: WorkerContext, source: str) -> None:
    send(
        context.writer,
        {
            "kind": "resolve_r",
            "packages": ["blockedresolver"],
        },
    )
    response = receive_resolver_response(context, "r_resolution_failed")
    send_output(
        context.writer,
        (f"zod R resolution failure: {response['failure']}: {response['message']}\n"),
    )
    send(context.writer, {"kind": "completed"})


def report_managed_r_requirement(context: WorkerContext, source: str) -> None:
    expected_r_library = Path(
        os.environ["MCP_CONSOLE_TEST_R_LIBRARY_IDENTITY"]
    ).read_text(encoding="utf-8")
    configured_r_libraries = [
        library for library in os.environ.get("R_LIBS", "").split(os.pathsep) if library
    ]
    r_prepared = str(
        context.prepared_r_library == expected_r_library
        or configured_r_libraries[:1] == [expected_r_library]
    ).lower()
    send_output(context.writer, f"zod R requirement: prepared={r_prepared}\n")
    send(context.writer, {"kind": "completed"})


def report_raw_r_library_bytes(context: WorkerContext, source: str) -> None:
    libraries = os.environb.get(b"R_LIBS", b"").split(os.pathsep.encode())
    preserved = any(path.endswith(b"/ambient-\xff") for path in libraries)
    send_output(
        context.writer, f"zod raw R library: preserved={str(preserved).lower()}\n"
    )
    send(context.writer, {"kind": "completed"})


def fail_next_r_preparation(context: WorkerContext, source: str) -> None:
    context.fail_next_r_preparation = True
    send(context.writer, {"kind": "completed"})


def fail_next_r_preparation_after_output(context: WorkerContext, source: str) -> None:
    context.fail_next_r_preparation = True
    context.emit_output_before_r_preparation_failure = True
    send(context.writer, {"kind": "completed"})


def report_managed_python_activation(context: WorkerContext, source: str) -> None:
    send(
        context.writer,
        {
            "kind": "python_activated",
            "requirements": {"packages": ["numpy", "pandas"]},
        },
    )


def receive_resolver_response(
    context: WorkerContext, expected_kind: str
) -> dict[str, Any]:
    while True:
        response = json.loads(context.reader.readline())
        if response["kind"] == expected_kind:
            return response
        assert context.queued_message is None, response
        context.queued_message = response


def prepare_r(context: WorkerContext, message: dict[str, Any]) -> None:
    assert set(message) == {"kind", "library"}
    if context.fail_next_r_preparation:
        context.fail_next_r_preparation = False
        if context.emit_output_before_r_preparation_failure:
            context.emit_output_before_r_preparation_failure = False
            output = "before failed preparation\n"
            send_output(context.writer, output)
            send(
                context.writer,
                {
                    "kind": "image",
                    "data": PNG_1X1,
                    "mime_type": "image/png",
                },
            )
        send(
            context.writer,
            {
                "kind": "r_preparation_failed",
                "message": "zod rejected R preparation",
            },
        )
        return
    context.prepared_r_library = message["library"]
    send(context.writer, {"kind": "r_prepared", "library": context.prepared_r_library})
