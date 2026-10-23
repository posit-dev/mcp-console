#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.capture import read_lines
from support.records import Transcript
from support.requirements import POSIX, WORKER, requires
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
                    {"kind": "r_initialization", "complete": False},
                    {"kind": "r_initialization", "complete": True},
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


if __name__ == "__main__":
    run_this_suite(__file__)
