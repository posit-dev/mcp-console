"""Portable command-routing specification observed by independent worker peers."""

import json
from pathlib import Path


STDIN_DATA = "stdin 🦀\nsecond line\r\n"
INTERRUPT_ID = 731
SHUTDOWN = {"kind": "shutdown", "grace_millis": 1000}

WORKER_COMMANDS = [
    {"kind": "evaluate", "language": "r", "source": "cat('R 🦀\\n')"},
    {"kind": "evaluate", "language": "python", "source": "print('Python 🦀')"},
    {"kind": "evaluate", "language": "sql", "source": "SELECT 'SQL 🦀'"},
    {"kind": "prepare_r", "library": "/libraries/requested R 🦀"},
    {"kind": "r_resolved", "library": "/libraries/resolved R 🦀"},
    *[
        {
            "kind": "r_resolution_failed",
            "failure": failure,
            "message": f"{failure}: R resolution 🦀\nsecond line",
        }
        for failure in ("host", "interrupted", "operation")
    ],
    {"kind": "prepare_python", "packages": ["pandas", "numpy", "custom_pkg>=2"]},
    {"kind": "python_resolved", "python": "/environments/managed Python 🦀"},
    {
        "kind": "python_resolved",
        "python": "/environments/native Python 🦀",
        "native": {
            "selected": {
                "embedding": {
                    "python": "/native/bin/python",
                    "libpython": "/native/lib/libpython",
                    "python_home": "/native/home",
                },
                "prefix": "/native/prefix",
                "exec_prefix": "/native/exec-prefix",
                "base_prefix": "/native/base-prefix",
                "base_exec_prefix": "/native/base-exec-prefix",
                "metadata": {
                    "base_executable": "/native/base/bin/python",
                    "pythonpath": "/native/extra 🦀",
                    "version": "3.13.11 (fixture)",
                    "version_number": "3.13.11",
                    "architecture": "fixture-architecture",
                    "conda": True,
                    "numpy": {"path": "/native/numpy", "version": "2.3.0"},
                },
            },
            "requirements": {
                "packages": ["custom_pkg>=2", "numpy", "pandas"],
                "python_version": ["<3.14", ">=3.13"],
                "exclude_newer": "2026-09-01T00:00:00Z",
            },
        },
    },
    {"kind": "python_resolution_failed", "message": "Python resolution 🦀\nfailed"},
    {"kind": "python_version_resolved", "version": ">=3.13,<3.14"},
    {"kind": "python_version_resolution_failed", "message": "version 🦀\nfailed"},
]


def command_batch() -> bytes:
    commands = [
        *WORKER_COMMANDS[:4],
        {"kind": "stdin", "data": STDIN_DATA},
        {"kind": "interrupt", "request_id": INTERRUPT_ID},
        *WORKER_COMMANDS[4:],
    ]
    return b"".join(
        json.dumps(command, ensure_ascii=False).encode() + b"\n" for command in commands
    )


def assert_forwarding(
    marker: Path, receipts: list[dict], tail: list[dict]
) -> list[dict]:
    # Stdin and interrupt acknowledgments are on independent channels. Only
    # sideband input order is promised; do not assert cross-channel chronology.
    assert [event for event in receipts if event["kind"] != "interrupt_result"] == [
        {"kind": "completed"}
    ] * len(WORKER_COMMANDS), receipts
    controls = [event for event in receipts if event["kind"] == "interrupt_result"]
    assert controls == [{"kind": "interrupt_result", "request_id": INTERRUPT_ID}], (
        controls
    )
    forwarded = [json.loads(line) for line in marker.read_bytes().splitlines()]
    assert forwarded == [*WORKER_COMMANDS, {"kind": "shutdown"}], forwarded
    stdin = marker.with_suffix(".stdin").read_bytes().decode()
    assert stdin == STDIN_DATA, stdin
    assert tail == [
        {"kind": "shutdown_started"},
        {"kind": "stdout_closed"},
        {"kind": "stderr_closed"},
        {"kind": "worker_sideband_closed"},
        {"kind": "worker_exited", "code": 0},
    ], tail
    return [
        {"forwarded": forwarded, "stdin": stdin, "controls": controls, "shutdown": tail}
    ]
