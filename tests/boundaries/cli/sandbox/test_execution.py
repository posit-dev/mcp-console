#!/usr/bin/env -S uv run --script

import os
import select
import selectors
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from tempfile import TemporaryDirectory

if os.name == "posix":
    import pty


sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.normalization import code
from support.records import Transcript, TranscriptEntry
from support.requirements import MACOS_SANDBOX, SANDBOX, requires
from support.suites import run_this_suite


def record(
    binary: Path,
    *arguments: str,
    environment: dict[str, str | None] | None = None,
    current_directory: Path | None = None,
) -> TranscriptEntry:
    child_environment = os.environ.copy()
    for name, value in (environment or {}).items():
        if value is None:
            child_environment.pop(name, None)
        else:
            child_environment[name] = value

    result = subprocess.run(
        [binary, *arguments],
        capture_output=True,
        cwd=current_directory,
        env=child_environment,
    )
    entry: TranscriptEntry = {
        "command": ["mcp-console", *arguments],
    }
    if environment is not None:
        entry["environment"] = environment
    if result.returncode != 0:
        entry["exit_code"] = result.returncode
    entry["stdout"] = result.stdout.decode("utf-8")
    if result.stderr:
        entry["stderr"] = result.stderr.decode("utf-8")
    return entry


@requires(SANDBOX)
def test_preserves_arguments_and_executable_names(binary: Path) -> Transcript:
    transcript: Transcript = []

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        shutil.copy("/usr/bin/true", current_directory / "program=fixture")
        entry = record(
            binary,
            "sandbox",
            "--",
            "./program=fixture",
            "/usr/bin/false",
            current_directory=current_directory,
        )

    assert "exit_code" not in entry, (
        "the executable name was parsed as an environment assignment"
    )
    transcript.append({"scenario": "equals sign in executable name", **entry})

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        shutil.copy("/usr/bin/true", current_directory / "--help")
        entry = record(
            binary,
            "sandbox",
            "--",
            "--help",
            environment={"PATH": os.pathsep.join((".", os.environ["PATH"]))},
            current_directory=current_directory,
        )

    entry["environment"] = {"PATH": ".:<inherited PATH>"}
    entry["transcript_normalization"] = {
        "target": "environment.PATH",
        "inherited_path": "omitted",
    }
    assert "exit_code" not in entry, (
        "the executable name was parsed as a launcher option"
    )
    assert entry["stdout"] == "", "the launcher handled the target's option-like name"
    transcript.append({"scenario": "option-like executable name", **entry})

    # fmt: python
    script = code(r"""
        import sys

        print("|".join(sys.argv[1:]))
        """)
    arguments = (
        "sandbox",
        "python",
        "-c",
        script,
        "hello world",
        "$(not-a-command)",
        "--child-option",
    )
    transcript.append(
        {
            "scenario": "literal arguments and standard output",
            **record(binary, *arguments),
        }
    )
    return transcript


@requires(SANDBOX)
def test_rejects_unrelated_exit_parent(binary: Path) -> Transcript:
    entry = record(
        binary,
        "sandbox",
        "--exit-with-parent",
        "1",
        "--",
        "python",
        "-c",
        "print('target ran')",
    )

    assert entry == {
        "command": [
            "mcp-console",
            "sandbox",
            "--exit-with-parent",
            "1",
            "--",
            "python",
            "-c",
            "print('target ran')",
        ],
        "exit_code": 1,
        "stdout": "",
        "stderr": "sandbox owner 1 is not the launcher's current parent\n",
    }
    return [entry]


