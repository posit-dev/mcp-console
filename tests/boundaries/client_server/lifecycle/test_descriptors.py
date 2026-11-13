#!/usr/bin/env -S uv run --script

from __future__ import annotations

import errno
import os
import subprocess
import sys
import tempfile
from pathlib import Path

if os.name == "posix":
    import fcntl


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.client import McpClient, stop_client
from support.processes import (
    capture_process_identity,
    child_process_identities,
    process_file_descriptors,
)
from support.normalization import code
from support.records import Transcript, TranscriptEntry
from support.requirements import LINUX_NATIVE, PROCESS_EVENTS, requires
from support.suites import run_this_suite


def descriptor_entry(
    binary: Path,
    execution: Execution,
    launch_path: str,
    serve_arguments: tuple[str, ...],
    has_resolver: bool,
    environment_updates: dict[str, str] | None = None,
    launch_prefix: tuple[str, ...] = (),
) -> TranscriptEntry:
    # fmt: python
    launcher = code(r"""
        import os
        import resource
        import sys

        _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard_limit))
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    # fmt: python
    source = code(r"""
        import errno
        import os

        descriptor = int(os.environ["MCP_CONSOLE_TEST_INHERITED_FD"])
        try:
            os.write(descriptor, b"escaped")
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise RuntimeError("unlisted server descriptor reached the worker")

        print("closed")
        """)

    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        host_path = temporary / "host.txt"
        host_path.write_bytes(b"")
        environment = os.environ.copy()
        environment.update(environment_updates or {})
        with host_path.open("ab", buffering=0) as stream:
            descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            os.set_inheritable(descriptor, True)
            environment["MCP_CONSOLE_TEST_INHERITED_FD"] = str(descriptor)
            client = McpClient(
                Path(sys.executable),
                (
                    "-c",
                    launcher,
                    *launch_prefix,
                    str(binary),
                    *execution.serve(*serve_arguments),
                ),
                environment,
                current_directory=temporary,
                pass_fds=(descriptor,),
            )
            passed = False
            try:
                server = capture_process_identity(client.process.pid)
                client.initialize_and_list_tools()
                result = client.send(python=source)
                assert result == {
                    "content": [{"type": "text", "text": "closed\n"}],
                    "isError": False,
                }, result
                launchers = child_process_identities(server)
                expected = 1 + int(has_resolver)
                assert len(launchers) == expected, launchers
                for launcher in launchers:
                    assert descriptor not in process_file_descriptors(launcher), (
                        "unlisted server descriptor remained open in a child process"
                    )
                transcript = client.finish()
                passed = True
            finally:
                if not passed:
                    stop_client(client)
                os.close(descriptor)

        assert host_path.read_bytes() == b""
        entry: TranscriptEntry = {"launch_path": launch_path}
        entry.update(transcript[-1])
        return entry


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_closes_unlisted_server_descriptors_on_every_launch_path(
    binary: Path,
    execution: Execution,
) -> Transcript:
    probe = Path(__file__).resolve().parents[3] / "fixtures" / "descriptor_probe"
    cases = (
        ("builtin worker", (), True, None),
        ("custom worker", ("--worker", str(probe)), False, None),
        (
            "custom relay and worker",
            ("--worker", str(probe), "--relay", str(probe)),
            False,
            {"MCP_CONSOLE_TEST_BUILTIN_RELAY": str(binary)},
        ),
    )
    return [
        descriptor_entry(
            binary, execution, launch_path, arguments, has_resolver, environment
        )
        for launch_path, arguments, has_resolver, environment in cases
    ]


@requires(LINUX_NATIVE, PROCESS_EVENTS)
def test_sanitizes_descriptors_without_close_range_cloexec(binary: Path) -> Transcript:
    fixtures = Path(__file__).resolve().parents[3] / "fixtures"
    with tempfile.TemporaryDirectory() as directory:
        wrapper = Path(directory) / "without-close-range"
        subprocess.run(
            [
                "cc",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                wrapper,
                fixtures / "native" / "without_close_range.c",
            ],
            check=True,
        )
        transcript = []
        for error in (errno.ENOSYS, errno.EINVAL, errno.EPERM):
            prefix = (str(wrapper), str(error))
            transcript.append(
                descriptor_entry(
                    binary,
                    DIRECT,
                    errno.errorcode[error],
                    (),
                    True,
                    launch_prefix=prefix,
                )
            )
            with McpClient(
                wrapper,
                (
                    str(error),
                    str(binary),
                    *DIRECT.serve(
                        "--worker",
                        str(fixtures / "descriptor_probe"),
                        "--relay",
                        "/missing-relay",
                    ),
                ),
            ) as client:
                client.initialize_and_list_tools()
                result = client.send(r="must not run")
                assert result == {
                    "content": [
                        {
                            "type": "text",
                            "text": "[failed to launch worker relay: No such file or directory (os error 2)]",
                        }
                    ],
                    "isError": True,
                }, result
                transcript.extend(client.finish()[-1:])
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
