#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import signal
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import (
    TIMEOUT,
    _assert_launcher_cleanup_barrier,
    _manager_pid,
    _read_lines,
    _sandbox_root_pid,
    _wait_for_process_exit,
    _watch_process_exits,
)
from support.checkpoints import FifoCheckpoint
from support.macos import (
    capture_darwin_process_identity,
    kill_darwin_processes,
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite


def _build_startup_interposer(
    directory: Path,
    fixture_name: str,
    *compiler_flags: str,
) -> Path:
    source = directory / f"{fixture_name}.c"
    library = directory / f"{fixture_name}.dylib"
    fixture = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "native"
        / f"{fixture_name}.c"
    )
    shutil.copyfile(fixture, source)
    subprocess.run(
        [
            "cc",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Wpedantic",
            "-Werror",
            *compiler_flags,
            "-dynamiclib",
            "-I",
            fixture.parent,
            "-o",
            library,
            source,
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return library


def _start_owned_echo_owner(
    binary: Path,
    environment: dict[str, str],
) -> subprocess.Popen[bytes]:
    # fmt: python
    owner_script = code(r"""
        import os
        import subprocess
        import sys

        launcher = subprocess.Popen(
            [
                sys.argv[1],
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                "/bin/echo",
                "target ran",
            ],
            stdin=subprocess.DEVNULL,
        )
        print(launcher.pid, flush=True)
        raise SystemExit(launcher.wait())
        """)
    return subprocess.Popen(
        [sys.executable, "-c", owner_script, binary],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_owner_loss_before_exit_watch_cleans_startup(binary: Path) -> Transcript:
    # Gate runner parent capture before it can create storage or a native root.
    # Caller loss here must leave no target or private storage behind.
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        waiter_started = FifoCheckpoint.create(temporary / "waiter-started")
        waiter_release = FifoCheckpoint.create(temporary / "waiter-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_ROOT_WAITER_START"] = str(waiter_started.path)
        environment["MCP_CONSOLE_TEST_ROOT_WAITER_RELEASE"] = str(waiter_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_startup_interposer(
                temporary,
                "root_waiter_start_interposer",
                "-Wno-deprecated-declarations",
            )
        )
        owner = _start_owned_echo_owner(binary, environment)
        assert owner.stdout is not None
        assert owner.stderr is not None

        identities = []
        released = False
        exit_events = select.kqueue()
        try:
            (launcher_pid,) = _read_lines(
                owner.stdout,
                1,
                "the owned sandbox launcher",
            )
            waiter_started.wait("root waiter startup")
            launcher = capture_darwin_process_identity(int(launcher_pid))
            identities = [launcher]
            exit_events.close()
            exit_events, watches = _watch_process_exits((launcher,))
            private_directories = list(temporary.glob("sandbox-*"))
            assert private_directories == [], private_directories

            owner_identity = capture_darwin_process_identity(owner.pid)
            assert signal_darwin_process(owner_identity, signal.SIGKILL), (
                "sandbox owner exited before crash injection"
            )
            owner_returncode = owner.wait(timeout=TIMEOUT)
            waiter_release.release()
            released = True

            events = exit_events.control(None, 1, TIMEOUT)
            assert len(events) == 1 and events[0].ident == launcher[0], events
            assert events[0].fflags & select.KQ_NOTE_EXIT
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox startup survived owner loss",
            )
            target_stdout = owner.stdout.read().decode("utf-8")
            stderr = owner.stderr.read().decode("utf-8")

            assert owner_returncode == -signal.SIGKILL, owner_returncode
            assert stderr == (
                "mcp-console-sandbox: parent_pid is not the current parent\n"
            ), stderr
            assert target_stdout == "", target_stdout
            assert not list(temporary.glob("sandbox-*")), (
                "owned sandbox startup preserved its private directory"
            )
            return [
                {
                    "command": [
                        "mcp-console",
                        "sandbox",
                        "--exit-with-parent",
                        "<owner pid>",
                        "--",
                        "/bin/echo",
                        "target ran",
                    ],
                    "owner_signal": "SIGKILL before owner-watch registration",
                    "owner_returncode": owner_returncode,
                    "stderr": stderr,
                    "verified_target": "did not run",
                    "verified_cleanup": "sandbox root and private directory",
                }
            ]
        finally:
            if not released:
                waiter_release.release()
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if identities:
                _wait_for_process_exit(
                    tuple(identities),
                    "owned sandbox startup cleanup did not stop all processes",
                )
            exit_events.close()
            waiter_started.close()
            waiter_release.close()
            owner.stdout.close()
            owner.stderr.close()


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_owner_loss_before_target_release_cancels_startup(binary: Path) -> Transcript:
    # The native readiness checkpoint is reached only after the runner
    # has captured its caller identity. Hold readiness there, then remove the
    # owner so the final identity check must keep the target gated.
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        manager_started = FifoCheckpoint.create(temporary / "manager-started")
        manager_release = FifoCheckpoint.create(temporary / "manager-release")
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_TEST_MANAGER_START"] = str(manager_started.path)
        environment["MCP_CONSOLE_TEST_MANAGER_RELEASE"] = str(manager_release.path)
        environment["DYLD_INSERT_LIBRARIES"] = str(
            _build_startup_interposer(temporary, "manager_start_interposer")
        )
        owner = _start_owned_echo_owner(binary, environment)
        assert owner.stdout is not None
        assert owner.stderr is not None

        identities = []
        released = False
        exit_events = select.kqueue()
        try:
            (launcher_pid,) = _read_lines(
                owner.stdout,
                1,
                "the owned sandbox launcher",
            )
            manager_started.wait("manager startup")
            launcher = capture_darwin_process_identity(int(launcher_pid))
            root = capture_darwin_process_identity(_sandbox_root_pid(launcher[0]))
            manager = capture_darwin_process_identity(_manager_pid(launcher[0]))
            identities = [launcher, root, manager]
            exit_events.close()
            cleanup = (root, manager)
            exit_events, watches = _watch_process_exits((*cleanup, launcher))
            private_directories = list(temporary.glob("sandbox-*"))
            assert len(private_directories) == 1, private_directories

            owner_identity = capture_darwin_process_identity(owner.pid)
            assert signal_darwin_process(owner_identity, signal.SIGKILL), (
                "sandbox owner exited before crash injection"
            )
            owner_returncode = owner.wait(timeout=TIMEOUT)
            manager_release.release()
            released = True

            observed_exits = _assert_launcher_cleanup_barrier(
                exit_events,
                watches,
                launcher,
                cleanup,
                private_directories[0],
                "startup",
            )
            assert observed_exits == {identity[0] for identity in (*cleanup, launcher)}
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox startup survived owner loss",
            )
            target_stdout = owner.stdout.read().decode("utf-8")
            stderr = owner.stderr.read().decode("utf-8")

            assert owner_returncode == -signal.SIGKILL, owner_returncode
            assert stderr == "", stderr
            assert target_stdout == "", target_stdout
            assert not list(temporary.glob("sandbox-*")), (
                "owned sandbox startup preserved its private directory"
            )
            return [
                {
                    "command": [
                        "mcp-console",
                        "sandbox",
                        "--exit-with-parent",
                        "<owner pid>",
                        "--",
                        "/bin/echo",
                        "target ran",
                    ],
                    "owner_signal": "SIGKILL before target release",
                    "owner_returncode": owner_returncode,
                    "stderr": stderr,
                    "verified_target": "did not run",
                    "verified_cleanup": "sandbox root, manager, and private directory",
                }
            ]
        finally:
            if not released:
                manager_release.release()
            if owner.poll() is None:
                owner.kill()
                owner.wait(timeout=TIMEOUT)
            kill_darwin_processes(identities)
            if identities:
                _wait_for_process_exit(
                    tuple(identities),
                    "owned sandbox startup cleanup did not stop all processes",
                )
            exit_events.close()
            manager_started.close()
            manager_release.close()
            owner.stdout.close()
            owner.stderr.close()


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_sigterm_before_setup_retires_without_releasing_target(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        started = FifoCheckpoint.create(temporary / "manager-started")
        release = FifoCheckpoint.create(temporary / "manager-release")
        environment = os.environ | {
            "TMPDIR": directory,
            "LARGE_SETUP": "x" * (96 * 1024),
            "MCP_CONSOLE_TEST_MANAGER_START": str(started.path),
            "MCP_CONSOLE_TEST_MANAGER_RELEASE": str(release.path),
            "DYLD_INSERT_LIBRARIES": str(
                _build_startup_interposer(temporary, "manager_start_interposer")
            ),
        }
        process = subprocess.Popen(
            [
                binary,
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                "/bin/echo",
                "target ran",
            ],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        identities = []
        try:
            started.wait("manager startup before cancellation")
            launcher = capture_darwin_process_identity(process.pid)
            root = capture_darwin_process_identity(_sandbox_root_pid(process.pid))
            manager = capture_darwin_process_identity(_manager_pid(process.pid))
            identities = [launcher, root, manager]
            # A stopped reader and a frame larger than pipe capacity make an
            # incorrect release block. This exposes cancellation ordering without
            # racing target execution against the manager's retirement signals.
            assert signal_darwin_process(root, signal.SIGSTOP)
            assert signal_darwin_process(launcher, signal.SIGTERM)
            release.release()
            assert process.wait(timeout=TIMEOUT) == 0
            stdout, stderr = process.communicate()
            assert (stdout, stderr) == (b"", b""), (stdout, stderr)
            _wait_for_process_exit(tuple(identities), "cancelled startup survived")
            assert not list(temporary.glob("sandbox-*"))
        finally:
            release.release()
            # Killing the stopped reader also unblocks an incorrect pipe write.
            kill_darwin_processes(identities[1:])
            if process.poll() is None:
                process.kill()
                process.wait(timeout=TIMEOUT)
            for stream in (process.stdin, process.stdout, process.stderr):
                stream.close()
            started.close()
            release.close()
        return [
            {
                "scenario": "owned SIGTERM before setup with stopped runner and large frame",
                "stdout": "",
                "stderr": "",
                "exit_code": process.returncode,
                "verified_cleanup": "runner, manager, and private directory",
            }
        ]


@requires(MACOS_SANDBOX, PROCESS_EVENTS, NATIVE_FIXTURES)
def test_cancels_owned_launch_during_setup(binary: Path) -> Transcript:
    transcript = []
    for reader, cancellation in (
        ("stopped reader", "SIGTERM"),
        ("stopped reader", "owner exit"),
        ("writable pipe", "SIGTERM"),
        ("writable pipe", "owner exit"),
        ("writable pipe", None),
    ):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            checkpoints = {
                name: FifoCheckpoint.create(temporary / name)
                for name in (
                    "manager-started",
                    "manager-release",
                    "write-started",
                    "write-release",
                    "write-continued",
                )
            }
            environment = os.environ | {
                "TMPDIR": directory,
                "MCP_CONSOLE_TEST_MANAGER_START": str(
                    checkpoints["manager-started"].path
                ),
                "MCP_CONSOLE_TEST_MANAGER_RELEASE": str(
                    checkpoints["manager-release"].path
                ),
                "MCP_CONSOLE_TEST_SETUP_WRITE_STARTED": str(
                    checkpoints["write-started"].path
                ),
                "MCP_CONSOLE_TEST_SETUP_WRITE_RELEASE": str(
                    checkpoints["write-release"].path
                ),
                "DYLD_INSERT_LIBRARIES": str(
                    _build_startup_interposer(
                        temporary,
                        "setup_write_interposer",
                        "-I",
                        str(Path(__file__).resolve().parents[3] / "fixtures/native"),
                    )
                ),
            }
            if reader == "stopped reader":
                environment["LARGE_SETUP"] = "x" * (96 * 1024)
            else:
                environment["MCP_CONSOLE_TEST_SETUP_WRITE_CONTINUED"] = str(
                    checkpoints["write-continued"].path
                )
            owner = _start_owned_echo_owner(binary, environment)
            identities = []
            try:
                (launcher_pid,) = _read_lines(owner.stdout, 1, "owned launcher")
                checkpoints["manager-started"].wait("manager before setup")
                launcher = capture_darwin_process_identity(int(launcher_pid))
                root = capture_darwin_process_identity(_sandbox_root_pid(launcher[0]))
                manager = capture_darwin_process_identity(_manager_pid(launcher[0]))
                identities = [launcher, root, manager]
                if reader == "stopped reader":
                    assert signal_darwin_process(root, signal.SIGSTOP)
                checkpoints["manager-release"].release()
                checkpoints["write-started"].wait("first setup byte queued")
                if cancellation == "SIGTERM":
                    assert signal_darwin_process(launcher, signal.SIGTERM)
                elif cancellation == "owner exit":
                    owner.kill()
                    assert owner.wait(timeout=TIMEOUT) == -signal.SIGKILL
                checkpoints["write-release"].release()
                returncode = owner.wait(timeout=TIMEOUT)
                expected_returncode = (
                    -signal.SIGKILL if cancellation == "owner exit" else 0
                )
                if returncode != expected_returncode:
                    stdout, stderr = owner.communicate(timeout=TIMEOUT)
                    raise AssertionError(
                        (reader, cancellation, returncode, stdout, stderr)
                    )
                _wait_for_process_exit(
                    tuple(identities), "setup cancellation leaked a process"
                )
                if reader == "writable pipe":
                    readable, _, _ = select.select(
                        [checkpoints["write-continued"].descriptor], [], [], 0
                    )
                    assert bool(readable) == (cancellation is None), (
                        "setup writing continued after cancellation"
                    )
                stdout, stderr = owner.communicate()
                expected_stdout = b"target ran\n" if cancellation is None else b""
                assert (stdout, stderr) == (expected_stdout, b""), (stdout, stderr)
                assert not list(temporary.glob("sandbox-*"))
                transcript.append(
                    {
                        "scenario": (cancellation or "no cancellation")
                        + " after setup writing begins with a "
                        + reader,
                        "stdout": stdout.decode(),
                        "stderr": "",
                        "verified_cleanup": "launcher, runner, manager, and private directory",
                    }
                )
            finally:
                checkpoints["manager-release"].release()
                checkpoints["write-release"].release()
                kill_darwin_processes(identities)
                if owner.poll() is None:
                    owner.kill()
                    owner.wait(timeout=TIMEOUT)
                owner.stdout.close()
                owner.stderr.close()
                for checkpoint in checkpoints.values():
                    checkpoint.close()
    return transcript


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_owner_loss_retires_the_sandbox_lifetime(binary: Path) -> Transcript:
    # Keep the target behind its inherited stdin until the owner has reported
    # the launcher PID. The detached child then leaves the target's session, so
    # owner-loss cleanup must come from manager observation rather than a
    # process-group signal.
    # fmt: python
    target_script = code(r"""
        import os
        import subprocess
        import time

        assert input() == "start"
        child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(os.getpid())
        print(child.pid)
        print(os.environ["TMPDIR"], flush=True)
        time.sleep(60)
        """)
    # fmt: python
    owner_script = code(r"""
        import os
        import subprocess
        import sys

        launcher = subprocess.Popen(
            [
                sys.argv[1],
                "sandbox",
                "--exit-with-parent",
                str(os.getpid()),
                "--",
                "python",
                "-c",
                sys.argv[2],
            ],
            stdin=subprocess.PIPE,
        )
        assert launcher.stdin is not None
        print(launcher.pid, flush=True)
        launcher.stdin.write(b"start\n")
        launcher.stdin.close()
        raise SystemExit(launcher.wait())
        """)
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_script, binary, target_script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert owner.stdout is not None
    assert owner.stderr is not None

    identities = []
    exit_events = select.kqueue()
    temporary_directory: Path | None = None
    try:
        launcher_pid, root_pid, descendant_pid, temporary_directory_text = _read_lines(
            owner.stdout,
            4,
            "the sandbox launcher, root, descendant, and temporary directory",
        )
        owner_identity = capture_darwin_process_identity(owner.pid)
        launcher = capture_darwin_process_identity(int(launcher_pid))
        target = capture_darwin_process_identity(int(root_pid))
        root = capture_darwin_process_identity(_sandbox_root_pid(launcher[0]))
        descendant = capture_darwin_process_identity(int(descendant_pid))
        manager = capture_darwin_process_identity(_manager_pid(launcher[0]))
        identities = [launcher, root, target, descendant, manager]
        temporary_directory = Path(temporary_directory_text)
        assert os.getsid(descendant[0]) != os.getsid(root[0]), (
            "sandbox descendant did not leave the target session"
        )
        exit_events.close()
        cleanup = (root, target, descendant, manager)
        exit_events, watches = _watch_process_exits((*cleanup, launcher))

        assert signal_darwin_process(owner_identity, signal.SIGKILL), (
            "sandbox owner exited before crash injection"
        )
        returncode = owner.wait(timeout=TIMEOUT)

        observed_exits = _assert_launcher_cleanup_barrier(
            exit_events,
            watches,
            launcher,
            cleanup,
            temporary_directory,
            "owner-loss",
        )
        assert observed_exits == {identity[0] for identity in (*cleanup, launcher)}
        _wait_for_process_exit(
            tuple(identities),
            "owned sandbox lifetime remained after launcher exit",
        )
        stderr = owner.stderr.read().decode("utf-8")

        assert returncode == -signal.SIGKILL, returncode
        assert stderr == "", stderr
        assert not temporary_directory.exists(), (
            "owned sandbox launcher exited before removing its temporary directory"
        )
        return [
            {
                "command": [
                    "mcp-console",
                    "sandbox",
                    "--exit-with-parent",
                    "<owner pid>",
                    "--",
                    "python",
                    "-c",
                    target_script,
                ],
                "stdout": (
                    """<sandbox root pid>
<detached descendant pid>
<sandbox temp>
"""
                ),
            },
            {
                "owner_signal": "SIGKILL",
                "owner_returncode": returncode,
                "verified_cleanup_barrier": (
                    "launcher exited after sandbox root/target, detached descendant, "
                    "manager, and temp"
                ),
            },
        ]
    finally:
        if owner.poll() is None:
            owner.kill()
            owner.wait(timeout=TIMEOUT)
        kill_darwin_processes(identities)
        if identities:
            _wait_for_process_exit(
                tuple(identities),
                "owned sandbox cleanup did not stop all processes",
            )
        if temporary_directory is not None:
            shutil.rmtree(temporary_directory.parent, ignore_errors=True)
        exit_events.close()
        owner.stdout.close()
        owner.stderr.close()


if __name__ == "__main__":
    run_this_suite(__file__)
