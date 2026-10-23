import json
import os
import re
import signal
import subprocess
import sys
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from support.checkpoints import FifoCheckpoint
from support.client import TextReader
from support.events import Events
from support.records import Transcript
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code
from support.requirements import (
    LINUX_SANDBOX,
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite

ROOT = Path(__file__).resolve().parents[3]


@contextmanager
def preparation(
    binary: Path, root: Path, environment: dict[str, str]
) -> Iterator[tuple]:
    process = subprocess.Popen(
        [binary, "resolve"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
        cwd=root,
        env={
            **os.environ,
            "MCP_CONSOLE_HOME": str(root / "home"),
            "RETICULATE_PYTHON": "managed",
            "RETICULATE_UV": str(root / "uv"),
            **environment,
        },
    )
    assert process.stdin is not None and process.stdout is not None
    reader = TextReader(process.stdout)

    def send(message: object) -> None:
        process.stdin.write(json.dumps(message) + "\n")
        process.stdin.flush()

    def receive(description: str) -> object:
        try:
            line = reader.readline(timeout=10)
            assert line, (description, process.wait(timeout=10), process.stderr.read())
            return json.loads(line)
        except TimeoutError:
            raise AssertionError(f"timed out waiting for {description}") from None

    try:
        send({"Open": {"mode": "PythonOnly"}})
        assert receive("hello") == "Hello"
        discovery = receive("discovery")["Completed"]
        assert discovery["confirmed"] and "Ok" in discovery["result"], discovery
        yield process, send, receive
    finally:
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=10)
        reader.close()


@requires(MACOS_SANDBOX, NATIVE_FIXTURES)
def test_exit_readiness_waits_for_terminal_status_before_reaping(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        gates = {
            name: FifoCheckpoint.create(root / name)
            for name in (
                "entered",
                "release",
                "registered",
                "exit",
                "pending",
                "status",
                "killed",
            )
        }
        uv = root / "uv"
        uv.write_text(
            f"#!{sys.executable}\n"
            "import json\n"
            f"with open({str(root / 'exit')!r}, 'rb') as gate:\n"
            "    assert gate.read(1) == b'1'\n"
            "print(json.dumps([{\n"
            '    "version": "3.12.7",\n'
            '    "version_parts": {"major": 3, "minor": 12, "patch": 7},\n'
            '    "symlink": None, "variant": "default", "implementation": "cpython",\n'
            "}]))\n"
        )
        uv.chmod(0o755)
        environment = {
            "PATH": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "child_exit_observation")),
            "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
            "MCP_CONSOLE_TEST_OBSERVER_DELAY_STATUS": "1",
            "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
            "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
            "MCP_CONSOLE_TEST_EXIT_REGISTERED": str(root / "registered"),
            "MCP_CONSOLE_TEST_STATUS_PENDING": str(root / "pending"),
            "MCP_CONSOLE_TEST_STATUS_RELEASE": str(root / "status"),
            "MCP_CONSOLE_TEST_OBSERVER_PID": str(root / "resolver-pid"),
            "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
            "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
        }
        try:
            with preparation(binary, root, environment) as (process, send, receive):
                send(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {"PythonVersion": {"constraints": [">=3.12"]}},
                        }
                    }
                )
                gates["entered"].wait("initial live-child observation")
                gates["release"].release()
                gates["registered"].wait("native exit notification registered")
                gates["exit"].release()
                gates["pending"].wait("exit readiness precedes terminal status")
                assert not (root / "early-reap").exists()
                gates["status"].release()
                completed = receive("successful terminal confirmation")["Completed"]
                assert completed == {
                    "id": 1,
                    "result": {"Ok": "3.12.7"},
                    "control": None,
                    "confirmed": True,
                }, completed
                send("Close")
                assert receive("close") == "Closed"
                process.stdin.close()
                assert process.wait(timeout=10) == 0, process.stderr.read()
                assert process.stderr.read() == ""
                assert not (root / "early-reap").exists(), (
                    "resolver reaped before terminal status was confirmed"
                )
                return [{"completed": completed, "terminal_status_before_reap": True}]
        finally:
            if (root / "resolver-pid").exists():
                pid = int((root / "resolver-pid").read_text())
                with Events() as exits:
                    try:
                        exits.watch_process(pid)
                    except ProcessLookupError:
                        pass
                    else:
                        os.killpg(pid, signal.SIGKILL)
                        assert exits.wait(10) == {pid}
            for gate in gates.values():
                gate.close()


@requires(PROCESS_EVENTS)
def test_success_closes_inherited_output_before_next_operation(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "success")


