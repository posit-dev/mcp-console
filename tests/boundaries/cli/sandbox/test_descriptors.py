#!/usr/bin/env -S uv run --script

import os
import select
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

if os.name == "posix":
    import fcntl


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.cli._harness import _read_lines
from support.normalization import code
from support.processes import host_process_id, process_exists
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite

TIMEOUT = 10


@requires(SANDBOX)
def test_accepts_closed_standard_input(binary: Path) -> Transcript:
    # fmt: python
    launcher_script = code(r"""
        import os
        import sys

        os.close(0)
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    arguments = ("sandbox", "--", "/bin/cat")
    result = subprocess.run(
        [sys.executable, "-c", launcher_script, binary, *arguments],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
    )

    assert result.returncode == 0, result
    assert result.stdout == "", result.stdout
    assert result.stderr == "", result.stderr
    return [
        {
            "scenario": "standard input closed before launcher exec",
            "command": ["mcp-console", *arguments],
            "stdout": result.stdout,
            "exit_code": result.returncode,
        }
    ]


@requires(SANDBOX)
def test_closes_unlisted_inherited_descriptors(binary: Path) -> Transcript:
    # fmt: python
    launcher_script = code(r"""
        import os
        import resource
        import sys

        _, hard_limit = resource.getrlimit(resource.RLIMIT_NOFILE)
        resource.setrlimit(resource.RLIMIT_NOFILE, (32, hard_limit))
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    # fmt: python
    sandboxed_script = code(r"""
        import errno
        import os
        import sys

        try:
            os.write(int(sys.argv[1]), b"escaped")
        except OSError as error:
            assert error.errno == errno.EBADF
        else:
            raise SystemExit("unlisted inherited descriptor remained writable")

        print("closed")
        """)
    arguments = ("sandbox", "--", "python", "-c", sandboxed_script)

    with TemporaryDirectory() as directory:
        host_file = Path(directory) / "host.txt"
        host_file.write_bytes(b"")
        with host_file.open("ab", buffering=0) as stream:
            descriptor = fcntl.fcntl(stream.fileno(), fcntl.F_DUPFD, 64)
            os.set_inheritable(descriptor, True)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        "-c",
                        launcher_script,
                        binary,
                        *arguments,
                        str(descriptor),
                    ],
                    pass_fds=(descriptor,),
                    capture_output=True,
                    text=True,
                    timeout=TIMEOUT,
                )
            finally:
                os.close(descriptor)
        escaped = host_file.read_bytes()

    assert result.returncode == 0, result
    assert result.stdout == "closed\n", result.stdout
    assert result.stderr == "", result.stderr
    assert escaped == b"", escaped
    return [
        {
            "command": ["mcp-console", *arguments, "<inherited fd>"],
            "stdout": result.stdout,
        }
    ]


@requires(SANDBOX)
def test_large_setup_preserves_prequeued_binary_stdio(binary: Path) -> Transcript:
    # Occupy the low descriptor slots so the setup pipe cannot assume fd 3.
    # The inherited files are identifiable even if their fd numbers are reused.
    with TemporaryDirectory() as directory:
        host_file = Path(directory) / "inherited"
        host_file.write_bytes(b"host data")
        descriptors = [os.open(host_file, os.O_RDONLY) for _ in range(24)]
        read, write = os.pipe()
        sentinel = bytes(range(256))
        os.write(write, sentinel)
        os.close(write)
        # fmt: python
        script = code(r"""
            import os
            import sys

            host = os.stat(sys.argv[1])
            for name in os.listdir("/dev/fd"):
                try:
                    entry = os.fstat(int(name))
                except OSError:
                    continue
                assert (entry.st_dev, entry.st_ino) != (host.st_dev, host.st_ino)
            assert len(os.environ["LARGE_SETUP"]) == 96 * 1024
            data = sys.stdin.buffer.read()
            os.write(1, data)
            os.write(2, data[::-1])
            """)
        try:
            result = subprocess.run(
                [binary, "sandbox", "--", sys.executable, "-c", script, host_file],
                stdin=read,
                pass_fds=descriptors,
                env=os.environ | {"LARGE_SETUP": "x" * (96 * 1024)},
                capture_output=True,
                timeout=TIMEOUT,
            )
        finally:
            os.close(read)
            for descriptor in descriptors:
                os.close(descriptor)
    assert result.returncode == 0, result
    assert result.stdout == sentinel, result.stdout
    assert result.stderr == sentinel[::-1], result.stderr
    return [
        {
            "scenario": "large setup, non-default setup descriptor, prequeued binary stdin",
            "stdout_hex": result.stdout.hex(),
            "stderr_hex": result.stderr.hex(),
            "exit_code": result.returncode,
        }
    ]