@requires(SANDBOX)
def test_forwards_interactive_standard_streams(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import sys

        print("ready", flush=True)
        for line in sys.stdin:
            if line == "EXIT\n":
                break

            sys.stdout.write(line)
            sys.stdout.flush()
            sys.stderr.write(line)
            sys.stderr.flush()
        """)
    arguments = ("sandbox", "--", "python", "-c", script)

    process = subprocess.Popen(
        [binary, *arguments],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None
    assert process.stderr is not None
    stdin = process.stdin
    stdout = process.stdout
    stderr = process.stderr
    timeout = 5

    try:
        # Time the interactive exchange after target startup, which can contend
        # with other parallel cases. The case supervisor bounds startup itself.
        assert stdout.readline() == b"ready\n"
        input_line = b"echo exactly: $(literal)\n"
        stdin.write(input_line)
        stdin.flush()

        output = {"stdout": bytearray(), "stderr": bytearray()}
        with selectors.DefaultSelector() as selector:
            selector.register(stdout, selectors.EVENT_READ, "stdout")
            selector.register(stderr, selectors.EVENT_READ, "stderr")
            deadline = time.monotonic() + timeout
            while any(b"\n" not in stream for stream in output.values()):
                remaining = deadline - time.monotonic()
                assert remaining > 0, "timed out waiting for sandbox output"
                ready = selector.select(remaining)
                assert ready, "timed out waiting for sandbox output"
                for key, _ in ready:
                    chunk = os.read(key.fd, 4096)
                    assert chunk, f"{key.data} closed before returning a line"
                    output[key.data].extend(chunk)

        echoed_output = output["stdout"].decode("utf-8")
        echoed_error = output["stderr"].decode("utf-8")
        input_text = input_line.decode("utf-8")

        assert process.poll() is None
        assert echoed_output == input_text
        assert echoed_error == input_text

        stdin.write(b"EXIT\n")
        stdin.flush()
        exit_code = process.wait(timeout=timeout)
        assert os.read(stdout.fileno(), 4096) == b""
        assert os.read(stderr.fileno(), 4096) == b""
        assert exit_code == 0
    finally:
        try:
            try:
                stdin.close()
            except BrokenPipeError:
                pass
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=timeout)
        finally:
            stdout.close()
            stderr.close()

    return [
        {
            "command": ["mcp-console", *arguments],
            "stdin": input_text,
            "stdout": "ready\n" + echoed_output,
            "stderr": echoed_error,
        },
        {
            "stdin": "EXIT\n",
            "exit_code": exit_code,
        },
    ]


@requires(MACOS_SANDBOX)
def test_enforces_host_read_only_and_temporary_writes(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import errno
        import os
        import pathlib
        import sys

        host_file = pathlib.Path(sys.argv[1])
        assert host_file.read_text(encoding="utf-8") == "host data"
        try:
            host_file.write_text("modified", encoding="utf-8")
        except OSError as error:
            assert error.errno == errno.EPERM
        else:
            raise SystemExit("host regular-file write was allowed")

        output = pathlib.Path(os.environ["TMPDIR"]) / "result.txt"
        output.write_text("sandbox temp", encoding="utf-8")
        print(host_file.read_text(encoding="utf-8"))
        print(output.read_text(encoding="utf-8"))
        print(os.environ["TMPDIR"])
        """)

    with TemporaryDirectory() as directory:
        current_directory = Path(directory)
        host_file = current_directory / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "host.txt",
            current_directory=current_directory,
        )
        stdout = entry["stdout"]
        assert isinstance(stdout, str)
        host_data, temporary_data, temporary_directory = stdout.splitlines()
        assert host_data == "host data"
        assert temporary_data == "sandbox temp"
        assert host_file.read_text(encoding="utf-8") == "host data"
        assert not Path(temporary_directory).exists()

    entry["stdout"] = """host data
sandbox temp
<sandbox temp>
"""
    entry["transcript_normalization"] = {
        "target": "stdout line 3",
        "sandbox_temporary_directory": "omitted",
    }
    return [entry]


