#!/usr/bin/env -S uv run --script

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import resolve_managed_python
from support.suites import run_this_suite
from support.requirements import POSIX, requires


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_worker_adopts_both_pipes_and_isolates_fork_and_exec(
    binary: Path, execution: Execution
) -> Transcript:
    wrapper = Path(__file__).resolve().parents[3] / "fixtures" / "sideband_worker"
    environment = os.environ.copy()
    environment["MCP_CONSOLE_TEST_WORKER_BINARY"] = str(binary)
    environment["MCP_CONSOLE_TEST_CLOSED_PROBE"] = str(
        wrapper.with_name("sideband_closed.py")
    )
    # A custom launcher wrapping the internal worker retains the on-demand
    # protocol: it receives neither default preparation nor bootstrap completion.
    with tempfile.TemporaryDirectory() as temporary:
        environment["RETICULATE_PYTHON"] = str(
            resolve_managed_python(binary, execution, Path(temporary))
        )
    # The custom launcher owns the built-in worker's storage until it exits.
    # A sandbox runner replaces TMPDIR with its own private lifetime directory.
    with (
        tempfile.TemporaryDirectory() as storage,
        McpClient(
            binary,
            execution.serve("--worker", str(wrapper)),
            dict(environment, TMPDIR=storage),
        ) as client,
    ):
        client.initialize_and_list_tools()
        # fmt: python
        source = code(r"""
            import errno
            import fcntl
            import os
            import stat
            import subprocess
            import sys
            import warnings

            assert "MCP_CONSOLE_SIDEBAND_FD" not in os.environ
            descriptors = []
            for direction, mode in (("READ", os.O_RDONLY), ("WRITE", os.O_WRONLY)):
                assert f"MCP_CONSOLE_SIDEBAND_{direction}_FD" not in os.environ
                fd = int(os.environ[f"MCP_CONSOLE_TEST_SIDEBAND_{direction}"])
                descriptors.append(fd)
                assert stat.S_ISFIFO(os.fstat(fd).st_mode)
                flags = fcntl.fcntl(fd, fcntl.F_GETFL)
                assert flags & os.O_ACCMODE == mode
                assert flags & os.O_NONBLOCK
                assert fcntl.fcntl(fd, fcntl.F_GETFD) & fcntl.FD_CLOEXEC


            def require_closed():
                for fd in descriptors:
                    try:
                        fcntl.fcntl(fd, fcntl.F_GETFD)
                    except OSError as error:
                        assert error.errno == errno.EBADF
                    else:
                        raise AssertionError("child retained sideband")


            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                child = os.fork()
            if child == 0:
                try:
                    require_closed()
                except BaseException:
                    os._exit(1)
                os._exit(0)
            assert os.waitpid(child, 0) == (child, 0)
            subprocess.run(
                [sys.executable, os.environ["MCP_CONSOLE_TEST_CLOSED_PROBE"]],
                close_fds=False,
                check=True,
            )
            print("both pipes isolated")
            """)
        result = client.send(python=source)
        assert result == {
            "content": [{"type": "text", "text": "both pipes isolated\n"}],
            "isError": False,
        }, result
        result = client.send(r="cat('parent sideband remains usable\\n')")
        assert result == {
            "content": [{"type": "text", "text": "parent sideband remains usable\n"}],
            "isError": False,
        }, result
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
