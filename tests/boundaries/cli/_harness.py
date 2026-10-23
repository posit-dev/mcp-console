from __future__ import annotations

import os
import select
import selectors
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

if os.name == "posix":
    import fcntl
    import pty
    import termios


sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.capture import read_lines as _read_lines
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    kill_darwin_processes,
    live_darwin_processes,
)
from support.normalization import code
from support.sandbox_observation import (
    RunnerObservations,
    build_runner_observation_interposer,
)

TIMEOUT = 10


@dataclass
class _SandboxLifetime:
    process: subprocess.Popen[bytes]
    arguments: tuple[str, ...]
    launcher: DarwinProcessIdentity
    root: DarwinProcessIdentity
    target: DarwinProcessIdentity
    descendant: DarwinProcessIdentity
    manager: DarwinProcessIdentity
    temporary_directory: Path
    observations: RunnerObservations
    observation_directory: tempfile.TemporaryDirectory


def _watch_process_exits(
    identities: tuple[DarwinProcessIdentity, ...],
) -> tuple[select.kqueue, list[select.kevent]]:
    exit_events = select.kqueue()
    watches = [
        select.kevent(
            identity[0],
            filter=select.KQ_FILTER_PROC,
            flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
            fflags=select.KQ_NOTE_EXIT,
        )
        for identity in dict.fromkeys(identities)
    ]
    assert exit_events.control(watches, 0, 0) == []
    return exit_events, watches


def _assert_launcher_cleanup_barrier(
    exit_events: select.kqueue,
    watches: list[select.kevent],
    launcher: DarwinProcessIdentity,
    cleanup: tuple[DarwinProcessIdentity, ...],
    temporary_directory: Path,
    action: str,
) -> set[int]:
    observed_exits = set()
    cleanup_processes = {identity[0] for identity in cleanup} - {launcher[0]}
    for _ in watches:
        events = exit_events.control(None, 1, TIMEOUT)
        assert len(events) == 1, "owned sandbox lifetime did not exit"
        event = events[0]
        assert event.filter == select.KQ_FILTER_PROC, event
        assert event.fflags & select.KQ_NOTE_EXIT, event
        assert event.ident not in observed_exits, event
        if event.ident == launcher[0]:
            assert observed_exits == cleanup_processes, (
                f"owned sandbox launcher exited before {action} cleanup completed: "
                f"{cleanup_processes - observed_exits}"
            )
            assert not temporary_directory.exists(), (
                f"owned sandbox launcher exited before {action} directory cleanup"
            )
        observed_exits.add(event.ident)
    return observed_exits


def _command(*arguments: str) -> list[str]:
    return ["mcp-console", *arguments]


def _start_with_controlling_terminal(
    arguments: list[str | Path],
    environment: dict[str, str] | None = None,
) -> tuple[subprocess.Popen[bytes], int, str]:
    master, slave = pty.openpty()
    try:
        slave_name = os.ttyname(slave)

        def attach_controlling_terminal() -> None:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.tcsetpgrp(slave, os.getpid())

        process = subprocess.Popen(
            arguments,
            env=environment,
            stdin=slave,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            preexec_fn=attach_controlling_terminal,
        )
    except BaseException:
        os.close(master)
        raise
    finally:
        os.close(slave)
    assert process.stdout is not None
    assert process.stderr is not None
    return process, master, slave_name


def _sandbox_root_pid(launcher_pid: int) -> int:
    # The frontend execs the supervisor. Its native child leads a separate
    # group and execs the target in the same PID; an inherited terminal peer
    # remains in the caller group.
    roots = [
        identity
        for identity in darwin_child_process_identities(
            capture_darwin_process_identity(launcher_pid)
        )
        if os.getpgid(identity[0]) == identity[0]
    ]
    (root,) = roots
    return root[0]


def _manager_pid(launcher_pid: int) -> int:
    # Historical transcript labels call the cleanup owner the manager.
    # That role is now the runner at the original frontend PID.
    return launcher_pid


