from __future__ import annotations

import os
import select
import sys
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from support.execution import SANDBOXED
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.native import build_interposer
from support.macos import (
    capture_darwin_process_identity,
    darwin_child_process_identities,
)
from support.normalization import code

CHECKPOINT_TIMEOUT_SECONDS = 15


@dataclass
class LauncherRetirement:
    client: McpClient
    launcher_pid: int
    launcher_exit: select.kqueue
    signal_blocked: FifoCheckpoint
    signal_release: FifoCheckpoint
    signal_returned: FifoCheckpoint
    signal_return_release: FifoCheckpoint
    relay_exit: FifoCheckpoint

    def assert_launcher_running(self) -> None:
        assert self.launcher_exit.control(None, 1, 0) == []

    def wait_for_launcher_exit(self) -> None:
        events = self.launcher_exit.control(None, 1, CHECKPOINT_TIMEOUT_SECONDS)
        assert len(events) == 1, "sandbox launcher did not exit"
        assert events[0].ident == self.launcher_pid, events[0]
        assert events[0].filter == select.KQ_FILTER_PROC, events[0]
        assert events[0].fflags & select.KQ_NOTE_EXIT, events[0]


@contextmanager
def launcher_retirement(binary: Path) -> Iterator[LauncherRetirement]:
    relay = (
        Path(__file__).resolve().parents[3]
        / "fixtures"
        / "server_relay"
        / "scripted_relay.py"
    )
    # fmt: python
    server = code(r"""
        import os
        import sys

        os.environ["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_PID"] = str(os.getpid())
        os.environ["DYLD_INSERT_LIBRARIES"] = os.environ.pop(
            "MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_DYLIB"
        )
        os.execv(sys.argv[1], sys.argv[1:])
        """)
    with ExitStack() as resources:
        temporary = Path(resources.enter_context(tempfile.TemporaryDirectory()))

        def checkpoint(name: str) -> FifoCheckpoint:
            result = FifoCheckpoint.create(temporary / name)
            resources.callback(result.close)
            return result

        signal_blocked = checkpoint("signal-blocked")
        signal_release = checkpoint("signal-release")
        signal_returned = checkpoint("signal-returned")
        signal_return_release = checkpoint("signal-return-release")
        relay_exit = checkpoint("relay-exit")
        environment = os.environ.copy()
        environment["TMPDIR"] = str(temporary)
        environment["MCP_CONSOLE_TEST_RELAY_SCENARIO"] = "shutdown_nonzero_after_output"
        environment["MCP_CONSOLE_TEST_RELAY_EXIT_STATUS"] = "137"
        environment["MCP_CONSOLE_TEST_RELAY_EXIT_RELEASE"] = str(relay_exit.path)
        environment["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_DYLIB"] = str(
            build_interposer(temporary, "launcher_retirement_interposer")
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_BLOCKED"] = str(
            signal_blocked.path
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RELEASE"] = str(
            signal_release.path
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURNED"] = str(
            signal_returned.path
        )
        environment["MCP_CONSOLE_TEST_RETIREMENT_SIGNAL_RETURN_RELEASE"] = str(
            signal_return_release.path
        )
        launcher_exit = select.kqueue()
        resources.callback(launcher_exit.close)
        client = resources.enter_context(
            McpClient(
                Path(sys.executable),
                (
                    "-c",
                    server,
                    str(binary),
                    *SANDBOXED.serve("--worker", str(binary), "--relay", str(relay)),
                ),
                environment,
                current_directory=temporary,
            )
        )
        try:
            client.initialize_and_list_tools()
            client.send(control="restart")
            assert last_tool_text(client) == "[starting new worker]\n[idle]"
            server_identity = capture_darwin_process_identity(client.process.pid)
            launchers = darwin_child_process_identities(server_identity)
            assert len(launchers) == 1, launchers
            launcher_pid = launchers[0][0]
            exit_watch = select.kevent(
                launcher_pid,
                filter=select.KQ_FILTER_PROC,
                flags=select.KQ_EV_ADD | select.KQ_EV_CLEAR,
                fflags=select.KQ_NOTE_EXIT,
            )
            assert launcher_exit.control([exit_watch], 0, 0) == []
            yield LauncherRetirement(
                client,
                launcher_pid,
                launcher_exit,
                signal_blocked,
                signal_release,
                signal_returned,
                signal_return_release,
                relay_exit,
            )
        finally:
            relay_exit.release()
            signal_release.release()
            signal_return_release.release()