@requires(MACOS_SANDBOX)
def test_allows_replacing_temporary_directories(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        from pathlib import Path

        temporary = Path(os.environ["TMPDIR"])
        nested = temporary / "nested"
        nested.mkdir()
        for parent in (temporary, nested):
            for name in (".git", ".agents", ".codex"):
                directory = parent / name
                directory.mkdir()
                (directory / "payload").write_text("temporary data", encoding="utf-8")
                renamed = directory.with_name(name + "-renamed")
                directory.rename(renamed)
                assert (renamed / "payload").read_text(encoding="utf-8") == "temporary data"
                (renamed / "payload").unlink()
                renamed.rmdir()
                directory.mkdir()
                directory.rmdir()
                print(f"{parent.relative_to(temporary)}: {name} created, renamed, removed")

        nested.rmdir()
        temporary.rmdir()
        assert not temporary.exists()
        temporary.mkdir()
        print("temporary directory recreated")
        """)
    entry = record(binary, "sandbox", "--", "python", "-c", script)
    assert "exit_code" not in entry, entry
    assert "stderr" not in entry, entry
    assert entry["stdout"] == (
        ".: .git created, renamed, removed\n"
        ".: .agents created, renamed, removed\n"
        ".: .codex created, renamed, removed\n"
        "nested: .git created, renamed, removed\n"
        "nested: .agents created, renamed, removed\n"
        "nested: .codex created, renamed, removed\n"
        "temporary directory recreated\n"
    ), entry
    return [entry]


@requires(SANDBOX)
def test_allows_processx_pty_processes(binary: Path) -> Transcript:
    # fmt: r
    script = code(r"""
        local({
          p <- processx::process$new(
            "/bin/sh",
            c("-c", 'IFS= read -r line && printf "received: %s" "$line"'),
            pty = TRUE,
            pty_options = list(echo = FALSE)
          )
          on.exit(if (p$is_alive()) p$kill())
          deadline <- proc.time()[["elapsed"]] + 5
          remaining <- function() {
            ms <- ceiling(1000 * (deadline - proc.time()[["elapsed"]]))
            stopifnot(ms > 0)
            ms
          }
          p$write_input("sandboxed pty\n")
          output <- ""
          while (p$is_incomplete_output()) {
            ready <- processx::poll(list(p$get_output_connection()), remaining())
            stopifnot(ready[[1]] == "ready")
            output <- paste0(output, p$read_output())
          }
          p$wait(remaining())
          stopifnot(identical(p$get_exit_status(), 0L))
          cat(output)
        })
        """)
    entry = record(binary, "sandbox", "--", "Rscript", "-e", script)
    assert "exit_code" not in entry, entry
    assert "stderr" not in entry, entry
    # Echo is disabled, and the response has no LF for the PTY to map to CRLF.
    assert entry["stdout"] == "received: sandboxed pty", entry
    return [entry]


@requires(MACOS_SANDBOX)
def test_cannot_open_a_preexisting_pseudo_terminal(binary: Path) -> Transcript:
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)
    # fmt: python
    script = code(r"""
        import errno
        import os
        import sys

        for flags in (os.O_RDONLY, os.O_WRONLY):
            try:
                descriptor = os.open(sys.argv[1], flags | os.O_NOCTTY)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                os.close(descriptor)
                raise SystemExit("pre-existing pseudo-terminal was accessible")

        print("blocked")
        """)
    try:
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            slave_name,
        )
    finally:
        os.close(master)
        os.close(slave)

    assert "exit_code" not in entry, entry
    assert entry["stdout"] == "blocked\n", entry
    command = entry["command"]
    assert isinstance(command, list)
    command[-1] = "<pre-existing pseudo-terminal>"
    entry["transcript_normalization"] = {
        "target": "command[-1]",
        "pseudo_terminal_path": "omitted",
    }
    return [entry]


@requires(MACOS_SANDBOX)
def test_cannot_flush_a_host_terminal(binary: Path) -> Transcript:
    # Reading terminal attributes remains usable for interactive commands, but
    # flushing another host terminal's queued input is a mutating ioctl.
    # fmt: python
    script = code(r"""
        import errno
        import sys
        import termios

        assert termios.tcgetattr(0)
        try:
            termios.tcflush(0, termios.TCIFLUSH)
        except termios.error as error:
            assert error.args[0] == errno.EPERM
            assert sys.argv[1] == "sandbox"
            print("blocked")
        else:
            assert sys.argv[1] == "host", "sandbox flushed a host terminal"
            print("flushed")
        """)
    master, slave = pty.openpty()
    try:
        for prefix, expected in (
            ([], b"flushed\n"),
            ([binary, "sandbox", "--"], b"blocked\n"),
        ):
            result = subprocess.run(
                [
                    *prefix,
                    sys.executable,
                    "-c",
                    script,
                    "sandbox" if prefix else "host",
                ],
                stdin=slave,
                capture_output=True,
                timeout=10,
            )
            assert result.returncode == 0, result
            assert result.stdout == expected, result
            assert result.stderr == b"", result
    finally:
        os.close(master)
        os.close(slave)
    return [
        {"scenario": "mutating ioctl on an inherited host PTY", "stdout": "blocked\n"}
    ]


@requires(MACOS_SANDBOX)
def test_cannot_hard_link_a_host_file_into_the_writable_directory(
    binary: Path,
) -> Transcript:
    with TemporaryDirectory() as directory:
        root = Path(directory)
        host_file = root / "host.txt"
        host_file.write_text("host data", encoding="utf-8")
        # fmt: python
        script = code(r"""
            import errno
            import os
            import pathlib
            import sys

            destination = pathlib.Path(os.environ["TMPDIR"]) / "host-link"
            assert os.stat(sys.argv[1]).st_dev == os.stat(destination.parent).st_dev
            try:
                os.link(sys.argv[1], destination)
            except OSError as error:
                assert error.errno == errno.EPERM
            else:
                destination.write_text("escaped")
                raise SystemExit("host hard-link escape succeeded")

            print("blocked")
            """)
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            "host.txt",
            environment={"TMPDIR": "."},
            current_directory=root,
        )

        assert "exit_code" not in entry, entry
        assert entry["stdout"] == "blocked\n", entry
        assert host_file.read_text(encoding="utf-8") == "host data"
        return [entry]


@requires(SANDBOX)
def test_denies_network_access(binary: Path) -> Transcript:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        # fmt: python
        script = code(r"""
            import errno
            import socket
            import sys

            try:
                socket.create_connection(("127.0.0.1", int(sys.argv[1])))
            except OSError as error:
                assert error.errno == errno.EPERM
                print("blocked")
            else:
                raise SystemExit("network access was allowed")
            """)
        entry = record(
            binary,
            "sandbox",
            "--",
            "python",
            "-c",
            script,
            str(port),
        )
        listener.setblocking(False)
        try:
            listener.accept()
        except BlockingIOError:
            pass
        else:
            raise AssertionError("sandboxed child reached the host listener")

    assert "exit_code" not in entry, entry
    assert entry["stdout"] == "blocked\n", entry
    command = entry["command"]
    assert isinstance(command, list)
    command[-1] = "<listener port>"
    entry["transcript_normalization"] = {
        "target": "command[-1]",
        "listener_port": "omitted",
    }
    return [entry]


@requires(SANDBOX)
def test_frontend_exec_preserves_pid_and_standard_streams(binary: Path) -> Transcript:
    # fmt: python
    script = code(r"""
        import os
        import sys

        assert "MCP_CONSOLE_SANDBOX_CONFIG" not in os.environ
        print("ready", flush=True)
        data = sys.stdin.buffer.read()
        os.write(1, data)
        os.write(2, data[::-1])
        sys.exit(23)
        """)
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
        stderr=subprocess.PIPE,
    )
    try:
        assert select.select([process.stdout], [], [], 10)[0]
        assert process.stdout.readline() == b"ready\n"
        if sys.platform == "linux":
            executable = os.readlink(f"/proc/{process.pid}/exe")
        else:
            executable = subprocess.check_output(
                ["/bin/ps", "-p", str(process.pid), "-o", "comm="], text=True
            ).strip()
        sentinel = bytes(range(256))
        stdout, stderr = process.communicate(sentinel, timeout=10)
        assert (process.returncode, stdout, stderr) == (23, sentinel, sentinel[::-1])
        runner = binary.parent.parent / "libexec" / "mcp-console-sandbox"
        assert Path(executable).resolve() == runner.resolve(), executable
        return [
            {
                "scenario": "frontend exec with original PID and binary standard streams",
                "verified_executable": "private mcp-console-sandbox",
                "stdout_hex": stdout.hex(),
                "stderr_hex": stderr.hex(),
                "exit_code": process.returncode,
            }
        ]
    finally:
        if process.poll() is None:
            process.communicate(timeout=10)
        for stream in (process.stdin, process.stdout, process.stderr):
            stream.close()


if __name__ == "__main__":
    run_this_suite(__file__)