@requires(LINUX_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_success_without_pidfd_open(binary: Path) -> Transcript:
    transcript = []
    for error in ("EPERM", "ENOSYS"):
        transcript.append({"pidfd_open": error})
        transcript.extend(inherited_output(binary, "success", pidfd_error=error))
    return transcript


@requires(LINUX_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_failed_stop_without_pidfd_open_cancels_live_observation(
    binary: Path,
) -> Transcript:
    return cleanup_failure(binary, observation_failed=False, pidfd_error="EPERM")


@requires(PROCESS_EVENTS)
def test_success_bounds_drain_from_continuously_writing_descendant(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "stream")


@requires(PROCESS_EVENTS)
def test_failure_preserves_diagnostic_and_closes_inherited_output(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "failed")


@requires(PROCESS_EVENTS)
def test_cancel_closes_inherited_output_before_preparation_close(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "cancel")


@requires(PROCESS_EVENTS)
def test_interrupt_keeps_cause_after_nonzero_resolver_exit(binary: Path) -> Transcript:
    return interrupted_failure(binary)


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_exit_observer_preserves_interrupt_stop_notification(
    binary: Path,
) -> Transcript:
    return interrupted_failure(binary, stop_overlap=True)


def interrupted_failure(binary: Path, *, stop_overlap: bool = False) -> Transcript:
    with (
        TemporaryDirectory() as temporary,
        closing(FifoCheckpoint.create(Path(temporary) / "started")) as started,
        ExitStack() as checkpoints,
    ):
        root = Path(temporary)
        gates = (
            {
                name: checkpoints.enter_context(
                    closing(FifoCheckpoint.create(root / name))
                )
                for name in (
                    "entered",
                    "release",
                    "signal-entered",
                    "signal-release",
                    "processed",
                    "killed",
                )
            }
            if stop_overlap
            else {}
        )
        uv = root / "uv"
        uv.write_text(
            f"#!{sys.executable}\n"
            # fmt: python
            + code(f"""
                import signal
                import sys

                signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGINT}})
                with open({str(started.path)!r}, "wb", buffering=0) as checkpoint:
                    assert checkpoint.write(b"1") == 1
                assert signal.sigwait({{signal.SIGINT}}) == signal.SIGINT
                print("fixture resolver interrupted", file=sys.stderr, flush=True)
                raise SystemExit(23)
                """),
        )
        uv.chmod(0o755)
        environment = {"PATH": str(root)}
        if stop_overlap:
            environment.update(
                {
                    LOADER_VARIABLE: str(
                        build_interposer(root, "child_exit_observation")
                    ),
                    "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
                    "MCP_CONSOLE_TEST_STOP_OVERLAP": "1",
                    "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
                    "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
                    "MCP_CONSOLE_TEST_SIGNAL_ENTERED": str(root / "signal-entered"),
                    "MCP_CONSOLE_TEST_SIGNAL_RELEASE": str(root / "signal-release"),
                    "MCP_CONSOLE_TEST_STOP_PROCESSED": str(root / "processed"),
                    "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
                    "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
                }
            )
        with preparation(binary, root, environment) as (process, send, receive):
            send(
                {
                    "Run": {
                        "id": 1,
                        "operation": {"PythonVersion": {"constraints": []}},
                    }
                }
            )
            started.wait("resolver is waiting for its interrupt")
            if stop_overlap:
                gates["entered"].wait("exit observer's initial probe")
            send({"Control": {"id": 1, "control": "Interrupted"}})
            if stop_overlap:
                gates["signal-entered"].wait(
                    "resolver stopped before observing suspension"
                )
                gates["release"].release()
                gates["processed"].wait("exit observer processed the stop notification")
                gates["signal-release"].release()
            assert receive("interrupt receipt") == {
                "Controlled": {"id": 1, "result": {"Ok": True}}
            }
            completed = receive("interrupted resolver completion")["Completed"]
            assert completed["confirmed"] is True, completed
            assert completed["control"] == "Interrupted", completed
            assert "fixture resolver interrupted" in completed["result"]["Err"], (
                completed
            )
            send("Close")
            assert receive("close") == "Closed"
            process.stdin.close()
            assert process.wait(timeout=10) == 0, process.stderr.read()
            assert process.stderr.read() == ""
            return [{"completed": completed}]


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_cancel_retires_backpressured_stdin_independently_of_output(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "stdin")


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_exit_during_observer_registration_keeps_accepted_interrupt(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "interrupt")


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_interrupt_after_failed_exit_preserves_independent_failure(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "interrupt-failed")


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_exit_between_liveness_probe_and_interrupt_preserves_failure(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "interrupt-race")


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_exit_during_registration_waits_for_terminal_status_before_reaping(
    binary: Path,
) -> Transcript:
    return inherited_output(binary, "registration")


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_cleanup_failure_preserves_original_observation_failure(
    binary: Path,
) -> Transcript:
    return cleanup_failure(binary, observation_failed=True)


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_failed_stop_cancels_live_exit_observation(binary: Path) -> Transcript:
    return cleanup_failure(binary, observation_failed=False)


@requires(PROCESS_EVENTS)
def test_stdin_failure_retires_live_materializer_and_preserves_diagnostic(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        ready = FifoCheckpoint.create(root / "ready")
        fail = FifoCheckpoint.create(root / "fail")
        uv = root / "uv"
        uv.write_text(
            f"#!{sys.executable}\n"
            + (ROOT / "tests/fixtures/resolver_output_holder.py").read_text()
        )
        uv.chmod(0o755)
        retired = False
        try:
            environment = {
                "PATH": str(root),
                "TEST_RESOLVER_ROOT": str(root),
                "TEST_RESOLVER_MODE": "input-failure",
            }
            with preparation(binary, root, environment) as (process, send, receive):
                send(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {
                                "DuckdbPython": {
                                    "python": {
                                        "python": str(uv),
                                        "requirements": {"packages": []},
                                    },
                                    "extensions": ["x" * 512_000],
                                    "extension_directory": str(root),
                                }
                            },
                        }
                    }
                )
                ready.wait("materializer owns stdin and queued its diagnostic")
                leader = int((root / "leader").read_text())
                with Events() as exits:
                    exits.watch_process(leader)
                    fail.release()
                    completed = receive("I/O failure retires live materializer")[
                        "Completed"
                    ]
                    assert exits.wait(10) == {leader}
                    retired = True
                assert (
                    completed["confirmed"] is True and completed["control"] is None
                ), completed
                assert completed["result"] == {
                    "Err": "failed to write resolver stdin: Broken pipe (os error 32): fixture closed stdin before replying"
                }, completed
                send("Close")
                assert receive("close") == "Closed"
                process.stdin.close()
                assert process.wait(timeout=10) == 0, process.stderr.read()
                assert process.stderr.read() == ""
                return [{"completed": completed, "materializer_retired": True}]
        finally:
            if not retired and (root / "leader").exists():
                try:
                    os.killpg(int((root / "leader").read_text()), signal.SIGKILL)
                except ProcessLookupError:
                    pass
            ready.close()
            fail.close()


def deny_pidfds(root: Path, environment: dict[str, str], error: str) -> None:
    library = str(build_interposer(root, "resolver_pidfd"))
    environment[LOADER_VARIABLE] = ":".join(
        filter(None, (environment.get(LOADER_VARIABLE), library))
    )
    environment["MCP_CONSOLE_TEST_PIDFD_ERRNO"] = error


def cleanup_failure(
    binary: Path, *, observation_failed: bool, pidfd_error: str | None = None
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        gates = {
            name: FifoCheckpoint.create(root / name)
            for name in ("entered", "release", "killed")
        }
        uv = root / "uv"
        uv.write_text(f"#!{sys.executable}\nimport signal\nsignal.pause()\n")
        uv.chmod(0o755)
        environment = {
            "PATH": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "child_exit_observation")),
            "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
            "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
            "MCP_CONSOLE_TEST_OBSERVER_PID": str(root / "leader"),
            "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
            "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
            "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
            "MCP_CONSOLE_TEST_CLEANUP_FAIL": "1",
        }
        if observation_failed:
            environment["MCP_CONSOLE_TEST_OBSERVER_FAIL"] = "1"
        if pidfd_error is not None:
            deny_pidfds(root, environment, pidfd_error)
        try:
            with preparation(binary, root, environment) as (process, send, receive):
                send(
                    {
                        "Run": {
                            "id": 1,
                            "operation": {"PythonVersion": {"constraints": []}},
                        }
                    }
                )
                gates["entered"].wait("resolver observation admitted")
                leader = int((root / "leader").read_text())
                gates["release"].release()
                if not observation_failed:
                    send({"Control": {"id": 1, "control": "Cancelled"}})
                    assert receive("cancel receipt") == {
                        "Controlled": {"id": 1, "result": {"Ok": True}}
                    }
                gates["killed"].wait("native process retirement rejected")
                completed = receive("failed cleanup completion")["Completed"]
                error = completed["result"]["Err"]
                if observation_failed:
                    assert (
                        "failed to wait for managed Python version resolver" in error
                    ), completed
                    assert "Input/output error" in error, completed
                else:
                    assert error.startswith(
                        "managed Python version resolution cancelled; "
                    ), completed
                assert "failed to stop managed Python version resolver" in error, (
                    completed
                )
                assert "Permission denied" in error, completed
                assert completed["confirmed"] is False, completed
                assert completed["control"] == (
                    None if observation_failed else "Cancelled"
                ), completed
                process.stdin.close()
                process.wait(timeout=10)
                stderr = process.stderr.read()
                assert (
                    process.returncode != 0
                    and "preparation retirement is unconfirmed" in stderr
                ), stderr
                os.kill(leader, 0)
                assert not (root / "early-reap").exists()
                completed["result"]["Err"] = re.sub(
                    r"child process \d+",
                    "child process PID",
                    error.replace(str(uv), "UV"),
                )
                return [{"completed": completed, "materializer_still_alive": True}]
        finally:
            if (root / "leader").exists():
                leader = int((root / "leader").read_text())
                with Events() as exits:
                    exits.watch_process(leader)
                    os.killpg(leader, signal.SIGKILL)
                    assert exits.wait(10) == {leader}
            for gate in gates.values():
                gate.close()


