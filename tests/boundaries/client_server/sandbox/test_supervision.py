#!/usr/bin/env -S uv run --script

import os
import re
import shutil
import signal
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text as _last_text
from support.assertions import wait_for_evaluation_output
from support.client import McpClient, stop_client
from support.execution import SANDBOXED
from support.macos import (
    DarwinProcessIdentity as _ProcessIdentity,
)
from support.macos import (
    capture_darwin_process_identity as _capture_identity,
)
from support.macos import (
    signal_darwin_process,
)
from support.normalization import code
from support.records import Transcript
from support.sandbox_observation import observed_sandbox_descendants
from support.requirements import (
    MACOS_SANDBOX,
    NATIVE_FIXTURES,
    PROCESS_EVENTS,
    requires,
)
from support.suites import run_this_suite

_Generation = tuple[_ProcessIdentity, _ProcessIdentity, _ProcessIdentity, Path]


def _normalize_generation(client: McpClient) -> _Generation:
    result = client.transcript[-1]["result"]
    text = _last_text(client)
    pattern = re.compile(r"(?m)^worker=(\d+)\nrelay=(\d+)\nchild=(\d+)\ntemp=(.+)\n$")
    match = pattern.search(text)
    assert match is not None, text
    worker_pid, relay_pid, child_pid = map(int, match.group(1, 2, 3))
    assert os.getsid(child_pid) != os.getsid(worker_pid), (
        "processx child did not leave the worker session"
    )
    temporary_directory = Path(match.group(4))
    normalized = (
        "worker=<worker pid>\n"
        "relay=<relay pid>\n"
        "child=<processx child pid>\n"
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
    return (
        _capture_identity(relay_pid),
        _capture_identity(worker_pid),
        _capture_identity(child_pid),
        temporary_directory,
    )


def _kill_if_alive(identity: _ProcessIdentity) -> bool:
    return signal_darwin_process(identity, signal.SIGKILL)


def _kill_generation(generation: _Generation) -> list[str]:
    relay, worker, child, _ = generation
    survivors = []
    for name, identity in (
        ("relay", relay),
        ("worker", worker),
        ("processx child", child),
    ):
        if _kill_if_alive(identity):
            survivors.append(name)
    return survivors


def _assert_generation_retired(generation: _Generation, action: str) -> None:
    survivors = _kill_generation(generation)
    assert survivors == [], f"worker generation survived {action}: {survivors}"
    temporary_directory = generation[3]
    assert not temporary_directory.exists(), (
        f"worker temporary directory survived {action}: {temporary_directory}"
    )


def _spawn_processx_generation(client: McpClient) -> _Generation:
    # processx calls setsid() for this child, so it leaves the relay and worker
    # process group while remaining a descendant of the worker generation.
    # fmt: r
    r = code(r"""
        sandbox_child <- processx::process$new(
          "/bin/sleep",
          "60",
          stdout = "|",
          stderr = "|",
          cleanup = FALSE
        )
        writeLines(c(
          sprintf("worker=%d", Sys.getpid()),
          sprintf("relay=%d", ps::ps_ppid()),
          sprintf("child=%d", sandbox_child$get_pid()),
          sprintf("temp=%s", Sys.getenv("TMPDIR"))
        ))
        """)
    wait_for_evaluation_output(
        client,
        None,
        "prepared processx generation",
        completion_timeout_seconds=client.response_timeout,
        r=r,
        requirements={"r": ["processx"]},
    )
    return _normalize_generation(client)


@contextmanager
def _observed_processx_generation(
    binary: Path,
) -> Iterator[tuple[McpClient, _Generation]]:
    with tempfile.TemporaryDirectory() as directory:
        environment = os.environ.copy()
        with observed_sandbox_descendants(Path(directory), environment) as observe:
            client = McpClient(binary, SANDBOXED.serve(), environment)
            generation: _Generation | None = None
            try:
                client.initialize_and_list_tools()
                generation = _spawn_processx_generation(client)
                # Retirement covers detached children already registered by the
                # runner; a ready worker does not establish that observation.
                observe(generation[2][0], client.process)
                yield client, generation
            finally:
                stop_client(client)
                if generation is not None:
                    _kill_generation(generation)
                    shutil.rmtree(generation[3].parent, ignore_errors=True)


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_restart_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    with _observed_processx_generation(binary) as (client, generation):
        client.send(control="restart")
        _assert_generation_retired(generation, "restart")
        return client.finish()


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_failure_replacement_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    with _observed_processx_generation(binary) as (client, generation):
        client.send(r="tools::pskill(Sys.getpid(), signal = 9L)")
        result = client.transcript[-1]["result"]
        assert result == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker sideband read failed: worker sideband closed]\n"
                        "[worker terminated by signal 9]\n"
                        "[worker stopped: in-memory state lost]\n"
                        "[starting new worker]\n"
                        "[idle]"
                    ),
                }
            ],
            "isError": True,
        }, result
        _assert_generation_retired(generation, "failure replacement")
        client.expect("replacement ready\n", r='writeLines("replacement ready")')
        return client.finish()


@requires(MACOS_SANDBOX, NATIVE_FIXTURES, PROCESS_EVENTS)
def test_server_shutdown_retires_descendants_outside_the_worker_group(
    binary: Path,
) -> Transcript:
    with _observed_processx_generation(binary) as (client, generation):
        transcript = client.finish()
        _assert_generation_retired(generation, "shutdown")
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
