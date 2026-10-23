#!/usr/bin/env -S uv run --script

import os
import select
import signal
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _assert_launcher_cleanup_barrier,
    _cleanup,
    _command_record,
    _start_lifetime,
    _wait_for_cleanup,
    _watch_process_exits,
)
from support.macos import (
    signal_darwin_process,
)
from support.macos import (
    wait_for_darwin_process_state as _wait_for_process_state,
)
from support.normalization import code
from support.records import Transcript
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    SANDBOX,
    requires,
)
from support.suites import run_this_suite


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_pending_signal_at_root_exit_preserves_status(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary)
    exit_events = select.kqueue()
    launcher_resumed = False
    try:
        root_exit = select.kevent(
            lifetime.root[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        assert exit_events.control([root_exit], 0, 0) == []
        assert signal_darwin_process(lifetime.launcher, signal.SIGSTOP), (
            "sandbox launcher exited before stop injection"
        )
        _wait_for_process_state(lifetime.launcher, "T", "sandbox launcher")

        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "sandbox launcher exited before pending-signal injection"
        )
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "sandbox root did not exit while launcher was stopped"
        assert events[0].ident == lifetime.root[0], events[0]
        assert events[0].filter == select.KQ_FILTER_PROC, events[0]
        assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]

        assert signal_darwin_process(lifetime.launcher, signal.SIGCONT), (
            "sandbox launcher exited before resume injection"
        )
        launcher_resumed = True
        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)

        assert returncode == 23, (returncode, stderr)
        assert stderr == "", stderr
        assert survivors == [], f"sandbox processes survived root exit: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "pending launcher signal preserved the sandbox temporary directory"
        )
        return [
            _command_record(lifetime),
            {
                "launcher_signal": "SIGSTOP",
                "pending_launcher_signal": "SIGTERM",
                "root_action": "exit 23",
                "verified_pending_signal": "before launcher resume",
            },
            {
                "launcher_signal": "SIGCONT",
                "launcher_returncode": returncode,
                "verified_signal": "consumed without replacing root status",
                "verified_cleanup": (
                    "sandbox root, detached descendant, manager, and temp"
                ),
            },
        ]
    finally:
        if not launcher_resumed:
            signal_darwin_process(lifetime.launcher, signal.SIGCONT)
        exit_events.close()
        _cleanup(lifetime)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_owned_sigterm_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary, exit_with_parent=os.getpid())
    cleanup = (lifetime.root, lifetime.target, lifetime.descendant, lifetime.manager)
    exit_events, watches = _watch_process_exits((*cleanup, lifetime.launcher))
    try:
        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "owned sandbox launcher exited before force-retirement request"
        )
        _assert_launcher_cleanup_barrier(
            exit_events,
            watches,
            lifetime.launcher,
            cleanup,
            lifetime.temporary_directory,
            "forced",
        )

        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)
        command = _command_record(lifetime)
        command["command"][3] = "<parent pid>"

        assert returncode == 0, returncode
        assert stderr == "", stderr
        assert survivors == [], f"owned sandbox processes survived SIGTERM: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "owned sandbox SIGTERM preserved the temporary directory"
        )
        return [
            command,
            {
                "launcher_signal": "SIGTERM",
                "launcher_returncode": returncode,
                "verified_cleanup_barrier": (
                    "launcher exited after sandbox root, detached descendant, manager, "
                    "and temp"
                ),
            },
        ]
    finally:
        exit_events.close()
        _cleanup(lifetime)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_owned_sigterm_retires_when_inherited_ignored(binary: Path) -> Transcript:
    lifetime = _start_lifetime(
        binary,
        exit_with_parent=os.getpid(),
        ignore_sigterm=True,
    )
    cleanup = (lifetime.root, lifetime.target, lifetime.descendant, lifetime.manager)
    exit_events, watches = _watch_process_exits((*cleanup, lifetime.launcher))
    try:
        assert signal_darwin_process(lifetime.launcher, signal.SIGTERM), (
            "owned sandbox launcher exited before force-retirement request"
        )
        _assert_launcher_cleanup_barrier(
            exit_events,
            watches,
            lifetime.launcher,
            cleanup,
            lifetime.temporary_directory,
            "forced",
        )

        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)
        command = _command_record(lifetime)
        command["command"][3] = "<parent pid>"

        assert returncode == 0, returncode
        assert stderr == "", stderr
        assert survivors == [], f"owned sandbox processes survived SIGTERM: {survivors}"
        assert not lifetime.temporary_directory.exists(), (
            "owned sandbox SIGTERM preserved the temporary directory"
        )
        return [
            command,
            {
                "inherited_sigterm": "ignored",
                "launcher_signal": "SIGTERM",
                "launcher_returncode": returncode,
                "verified_cleanup_barrier": (
                    "launcher exited after sandbox root, detached descendant, manager, "
                    "and temp"
                ),
            },
        ]
    finally:
        exit_events.close()
        _cleanup(lifetime)


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_owned_root_exit_waits_for_cleanup(binary: Path) -> Transcript:
    lifetime = _start_lifetime(binary, exit_with_parent=os.getpid())
    cleanup = (lifetime.root, lifetime.target, lifetime.descendant, lifetime.manager)
    exit_events, watches = _watch_process_exits((*cleanup, lifetime.launcher))
    try:
        lifetime.process.stdin.write(b"exit\n")
        lifetime.process.stdin.close()
        _assert_launcher_cleanup_barrier(
            exit_events,
            watches,
            lifetime.launcher,
            cleanup,
            lifetime.temporary_directory,
            "natural-root",
        )

        returncode = lifetime.process.wait(timeout=TIMEOUT)
        stderr = lifetime.process.stderr.read().decode("utf-8")
        survivors = _wait_for_cleanup(lifetime)
        command = _command_record(lifetime)
        command["command"][3] = "<parent pid>"

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert survivors == [], (
            f"owned sandbox processes survived root exit: {survivors}"
        )
        assert not lifetime.temporary_directory.exists(), (
            "owned sandbox root exit preserved the temporary directory"
        )
        return [
            command,
            {
                "root_action": "exit 23",
                "launcher_returncode": returncode,
                "verified_cleanup_barrier": (
                    "launcher exited after sandbox root, detached descendant, manager, "
                    "and temp"
                ),
            },
        ]
    finally:
        exit_events.close()
        _cleanup(lifetime)


