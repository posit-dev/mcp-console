#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.checkpoints import wait_for_path
from support.native import LOADER_VARIABLE, build_interposer
from support.processes import process_exists, stop_process_id
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import NATIVE_FIXTURES, POSIX, R, WORKER, requires
from support.suites import run_this_suite


@requires(POSIX)
@requires(WORKER)
def test_worker_reports_closed_output_pipe_without_r_sigpipe_handler(
    binary: Path,
) -> Transcript:
    # This raw launch supplies the private storage normally owned by the launcher.
    with tempfile.TemporaryDirectory() as temporary:
        worker_read, relay_write = os.pipe()
        relay_read, worker_write = os.pipe()
        # Exercise the output callback without user startup or default packages.
        environment = dict(
            os.environ,
            TMPDIR=temporary,
            R_DEFAULT_PACKAGES="NULL",
        )
        for name in ("R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"):
            environment[name] = os.devnull
        environment["MCP_CONSOLE_SIDEBAND_READ_FD"] = str(worker_read)
        environment["MCP_CONSOLE_SIDEBAND_WRITE_FD"] = str(worker_write)
        process = subprocess.Popen(
            [binary, "worker"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            pass_fds=(worker_read, worker_write),
            env=environment,
            cwd=temporary,
            text=True,
        )
        os.close(worker_read)
        os.close(worker_write)
        with os.fdopen(relay_read) as output, os.fdopen(relay_write, "w") as commands:
            try:
                assert json.loads(read_lines(output, 1, "worker ready")[0]) == {
                    "kind": "ready"
                }
                commands.write(
                    json.dumps(
                        {
                            "kind": "evaluate",
                            "language": "r",
                            "source": "invisible(NULL)",
                        }
                    )
                    + "\n"
                )
                commands.flush()
                assert [
                    json.loads(line)
                    for line in read_lines(output, 4, "cell start and R initialization")
                ] == [
                    {"kind": "cell_started"},
                    {"kind": "r_initialization", "state": "entered"},
                    {"kind": "r_initialization", "state": "complete"},
                    {"kind": "completed"},
                ]
                commands.write(
                    json.dumps(
                        {
                            "kind": "evaluate",
                            "language": "r",
                            "source": "readBin(file('/dev/stdin', 'rb'), 'raw', 1L); cat('closed pipe\\n')",
                        }
                    )
                    + "\n"
                )
                commands.flush()
                assert json.loads(read_lines(output, 1, "second cell started")[0]) == {
                    "kind": "cell_started"
                }
                output.close()
                process.stdin.write("1")
                process.stdin.flush()
                # Keep stdin and the command pipe open until failure is observed.
                # Their EOF must not cause ordinary worker retirement first.
                assert process.wait(timeout=5) == 1
                stdout, stderr = process.communicate(timeout=5)
                assert stdout == "", stdout
                assert (
                    stderr == "R console output failed: Broken pipe (os error 32)\n"
                ), stderr
                return [
                    {
                        "exit_code": process.returncode,
                        "stdout": stdout,
                        "stderr": stderr,
                    }
                ]
            finally:
                if process.returncode is None:
                    process.kill()
                process.communicate(timeout=5)


@requires(NATIVE_FIXTURES, R, WORKER)
def test_worker_reaps_library_probe_on_stdin_eof(binary: Path) -> Transcript:
    return reap_library_probe(binary, "stdin_eof")


@requires(NATIVE_FIXTURES, R, WORKER)
def test_worker_reaps_library_probe_on_shutdown_frame(binary: Path) -> Transcript:
    return reap_library_probe(binary, "shutdown_frame")


def reap_library_probe(
    binary: Path, shutdown: Literal["stdin_eof", "shutdown_frame"]
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        library = root / "library"
        library.mkdir()
        # Supply the native launcher's managed-library handoff; R's own
        # .Renviron adds the project glob and the selected Rscript checkpoint.
        fixture = build_interposer(root, "r_library_probe")
        environ = root / ".Renviron"
        environ.write_text(
            f'{LOADER_VARIABLE}="{fixture}"\nR_LIBS="${{R_LIBS}}:project-*"\n'
        )
        worker_read, relay_write = os.pipe()
        relay_read, worker_write = os.pipe()
        environment.update(
            TMPDIR=str(root),
            R_LIBS=str(library),
            MCP_CONSOLE_R_LIBRARY=str(library),
            R_ENVIRON=os.devnull,
            R_ENVIRON_USER=str(environ),
            R_PROFILE=os.devnull,
            R_PROFILE_USER=os.devnull,
            MCP_CONSOLE_SIDEBAND_READ_FD=str(worker_read),
            MCP_CONSOLE_SIDEBAND_WRITE_FD=str(worker_write),
        )
        with (
            os.fdopen(worker_read) as incoming,
            os.fdopen(worker_write, "w") as outgoing,
            os.fdopen(relay_read) as output,
            os.fdopen(relay_write, "w") as commands,
            subprocess.Popen(
                [binary, "worker"],
                cwd=root,
                env=environment,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                pass_fds=(worker_read, worker_write),
                text=True,
            ) as worker,
        ):
            incoming.close()
            outgoing.close()
            pid = None
            try:
                assert json.loads(read_lines(output, 1, "worker ready")[0]) == {
                    "kind": "ready"
                }
                commands.write('{"kind":"evaluate","language":"r","source":"42L"}\n')
                commands.flush()
                marker = root / "probe.pid"
                wait_for_path(marker, "Rscript library probe")
                pid = int(marker.read_text())
                # Keep the command pipe open and send only the selected shutdown signal.
                if shutdown == "stdin_eof":
                    worker.stdin.close()
                else:
                    commands.write('{"kind":"shutdown"}\n')
                    commands.flush()
                assert worker.wait(timeout=10) == 0
                assert not process_exists(pid), pid
                assert worker.stdout.read() == worker.stderr.read() == ""
                return [json.loads(line) for line in output] + [
                    {f"{shutdown}_reaped_probe": True}
                ]
            except BaseException:
                stop_process_id(pid)
                if worker.poll() is None:
                    worker.kill()
                raise


if __name__ == "__main__":
    run_this_suite(__file__)
