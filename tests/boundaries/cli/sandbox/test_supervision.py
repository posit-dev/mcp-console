#!/usr/bin/env -S uv run --script

import ctypes
import os
import selectors
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name == "posix":
    import termios


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    _command,
    _read_lines,
    _sandbox_root_pid,
    _start_with_controlling_terminal,
)
from support.macos import (
    DarwinProcessIdentity as _ProcessIdentity,
)
from support.macos import (
    capture_darwin_process_identity as _capture_identity,
)
from support.macos import (
    kill_darwin_processes as _kill_survivors,
)
from support.checkpoints import FifoCheckpoint
from support.normalization import code
from support.records import Transcript
from support.requirements import MACOS_SANDBOX, PROCESS_EVENTS, SANDBOX, requires
from support.suites import run_this_suite

TIMEOUT = 10


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_retires_processx_descendants_across_sessions(binary: Path) -> Transcript:
    # The processx child starts a new session on Unix. Its lightweight Python
    # program starts the sleep grandchild in a third session, so neither
    # descendant remains in its parent's process group or session.
    # fmt: r
    script = code(r"""
        child_script <- '
        import os
        import subprocess
        import time

        grandchild = subprocess.Popen(
            ["/bin/sleep", "60"],
            start_new_session=True,
        )
        payload = (
            str(grandchild.pid)
            + os.linesep
            + os.environ["TMPDIR"]
            + os.linesep
        ).encode()
        os.write(1, payload)
        time.sleep(60)
        '

        child <- processx::process$new(
          "python",
          c("-c", child_script),
          stdout = "|",
          stderr = "2>&1",
          cleanup = FALSE
        )
        stopifnot(child$poll_io(-1)[["output"]] == "ready")
        child_output <- child$read_output_lines()
        stopifnot(length(child_output) == 2L)
        writeLines(c(
          as.character(Sys.getpid()),
          as.character(child$get_pid()),
          child_output
        ))
        flush.console()
        stopifnot(identical(readLines("stdin", n = 1L), "exit"))
        quit(save = "no", status = 23L, runLast = FALSE)
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[_ProcessIdentity] = []
    try:
        # The case supervisor bounds R and descendant startup. TIMEOUT bounds
        # retirement after the complete process tree has reported readiness.
        root_pid, child_pid, grandchild_pid, temporary_directory = _read_lines(
            process.stdout,
            4,
            "the sandbox root, processx descendants, and temporary directory",
            timeout=None,
        )
        pids = [int(root_pid), int(child_pid), int(grandchild_pid)]
        for pid in pids:
            identities.append(_capture_identity(pid))
        identities.append(_capture_identity(_sandbox_root_pid(process.pid)))
        temporary_directory = Path(temporary_directory)

        assert os.getsid(pids[1]) != os.getsid(pids[0])
        assert os.getsid(pids[2]) != os.getsid(pids[1])

        process.stdin.write(b"exit\n")
        process.stdin.close()
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities[1:])

        assert returncode == 23, returncode
        assert stderr == "", stderr
        assert survivors == [], f"sandbox descendants survived: {survivors}"
        assert not temporary_directory.exists(), (
            f"sandbox temporary directory survived: {temporary_directory}"
        )
    finally:
        if process.poll() is None:
            if not process.stdin.closed:
                try:
                    process.stdin.write(b"exit\n")
                    process.stdin.close()
                except BrokenPipeError:
                    pass
            try:
                process.wait(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        if not process.stdin.closed:
            process.stdin.close()
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "exit_code": returncode,
            "stdout": (
                "<sandbox root pid>\n"
                "<processx child pid>\n"
                "<processx grandchild pid>\n"
                "<sandbox temp>\n"
            ),
            "verified_descendants": [
                "processx child outside root session",
                "detached grandchild outside processx child session",
            ],
        }
    ]


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_relays_interrupt_then_retires_descendants(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        child <- processx::process$new(
          "/bin/sleep",
          "60",
          cleanup = FALSE
        )
        tryCatch(
          {
            writeLines(c(
              as.character(Sys.getpid()),
              as.character(child$get_pid())
            ))
            flush.console()
            Sys.sleep(60)
          },
          interrupt = function(...) {
            quit(save = "no", status = 130L, runLast = FALSE)
          }
        )
        """)
    arguments = ("sandbox", "--", "Rscript", "--vanilla", "-e", script)
    process = subprocess.Popen(
        [binary, *arguments],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[_ProcessIdentity] = []
    try:
        # Bound startup with the case supervisor, then time signal delivery and
        # retirement only after R and its descendant have reported readiness.
        pids = [
            int(line)
            for line in _read_lines(
                process.stdout,
                2,
                "the sandbox root and processx descendant PIDs",
                timeout=None,
            )
        ]
        identities = [_capture_identity(pid) for pid in pids]
        root = _capture_identity(_sandbox_root_pid(process.pid))
        identities.append(root)
        assert os.getpgid(pids[0]) == root[0]
        assert root[0] == pids[0]
        assert os.getpgid(pids[1]) != os.getpgid(pids[0])
        os.kill(process.pid, signal.SIGINT)
        returncode = process.wait(timeout=TIMEOUT)
        stderr = process.stderr.read().decode("utf-8")
        survivors = _kill_survivors(identities)

        assert returncode == 130, returncode
        assert stderr == "", stderr
        assert survivors == [], f"interrupted sandbox processes survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()

    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox root pid>\n<processx child pid>\n",
            "verified_descendant": "processx child outside root process group",
        },
        {
            "signal": "SIGINT",
            "exit_code": returncode,
            "stderr": stderr,
        },
    ]


