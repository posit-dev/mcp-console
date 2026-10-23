"""Startup scenarios run after sideband and fixture control setup."""

import os
import signal
import time
from pathlib import Path
from typing import TextIO

from .control import publish_marker
from .io import write_all
from .protocol import close_sideband


def configure_startup(
    root: Path,
    reader: TextIO,
    writer: TextIO,
) -> tuple[bool, bool]:
    control_path = os.environ.get("ZOD_STARTUP_CONTROL")
    if control_path is None:
        return False, False
    control = Path(control_path)
    mode = control.read_text(encoding="utf-8")
    if mode == "fail":
        os._exit(86)
    if mode == "fail with stderr":
        # Keep the checkpoint after the failed worker's sandbox is retired.
        publish_marker(control.with_name("zod-replacement-startup-failing"))
        write_all(2, b"zod replacement startup failed\n")
        os._exit(86)
    if mode == "block with detached sideband writer":
        child = os.fork()
        if child == 0:
            os.setsid()
            sideband_writer = os.dup(writer.fileno())
            close_sideband(reader, writer)
            for descriptor in (0, 1, 2):
                os.close(descriptor)
            publish_marker(
                root / "zod-detached-startup-sideband-pid",
                str(os.getpid()),
            )
            while True:
                signal.pause()
            os.close(sideband_writer)
        mode = "block"
    if mode == "block":
        checkpoint = os.environ.get("ZOD_STARTUP_STARTED")
        publish_marker(
            Path(checkpoint) if checkpoint else root / "zod-replacement-waiting-ready"
        )
        release = Path(os.environ["ZOD_STARTUP_RELEASE"])
        while not release.exists():
            time.sleep(0.01)
        mode = "ready"
    if mode == "ready with callback":
        return True, False
    assert mode == "ready", f"unsupported Zod startup mode: {mode}"
    return False, False
