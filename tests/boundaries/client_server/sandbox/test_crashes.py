#!/usr/bin/env -S uv run --script

import os
import re
import select
import shutil
import signal
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.macos import (
    DarwinProcessIdentity,
    capture_darwin_process_identity,
    darwin_child_process_identities,
    kill_darwin_processes,
    live_darwin_processes,
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
from support.sandbox_observation import observed_sandbox_descendants
from support.suites import run_this_suite

TIMEOUT = 10
# Python's select module omits Darwin's deprecated process-reaping flag.
_KQ_NOTE_REAP = 0x10000000
Generation = tuple[
    DarwinProcessIdentity,
    DarwinProcessIdentity,
    DarwinProcessIdentity,
    DarwinProcessIdentity,
    Path,
]


def _last_text(client: McpClient) -> str:
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    content = result["content"]
    assert len(content) == 1 and content[0]["type"] == "text", content
    return content[0]["text"]


def _spawn_detached_generation(client: McpClient) -> Generation:
    # Use the bundled Python runtime and standard library so crash supervision
    # does not depend on an externally resolved R package. Starting a new
    # session proves that observation is not limited to the relay's group.
    # fmt: python
    python = code(r"""
        import os
        import subprocess

        crash_child = subprocess.Popen(
            ["/bin/sleep", "60"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        print(f"worker={os.getpid()}")
        print(f"relay={os.getppid()}")
        print(f"child={crash_child.pid}")
        print(f"temp={os.environ['TMPDIR']}")
        """)
    client.send(python=python)

    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    assert os.getsid(child_pid) != os.getsid(worker_pid), (
        "detached child did not leave the worker session"
    )
    temporary_directory = Path(match.group(4))
    normalized = (
        "worker=<worker pid>\n"
        "relay=<relay pid>\n"
        "child=<detached child pid>\n"
        "temp=<sandbox temp>\n"
    )
    result["content"][0]["text"] = (
        text[: match.start()] + normalized + text[match.end() :]
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "process_ids": "omitted",
        "sandbox_temporary_directory": "omitted",
    }
    relay_identity = capture_darwin_process_identity(relay_pid)
    server_identity = capture_darwin_process_identity(client.process.pid)
    runners = [
        child
        for child in darwin_child_process_identities(server_identity)
        if relay_identity in darwin_child_process_identities(child)
    ]
    assert len(runners) == 1, runners
    runner_identity = runners[0]
    assert darwin_child_process_identities(runner_identity) == (relay_identity,)
    worker_identity = capture_darwin_process_identity(worker_pid)
    child_identity = capture_darwin_process_identity(child_pid)
    return (
        runner_identity,
        relay_identity,
        worker_identity,
        child_identity,
        temporary_directory,
    )


def _wait_for_process_reaping(
    process_events: "select.kqueue",
    identities: tuple[DarwinProcessIdentity, ...],
    timeout: float,
) -> None:
    watched = {identity[0] for identity in identities}
    pending = watched.copy()
    deadline = time.monotonic() + timeout
    while pending:
        remaining = deadline - time.monotonic()
        assert remaining > 0, f"processes were not reaped: {sorted(pending)}"
        events = process_events.control(None, len(watched), remaining)
        assert events, (
            f"processes were not reaped: {sorted(pending)}; "
            f"runner/relay/worker/child identities: {identities}; "
            f"remaining identities: {live_darwin_processes(identities)}"
        )
        for event in events:
            assert event.ident in watched, event
            assert event.filter == select.KQ_FILTER_PROC, event
            if event.fflags & _KQ_NOTE_REAP:
                pending.remove(event.ident)


def _wait_for_generation_failure(client: McpClient) -> None:
    deadline = time.monotonic() + 5
    poll_start = len(client.transcript)
    while True:
        result = client.send(session="default")
        if result.get("isError") is True:
            assert result["content"][0]["text"] == (
                "[worker relay stdout closed before retirement completed; "
                "additionally failed to stop the worker: "
                "worker launcher terminated by signal 9]"
            ), result
            final_poll = client.transcript[-1]
            client.transcript[poll_start:] = [final_poll]
            return
        assert time.monotonic() < deadline, (
            "server did not retire the failed generation"
        )
        time.sleep(0.01)


def _close_client_streams(client: McpClient) -> None:
    for stream in (client.stdin, client.stdout, client.stderr):
        try:
            stream.close()
        except BrokenPipeError:
            pass


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_server_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # The owned launcher must treat loss of the server as retirement of the
    # entire worker generation. A detached child must not survive merely because
    # the server received an uncatchable signal before its normal shutdown path.
    environment = os.environ.copy()
    with (
        tempfile.TemporaryDirectory() as directory,
        observed_sandbox_descendants(Path(directory), environment) as observe,
    ):
        client = McpClient(binary, SANDBOXED.serve(), environment)
        generation: Generation | None = None
        manager_identity: DarwinProcessIdentity | None = None
        manager_exit = select.kqueue()
        generation_reaping = select.kqueue()
        try:
            client.initialize_and_list_tools()
            generation = _spawn_detached_generation(client)
            # A ready worker does not prove the runner has observed a detached child.
            observe(generation[3][0], client.process)
            manager_identity = generation[0]
            exit_watch = select.kevent(
                manager_identity[0],
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            assert manager_exit.control([exit_watch], 0, 0) == []
            reap_watches = [
                select.kevent(
                    identity[0],
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT | _KQ_NOTE_REAP,
                )
                for identity in generation[:4]
            ]
            assert generation_reaping.control(reap_watches, 0, 0) == []
            assert live_darwin_processes(generation[:4]) == [
                identity[0] for identity in generation[:4]
            ], "worker generation changed while registering reap watches"

            client.process.kill()
            returncode = client.process.wait(timeout=TIMEOUT)
            events = manager_exit.control(None, 1, TIMEOUT)
            assert len(events) == 1, "sandbox manager did not exit after server crash"
            event = events[0]
            assert event.ident == manager_identity[0], event
            assert event.filter == select.KQ_FILTER_PROC, event
            assert event.fflags & select.KQ_NOTE_EXIT, event
            # The manager treats zombies as stopped, but their new parent may reap
            # them just after manager exit. The pre-registered process watches make
            # that final transition observable without racing a libproc sample.
            _wait_for_process_reaping(generation_reaping, generation[:4], TIMEOUT)

            assert returncode == -signal.SIGKILL, returncode
            assert not generation[4].exists(), (
                f"worker temporary directory survived server crash: {generation[4]}"
            )
            client.transcript.append(
                {
                    "server_signal": "SIGKILL",
                    "server_returncode": returncode,
                }
            )
            return client.transcript
        finally:
            stop_client(client)
            if generation is not None:
                kill_darwin_processes(generation[:4])
                shutil.rmtree(generation[4].parent, ignore_errors=True)
            if manager_identity is not None:
                kill_darwin_processes((manager_identity,))
            _close_client_streams(client)
            manager_exit.close()
            generation_reaping.close()


@requires(MACOS_SANDBOX, PROCESS_EVENTS)
def test_manager_crash_retires_the_worker_generation(binary: Path) -> Transcript:
    # Losing the runner must still fail the logical generation and permit a
    # replacement. Native cleanup after supervisor death is not guaranteed.
    client = McpClient(binary, SANDBOXED.serve())
    generation: Generation | None = None
    manager_identity: DarwinProcessIdentity | None = None
    try:
        client.initialize_and_list_tools()
        generation = _spawn_detached_generation(client)
        manager_identity = generation[0]

        assert signal_darwin_process(manager_identity, signal.SIGKILL), (
            "manager exited before crash injection"
        )
        client.transcript.append({"manager_signal": "SIGKILL"})
        _wait_for_generation_failure(client)
        client.send(r=code('writeLines("replacement ready")'))
        replacement = _last_text(client)
        assert replacement == "[starting new worker]\nreplacement ready\n", repr(
            replacement
        )
        assert generation[4].exists(), "failed supervisor cleanup removed worker temp"
        return client.transcript
    finally:
        stop_client(client)
        if generation is not None:
            kill_darwin_processes(generation[:4])
            shutil.rmtree(generation[4].parent, ignore_errors=True)
        if manager_identity is not None:
            kill_darwin_processes((manager_identity,))
        _close_client_streams(client)


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_worker_failure_retires_generation_without_duplicate_error(
    binary: Path,
) -> Transcript:
    environment = os.environ.copy()
    with (
        tempfile.TemporaryDirectory() as directory,
        observed_sandbox_descendants(Path(directory), environment) as observe,
    ):
        client = McpClient(binary, SANDBOXED.serve(), environment)
        generation: Generation | None = None
        generation_reaping = select.kqueue()
        try:
            client.initialize_and_list_tools()
            generation = _spawn_detached_generation(client)
            observe(generation[3][0], client.process)
            reap_watches = [
                select.kevent(
                    identity[0],
                    filter=select.KQ_FILTER_PROC,
                    flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                    fflags=select.KQ_NOTE_EXIT | _KQ_NOTE_REAP,
                )
                for identity in generation[:4]
            ]
            assert generation_reaping.control(reap_watches, 0, 0) == []
            assert live_darwin_processes(generation[:4]) == [
                identity[0] for identity in generation[:4]
            ], "worker generation changed while registering reap watches"

            result = client.send(python="os._exit(47)")
            assert result["isError"] is True, result
            text = result["content"][0]["text"]
            assert text.count("[worker exited with status 47]") == 1, text
            assert "worker launcher" not in text, text
            _wait_for_process_reaping(generation_reaping, generation[:4], TIMEOUT)
            assert not generation[4].exists(), (
                f"worker temporary directory survived workload failure: {generation[4]}"
            )

            client.send(python="print('replacement ready')")
            assert _last_text(client) == "replacement ready\n"
            transcript, stderr = client.finish_with_standard_error()
            assert not stderr, stderr
            return transcript
        finally:
            stop_client(client)
            if generation is not None:
                kill_darwin_processes(generation[:4])
                shutil.rmtree(generation[4].parent, ignore_errors=True)
            _close_client_streams(client)
            generation_reaping.close()


if __name__ == "__main__":
    run_this_suite(__file__)