def inherited_output(
    binary: Path, mode: str, *, pidfd_error: str | None = None
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary)
        retired: set[str] = set()
        gates = {
            name: FifoCheckpoint.create(root / name)
            for name in (
                "ready",
                "exit",
                "probe",
                "closed",
                "blocked",
                "entered",
                "release",
                "killed",
                "streaming",
                "pending",
                "status",
                "signal-entered",
                "signal-release",
            )
        }
        uv = root / "uv"
        uv.write_text(
            f"#!{sys.executable}\n"
            + (ROOT / "tests/fixtures/resolver_output_holder.py").read_text()
        )
        uv.chmod(0o755)
        try:
            environment = {
                "PATH": str(root),
                "TEST_RESOLVER_ROOT": str(root),
                "TEST_RESOLVER_MODE": (
                    "failed" if mode in ("interrupt-failed", "interrupt-race") else mode
                ),
            }
            if mode == "stdin":
                environment.update(
                    {
                        LOADER_VARIABLE: str(build_interposer(root, "resolver_stdin")),
                        "MCP_CONSOLE_TEST_STDIN_BLOCKED": str(root / "blocked"),
                    }
                )
            elif (
                mode
                in ("interrupt", "interrupt-failed", "interrupt-race", "registration")
                or pidfd_error is not None
            ):
                environment.update(
                    {
                        LOADER_VARIABLE: str(
                            build_interposer(root, "child_exit_observation")
                        ),
                        "MCP_CONSOLE_TEST_OBSERVER_CANCELLABLE": "1",
                        "MCP_CONSOLE_TEST_OBSERVER_STALE_PROBE": "1",
                        "MCP_CONSOLE_TEST_OBSERVER_ENTERED": str(root / "entered"),
                        "MCP_CONSOLE_TEST_OBSERVER_RELEASE": str(root / "release"),
                        "MCP_CONSOLE_TEST_CHILD_KILLED": str(root / "killed"),
                        "MCP_CONSOLE_TEST_EARLY_REAP": str(root / "early-reap"),
                    }
                )
            if mode == "registration":
                environment.update(
                    {
                        "MCP_CONSOLE_TEST_OBSERVER_DELAY_STATUS": "1",
                        "MCP_CONSOLE_TEST_STATUS_PENDING": str(root / "pending"),
                        "MCP_CONSOLE_TEST_STATUS_RELEASE": str(root / "status"),
                    }
                )
            if mode == "interrupt-race":
                environment.update(
                    {
                        "MCP_CONSOLE_TEST_INTERRUPT_RACE": "1",
                        "MCP_CONSOLE_TEST_SIGNAL_ENTERED": str(root / "signal-entered"),
                        "MCP_CONSOLE_TEST_SIGNAL_RELEASE": str(root / "signal-release"),
                    }
                )
            if pidfd_error is not None:
                deny_pidfds(root, environment, pidfd_error)
            with preparation(binary, root, environment) as (process, send, receive):
                operation = {"PythonVersion": {"constraints": [">=3.12"]}}
                request = operation
                if mode == "stdin":
                    request = {
                        "DuckdbPython": {
                            "python": {
                                "python": str(uv),
                                "requirements": {"packages": []},
                            },
                            "extensions": ["x" * 512_000],
                            "extension_directory": str(root),
                        }
                    }
                send({"Run": {"id": 1, "operation": request}})
                gates["ready"].wait("escaped descendant owns both output descriptors")
                if mode == "stdin":
                    gates["blocked"].wait(
                        "materializer stdin writer reached actual backpressure"
                    )
                elif (
                    mode
                    in (
                        "interrupt",
                        "interrupt-failed",
                        "interrupt-race",
                        "registration",
                    )
                    or pidfd_error is not None
                ):
                    gates["entered"].wait("live-child exit probe held")
                    if pidfd_error is not None:
                        gates["release"].release()
                elif mode == "stream":
                    gates["streaming"].wait("descendant is continuously writing stderr")
                leader = int((root / "leader").read_text())
                if mode == "interrupt-race":
                    send({"Control": {"id": 1, "control": "Interrupted"}})
                    gates["signal-entered"].wait("control signal held before delivery")
                with Events() as exits:
                    exits.watch_process(leader)
                    if mode in ("cancel", "stdin"):
                        send({"Control": {"id": 1, "control": "Cancelled"}})
                        assert receive("control receipt") == {
                            "Controlled": {"id": 1, "result": {"Ok": True}}
                        }
                    else:
                        gates["exit"].release()
                    assert exits.wait(10) == {leader}
                    retired.add("leader")
                if mode in ("interrupt", "interrupt-failed", "interrupt-race"):
                    if mode == "interrupt-race":
                        gates["signal-release"].release()
                    else:
                        send({"Control": {"id": 1, "control": "Interrupted"}})
                    assert receive("accepted interrupt after leader exit") == {
                        "Controlled": {"id": 1, "result": {"Ok": True}}
                    }
                    gates["release"].release()
                elif mode == "registration":
                    # The child exited while the initial live-status probe was
                    # held. NOTE_EXIT registration now reports ESRCH, before
                    # the fixture permits terminal-status confirmation.
                    gates["release"].release()
                    gates["pending"].wait("registration exit precedes terminal status")
                    assert not (root / "early-reap").exists()
                    gates["status"].release()
                completed = receive("completion independent of inherited output EOF")[
                    "Completed"
                ]
                assert completed["confirmed"] is True, completed
                if mode in ("success", "stream", "registration"):
                    assert completed["result"] == {"Ok": "3.12.7"}, completed
                    assert completed["control"] is None, completed
                elif mode == "interrupt":
                    assert completed["result"] == {
                        "Err": "managed Python version resolution interrupted"
                    }, completed
                    assert completed["control"] == "Interrupted", completed
                elif mode in ("failed", "interrupt-failed", "interrupt-race"):
                    assert (
                        "fixture materialization failed before replying"
                        in completed["result"]["Err"]
                    ), completed
                    assert completed["control"] is None, completed
                else:
                    assert completed["control"] == "Cancelled", completed
                    kind = (
                        "DuckDB extension"
                        if mode == "stdin"
                        else "managed Python version"
                    )
                    assert completed["result"] == {
                        "Err": f"{kind} resolution cancelled"
                    }, completed
                # A returned response is insufficient: both pipe readers must
                # already be closed while this preparation owner is still live.
                holder = int((root / "holder").read_text())
                with Events() as exits:
                    exits.watch_process(holder)
                    gates["probe"].release()
                    gates["closed"].wait("stdout and stderr readers retired")
                    assert exits.wait(10) == {holder}
                    retired.add("holder")
                # The same owner remains usable after retiring this invocation.
                uv.write_text(
                    '#!/bin/sh\nprintf \'%s\\n\' \'[{"version":"3.12.7","version_parts":{"major":3,"minor":12,"patch":7},"symlink":null,"variant":"default","implementation":"cpython"}]\'\n'
                )
                send({"Run": {"id": 2, "operation": operation}})
                next_result = receive("subsequent preparation")["Completed"]
                assert next_result["confirmed"] and next_result["result"] == {
                    "Ok": "3.12.7"
                }, next_result
                send("Close")
                assert receive("close") == "Closed"
                process.stdin.close()
                process.wait(timeout=10)
                stderr = process.stderr.read()
                assert process.returncode == 0 and stderr == "", stderr
                assert not (root / "early-reap").exists()
                return [
                    {
                        "completed": completed,
                        "output_readers_closed": True,
                        "subsequent_version": "3.12.7",
                    }
                ]
        finally:
            for name in ("leader", "holder"):
                if name not in retired and (root / name).exists():
                    pid = int((root / name).read_text())
                    with Events() as exits:
                        try:
                            exits.watch_process(pid)
                        except ProcessLookupError:
                            pass
                        else:
                            os.kill(pid, signal.SIGKILL)
                            assert exits.wait(10) == {pid}, (
                                "fixture actor survived cleanup"
                            )
            for gate in gates.values():
                gate.close()


if __name__ == "__main__":
    run_this_suite(__file__)
