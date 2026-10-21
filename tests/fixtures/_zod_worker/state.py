"""Persistent state for one worker; constructing it has no runtime effects."""

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, TextIO


class LoopAction(Enum):
    """A handler may end main instead of returning to the receive loop."""

    STOP = "stop"


@dataclass
class WorkerContext:
    root: Path
    reader: TextIO
    writer: TextIO

    prepared_r_library: str | None = None
    fail_next_r_preparation: bool = False
    emit_output_before_r_preparation_failure: bool = False
    received_sigint: bool = False
    controlled_restart_state: str = "fresh"
    controlled_restart_evaluations: int = 0
    python_globals: dict[str, Any] = field(default_factory=dict)
    # One callback-interleaved message, consumed on the next loop iteration.
    queued_message: dict[str, Any] | None = None
    idle_input_received: bool = False
    block_next_sideband_write: int | None = None
    retain_blocked_sideband: bool = False
    recording_startup_pending: bool = False

    # Opened by control.py; closed only by scenarios that explicitly do so.
    test_event_descriptor: int | None = None
    test_control_descriptor: int | None = None
    test_cleanup_descriptor: int | None = None
    test_response_query_descriptor: int | None = None
    test_response_result_descriptor: int | None = None