def _start_lifetime(
    binary: Path,
    exit_with_parent: int | None = None,
    ignore_sigterm: bool = False,
) -> _SandboxLifetime:
    # The detached child leaves the root's session, so cleanup must come from
    # exact descendant observation rather than an inherited process group.
    # fmt: python
    script = code(r"""
        import os
        import subprocess
        import sys

        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"])
        sys.stdout.flush()
        if sys.stdin.readline() == "exit\n":
            raise SystemExit(23)
        raise SystemExit(24)
        """)
    arguments = ["sandbox"]
    if exit_with_parent is not None:
        arguments.extend(("--exit-with-parent", str(exit_with_parent)))
    arguments.extend(("--", "python", "-c", script))
    recorded_arguments = tuple(arguments)
    launch_arguments: list[str | Path] = [binary, *recorded_arguments]
    if ignore_sigterm:
        # fmt: python
        host_script = code(r"""
            import os
            import signal
            import sys

            signal.signal(signal.SIGTERM, signal.SIG_IGN)
            os.execv(sys.argv[1], sys.argv[1:])
            """)
        launch_arguments = [
            sys.executable,
            "-c",
            host_script,
            binary,
            *recorded_arguments,
        ]
    observation_directory = tempfile.TemporaryDirectory()
    observation_path = Path(observation_directory.name)
    observations = RunnerObservations(observation_path / "runner-observations")
    environment = os.environ.copy()
    environment["MCP_CONSOLE_TEST_MANAGER_OBSERVATIONS"] = str(observations.path)
    environment["DYLD_INSERT_LIBRARIES"] = str(
        build_runner_observation_interposer(observation_path)
    )
    process = subprocess.Popen(
        launch_arguments,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=environment,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None

    identities: list[DarwinProcessIdentity] = []
    temporary_directory: Path | None = None
    try:
        root_pid, descendant_pid, temporary_directory_text = _read_lines(
            process.stdout,
            3,
            "the sandbox root, descendant, and temporary directory",
        )
        temporary_directory = Path(temporary_directory_text)
        target = capture_darwin_process_identity(int(root_pid))
        identities.append(target)
        root = capture_darwin_process_identity(_sandbox_root_pid(process.pid))
        identities.append(root)
        assert root == target
        assert os.getpgid(target[0]) == root[0]
        descendant = capture_darwin_process_identity(int(descendant_pid))
        identities.append(descendant)
        launcher = capture_darwin_process_identity(process.pid)
        assert os.getsid(descendant[0]) != os.getsid(root[0]), (
            "sandbox descendant did not leave the root session"
        )
        manager = capture_darwin_process_identity(_manager_pid(process.pid))
        identities.append(manager)
        # This case requires retirement of an observed detached descendant.
        # Keep its parent alive until the runner has registered that identity;
        # otherwise immediate root exit can orphan it before discovery.
        observations.wait_for(descendant[0], process)
        lifetime = _SandboxLifetime(
            process=process,
            arguments=recorded_arguments,
            launcher=launcher,
            root=root,
            target=target,
            descendant=descendant,
            manager=manager,
            temporary_directory=temporary_directory,
            observations=observations,
            observation_directory=observation_directory,
        )
        return lifetime
    except BaseException as error:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=TIMEOUT)
        with selectors.DefaultSelector() as selector:
            selector.register(process.stderr.fileno(), selectors.EVENT_READ)
            stderr_ready = selector.select(0)
        stderr = (
            os.read(process.stderr.fileno(), 4096).decode("utf-8", errors="replace")
            if stderr_ready
            else ""
        )
        error.add_note(
            f"sandbox returncode after setup failure: {process.returncode}\n"
            f"sandbox stderr:\n{stderr}"
        )
        kill_darwin_processes(identities)
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory.parent, ignore_errors=True)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()
        observations.close()
        observation_directory.cleanup()
        raise


def _wait_for_cleanup(lifetime: _SandboxLifetime, timeout: float = 5) -> list[int]:
    identities = (lifetime.root, lifetime.target, lifetime.descendant, lifetime.manager)
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while (
        survivors or lifetime.temporary_directory.exists()
    ) and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    return live_darwin_processes(identities)


def _wait_for_process_exit(
    identities: tuple[DarwinProcessIdentity, ...],
    description: str,
    timeout: float = 5,
) -> list[int]:
    deadline = time.monotonic() + timeout
    survivors = live_darwin_processes(identities)
    while survivors and time.monotonic() < deadline:
        time.sleep(0.01)
        survivors = live_darwin_processes(identities)
    assert survivors == [], f"{description}: {survivors}"
    return survivors


def _cleanup(lifetime: _SandboxLifetime) -> None:
    if lifetime.process.poll() is None:
        lifetime.process.kill()
        lifetime.process.wait(timeout=TIMEOUT)
    identities = (lifetime.root, lifetime.target, lifetime.descendant, lifetime.manager)
    kill_darwin_processes(identities)
    _wait_for_process_exit(identities, "sandbox cleanup did not stop all processes")
    shutil.rmtree(lifetime.temporary_directory.parent, ignore_errors=True)
    for stream in (
        lifetime.process.stdin,
        lifetime.process.stdout,
        lifetime.process.stderr,
    ):
        if not stream.closed:
            stream.close()
    lifetime.observations.close()
    lifetime.observation_directory.cleanup()


def _command_record(lifetime: _SandboxLifetime) -> dict[str, object]:
    return {
        "command": _command(*lifetime.arguments),
        "stdout": """<sandbox root pid>
<detached descendant pid>
<sandbox temp>
""",
    }