@requires(SANDBOX)
def test_regular_file_stdin_shares_offset_and_seekability(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os

        assert os.lseek(0, 0, os.SEEK_CUR) == 7
        os.write(1, os.read(0, 100))
        os.lseek(0, 2, os.SEEK_SET)
        """)
    with TemporaryDirectory() as directory:
        source = Path(directory) / "input"
        source.write_bytes(b"prefix:" + bytes(range(256)))
        with source.open("rb") as stream:
            stream.seek(7)
            result = subprocess.run(
                [binary, "sandbox", "--", sys.executable, "-c", script],
                stdin=stream,
                capture_output=True,
                timeout=TIMEOUT,
            )
            assert result.returncode == 0, result
            assert stream.tell() == 2, (
                "target did not inherit the open file description"
            )
    assert result.stdout == bytes(range(100)), result.stdout
    assert result.stderr == b"", result.stderr
    return [
        {
            "scenario": "target seeks the original input file",
            "shared_offset": 2,
            "stdout_hex": result.stdout.hex(),
            "exit_code": result.returncode,
        }
    ]


@requires(SANDBOX)
def test_owned_launcher_exposes_target_input_closure(binary: Path) -> Transcript:
    # Hold the target alive using a separate inherited standard stream. Fill its
    # input first so POLLOUT can wake only when all readers have closed it.
    # fmt: python
    script = code(r"""
        import os

        os.close(0)
        parent = os.getppid()
        print("closed", parent, flush=True)
        assert os.read(2, 1) == b"x"
        assert os.getppid() == parent
        """)
    gate, release = os.pipe()
    process = subprocess.Popen(
        [
            binary,
            "sandbox",
            "--exit-with-parent",
            str(os.getpid()),
            "--",
            sys.executable,
            "-c",
            script,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=gate,
    )
    os.close(gate)
    assert process.stdin is not None and process.stdout is not None
    try:
        os.set_blocking(process.stdin.fileno(), False)
        while True:
            try:
                os.write(process.stdin.fileno(), b"x" * 4096)
            except BlockingIOError:
                break
            except BrokenPipeError:
                break
        (ready,) = _read_lines(process.stdout, 1, "target input closure")
        checkpoint, parent = ready.split()
        assert checkpoint == "closed", ready
        # Seatbelt cannot signal its host parent, even with signal zero.
        # Inspect that process from the host while the target holds its gate.
        parent = host_process_id(int(parent), process.pid)
        assert process_exists(parent)
        assert process.poll() is None
        _, writable, _ = select.select([], [process.stdin], [], TIMEOUT)
        assert writable, "a host process retained the target's input reader"
        try:
            os.write(process.stdin.fileno(), b"closure probe")
        except BrokenPipeError:
            pass
        else:
            raise AssertionError("a host process retained the target's input reader")
        assert process.poll() is None
        assert process_exists(parent)
        os.write(release, b"x")
        assert process.wait(timeout=TIMEOUT) == 0
    finally:
        os.close(release)
        process.stdin.close()
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=TIMEOUT)
        process.stdout.close()
    return [
        {
            "scenario": "target closes stdin while target, runner, and launcher remain alive",
            "stdin_write": "EPIPE",
            "exit_code": process.returncode,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