@requires(SANDBOX)
def test_sandbox_cannot_retain_its_temporary_directory(binary: Path) -> Transcript:
    # fmt: python
    sandboxed_script = code(r"""
        import os
        from pathlib import Path

        temporary_directory = Path(os.environ["TMPDIR"])
        (temporary_directory / ".mcp-console-preserve").write_text("retain\n")
        print(temporary_directory)
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)
    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    temporary_directory = Path(result.stdout.strip())
    assert result.returncode == 0, result
    assert result.stderr == "", result.stderr
    assert not temporary_directory.exists(), (
        f"sandbox retained its host-owned temporary directory: {temporary_directory}"
    )
    return [
        {
            "command": _command(*arguments),
            "stdout": "<sandbox temp>\n",
            "transcript_normalization": {
                "target": "stdout",
                "sandbox_temporary_directory": "omitted",
            },
        }
    ]


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_delivers_terminal_interrupt_once(binary: Path) -> Transcript:
    # Event.set() in a signal handler can deadlock on Event.wait()'s lock.
    # A wakeup pipe retains the signal even if it arrives before the read.
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal

        interrupts = 0
        interrupted, wakeup = os.pipe()
        os.set_blocking(wakeup, False)
        signal.set_wakeup_fd(wakeup)


        def handle_interrupt(_signal, _frame):
            global interrupts
            interrupts += 1


        signal.signal(signal.SIGINT, handle_interrupt)
        previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGINT})
        assert signal.SIGINT not in previous_mask
        print(f"ready {os.getpid()} {os.getpgrp()}", flush=True)
        print(input(), flush=True)
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)
        print("interrupt ready", flush=True)
        assert os.read(interrupted, 1) == bytes([signal.SIGINT])
        print(interrupts)
        """)
    arguments = [binary, "sandbox", "--", "python", "-c", sandboxed_script]
    process, master, _ = _start_with_controlling_terminal(arguments)
    identities: list[_ProcessIdentity] = []
    try:
        readiness = _read_lines(
            process.stdout,
            1,
            "the foreground sandbox readiness",
        )[0].split()
        assert len(readiness) == 3 and readiness[0] == "ready", readiness
        target_pid, target_group = map(int, readiness[1:])
        identities.append(_capture_identity(target_pid))
        root = _capture_identity(_sandbox_root_pid(process.pid))
        identities.append(root)
        assert target_group == root[0]
        assert target_group == target_pid
        assert target_group != process.pid
        assert os.tcgetpgrp(master) == target_group

        os.write(master, b"sandbox input\n")
        assert _read_lines(process.stdout, 2, "the sandbox input acknowledgement") == [
            "sandbox input",
            "interrupt ready",
        ]
        assert os.tcgetpgrp(master) == target_group
        terminal_attributes = termios.tcgetattr(master)
        assert terminal_attributes[3] & termios.ISIG
        assert terminal_attributes[6][termios.VINTR] == b"\x03"
        os.write(master, b"\x03")
        stdout, stderr = process.communicate(timeout=TIMEOUT)
        survivors = _kill_survivors(identities)

        assert process.returncode == 0, process.returncode
        assert stdout == b"1\n", stdout
        assert stderr == b"", stderr
        assert survivors == [], f"terminal sandbox survived: {survivors}"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        _kill_survivors(identities)
        process.stdout.close()
        process.stderr.close()
        os.close(master)

    return [
        {
            "command": _command("sandbox", "--", "python", "-c", sandboxed_script),
            "stdin": "sandbox input\n<Ctrl-C>",
            "stdout": stdout.decode("utf-8"),
            "terminal_ownership": "transferred to target",
        }
    ]


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_preserves_status_after_its_controlling_terminal_closes(
    binary: Path,
) -> Transcript:
    # Port of sandbox_preserves_status_after_its_controlling_terminal_closes
    # from tests/sandbox_terminal.rs at 660a9aaf (PR #5). The FIFO replaces its
    # sleep so the target cannot exit before terminal revocation and closure.
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import signal
        import sys

        signal.signal(signal.SIGHUP, signal.SIG_IGN)
        assert signal.getsignal(signal.SIGHUP) == signal.SIG_IGN
        assert os.isatty(0)
        with open(sys.argv[1], "rb", buffering=0) as release:
            print(f"ready {os.getpid()} {os.getpgrp()}", flush=True)
            assert release.read(1) == b"1"
        assert not os.isatty(0)
        print("terminal closed before release", flush=True)
        raise SystemExit(23)
        """)

    with tempfile.TemporaryDirectory() as directory:
        release = FifoCheckpoint.create(Path(directory) / "release")
        try:
            process, master, slave_name = _start_with_controlling_terminal(
                [
                    binary,
                    "sandbox",
                    "--",
                    "python",
                    "-c",
                    sandboxed_script,
                    release.path,
                ]
            )
            # File ownership makes the explicit master close and failure-path
            # closure idempotent, without ever closing a reused descriptor.
            with (
                os.fdopen(master, "rb", buffering=0) as terminal,
                process.stdout,
                process.stderr,
            ):
                identities: list[_ProcessIdentity] = []
                stdout = stderr = b""
                phase = "foreground readiness with SIGHUP ignored"
                try:
                    readiness = _read_lines(process.stdout, 1, phase)[0].split()
                    assert len(readiness) == 3 and readiness[0] == "ready", readiness
                    target_pid, target_group = map(int, readiness[1:])
                    target = _capture_identity(target_pid)
                    identities.append(target)
                    root = _capture_identity(_sandbox_root_pid(process.pid))
                    identities.append(root)
                    assert target == root
                    assert target_group == target_pid
                    assert target_group != process.pid
                    # Observe ownership from the host: the sandbox denies the
                    # target's foreground-group ioctl on an inherited PTY.
                    assert os.tcgetpgrp(terminal.fileno()) == target_group

                    phase = "terminal revocation"
                    libc = ctypes.CDLL(None, use_errno=True)
                    libc.revoke.argtypes = [ctypes.c_char_p]
                    libc.revoke.restype = ctypes.c_int
                    ctypes.set_errno(0)
                    status = libc.revoke(os.fsencode(slave_name))
                    assert status == 0, ("revoke", status, ctypes.get_errno())
                    phase = "master closure while target remains gated"
                    terminal.close()
                    assert process.poll() is None, process.returncode

                    phase = "post-loss release and status preservation"
                    release.release()
                    stdout, stderr = process.communicate(timeout=TIMEOUT)
                    assert process.returncode == 23, process.returncode
                    assert stdout == b"terminal closed before release\n", stdout
                    assert stderr == b"", stderr
                    assert _kill_survivors(identities) == [], (
                        "terminal sandbox survived launcher completion"
                    )
                except BaseException as error:
                    if isinstance(error, subprocess.TimeoutExpired):
                        stdout = error.output or b""
                        stderr = error.stderr or b""
                    # Capture currently available diagnostics without waiting
                    # for a hung runner or an inherited pipe to reach EOF.
                    with selectors.DefaultSelector() as selector:
                        for stream, name in (
                            (process.stdout, "stdout"),
                            (process.stderr, "stderr"),
                        ):
                            if not stream.closed:
                                selector.register(stream, selectors.EVENT_READ, name)
                        for key, _ in selector.select(0):
                            chunk = os.read(key.fd, 4096)
                            if key.data == "stdout":
                                stdout += chunk
                            else:
                                stderr += chunk
                    error.add_note(
                        f"terminal-loss phase: {phase}; returncode: {process.poll()}\n"
                        f"stdout: {stdout!r}\nstderr: {stderr!r}"
                    )
                    raise
                finally:
                    try:
                        if process.poll() is None:
                            # Ask the native owner to retire its target before
                            # forcing termination of captured identities.
                            process.terminate()
                            try:
                                process.wait(timeout=TIMEOUT)
                            except subprocess.TimeoutExpired:
                                _kill_survivors(identities)
                                process.kill()
                                process.wait(timeout=TIMEOUT)
                    finally:
                        _kill_survivors(identities)
        finally:
            release.close()

    return [
        {
            "command": _command(
                "sandbox", "--", "python", "-c", sandboxed_script, "<release gate>"
            ),
            "terminal_foreground_group": "target",
            "terminal_loss": "revoke slave, close master, then release FIFO",
            "stdout": stdout.decode("utf-8"),
            "exit_code": process.returncode,
            "stderr": stderr.decode("utf-8"),
        }
    ]


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_preserves_terminal_ownership_with_foreground_peer(binary: Path) -> Transcript:
    # A foreground shell pipeline places all of its stages in one process group.
    # Model another stage with a sibling that remains in the launcher's group.
    # fmt: python
    wrapper_script = code(r"""
        import os
        import sys

        peer = os.fork()
        if peer == 0:
            os.closerange(0, 3)
            os.execl("/bin/sleep", "sleep", "60")
        os.execv(
            sys.argv[1],
            [
                sys.argv[1],
                "sandbox",
                "--",
                "python",
                "-c",
                sys.argv[2],
                sys.argv[3],
                str(peer),
            ],
        )
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import os
        import sys

        print(
            f"ready {os.getpid()} {os.getpgrp()} {sys.argv[2]}",
            flush=True,
        )
        with open(sys.argv[1], encoding="utf-8") as release:
            assert release.readline() == "release\n"
        """)

    with tempfile.TemporaryDirectory() as directory:
        release = os.path.join(directory, "release")
        os.mkfifo(release)
        process, master, _ = _start_with_controlling_terminal(
            [sys.executable, "-c", wrapper_script, binary, sandboxed_script, release]
        )

        identities: list[_ProcessIdentity] = []
        release_descriptor = None
        try:
            readiness = _read_lines(
                process.stdout,
                1,
                "the foreground-peer sandbox readiness",
            )[0].split()
            assert len(readiness) == 4 and readiness[0] == "ready", readiness
            target_pid, target_group, peer_pid = map(int, readiness[1:])
            foreground_group = os.tcgetpgrp(master)
            identities.append(_capture_identity(target_pid))
            peer_identity = _capture_identity(peer_pid)
            identities.append(peer_identity)

            assert os.getpgid(process.pid) == process.pid
            assert os.getpgid(peer_pid) == process.pid
            root = _capture_identity(_sandbox_root_pid(process.pid))
            identities.insert(0, root)
            assert target_group == root[0]
            assert target_group == target_pid
            assert foreground_group == process.pid
            assert target_group != foreground_group

            assert _kill_survivors([peer_identity]) == [peer_pid]
            identities.pop()
            release_descriptor = os.open(release, os.O_RDWR | os.O_NONBLOCK)
            os.write(release_descriptor, b"release\n")
            returncode = process.wait(timeout=TIMEOUT)
            stderr = process.stderr.read().decode("utf-8")
            survivors = _kill_survivors(identities)

            assert returncode == 0, returncode
            assert stderr == "", stderr
            assert survivors == [], f"foreground-peer sandbox survived: {survivors}"
        finally:
            if release_descriptor is not None:
                os.close(release_descriptor)
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            _kill_survivors(identities)
            process.stdout.close()
            process.stderr.close()
            os.close(master)

    return [
        {
            "command": _command(
                "sandbox",
                "--",
                "python",
                "-c",
                sandboxed_script,
                "<release gate>",
                "<peer pid>",
            ),
            "foreground_peer": "shares launcher process group",
            "target_process_group": "dedicated",
            "terminal_foreground_group": "launcher and peer",
            "exit_code": returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