@requires(SANDBOX)
def test_preserves_status_when_sigchld_was_ignored(binary: Path) -> Transcript:
    # Darwin preserves the ignored disposition across exec but clears its
    # no-child-wait state. Exercise the real binary entry point so later
    # supervision changes continue to preserve the command's waitable status.
    # fmt: python
    host_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    arguments = (
        "sandbox",
        "--",
        "python",
        "-c",
        "raise SystemExit(23)",
    )
    result = subprocess.run(
        [sys.executable, "-c", host_script, binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )

    assert result.returncode == 23, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "command": ["mcp-console", *arguments],
            "inherited_sigchld": "ignored",
            "exit_code": result.returncode,
        }
    ]


@requires(SANDBOX)
def test_preserves_inherited_ignored_signals(binary: Path) -> Transcript:
    # fmt: python
    host_script = code(r"""
        import os
        import signal
        import sys

        for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGCHLD"):
            signal.signal(getattr(signal, name), signal.SIG_IGN)
        signal.signal(max(signal.valid_signals()), signal.SIG_IGN)
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    # fmt: python
    target_script = code(r"""
        import os
        import signal

        for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGCHLD"):
            number = getattr(signal, name)
            assert signal.getsignal(number) == signal.SIG_IGN, name
            os.kill(os.getpid(), number)
            print(name, "ignored")
        highest = max(signal.valid_signals())
        assert signal.getsignal(highest) == signal.SIG_IGN
        os.kill(os.getpid(), highest)
        print("highest signal ignored")
        """)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            host_script,
            binary,
            "sandbox",
            "--",
            sys.executable,
            "-c",
            target_script,
        ],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert result.stdout.splitlines() == [
        f"{name} ignored" for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGCHLD")
    ] + ["highest signal ignored"], result.stdout
    return [{"inherited_signals": "ignored", "stdout": result.stdout, "exit_code": 0}]


if __name__ == "__main__":
    run_this_suite(__file__)
