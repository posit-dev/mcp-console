"""Gate public runtime discovery and observe its descendant's retirement."""

import os
import select
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import ExitStack, closing, contextmanager
from pathlib import Path

from support.resolvers import bare_runtime_environment
from support.checkpoints import FifoCheckpoint

if os.name == "nt":
    import _winapi


class DiscoveryGate:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.executable = root / ("R.exe" if os.name == "nt" else "R")
        self.resources = ExitStack()
        self.connection: socket.socket | None = None
        self.process: int | None = None
        self.released = False
        self.retired = False
        if os.name == "nt":
            self.listener = self.resources.enter_context(closing(socket.socket()))
            self.listener.bind(("127.0.0.1", 0))
            self.listener.listen(1)
            self.listener.settimeout(10)
            (root / "address").write_text(f"127.0.0.1:{self.listener.getsockname()[1]}")
        else:
            # Restricted Linux sandboxes cannot connect to the host's TCP gate.
            # Only the descendant retains the FIFO writer after readiness, so
            # EOF still proves its retirement rather than just its parent's.
            lifetime = root / "lifetime"
            os.mkfifo(lifetime)
            self.lifetime = self.resources.enter_context(
                os.fdopen(
                    os.open(lifetime, os.O_RDONLY | os.O_NONBLOCK), "rb", buffering=0
                )
            )
            self.keeper = self.resources.enter_context(lifetime.open("wb", buffering=0))
            self.proceed = self.resources.enter_context(
                closing(FifoCheckpoint.create(root / "release"))
            )

    def wait(self, description: str) -> None:
        if os.name != "nt":
            assert select.select([self.lifetime], [], [], 10)[0], description
            assert len(self.lifetime.read(4)) == 4, description
            self.keeper.close()
            return
        assert self.connection is None, description
        self.connection, _ = self.listener.accept()
        self.connection.settimeout(5)
        identity = b""
        while len(identity) < 4:
            chunk = self.connection.recv(4 - len(identity))
            assert chunk, description
            identity += chunk
        # Pin the blocked descendant before release or cancellation. A killed
        # Windows process resets TCP, so socket EOF is not its exit receipt.
        self.process = _winapi.OpenProcess(
            _winapi.SYNCHRONIZE, False, int.from_bytes(identity, "big")
        )

    def release(self) -> None:
        if not self.released:
            if os.name == "nt":
                assert self.connection is not None
                self.connection.sendall(b"1")
            else:
                self.proceed.release()
            self.released = True

    def assert_retired(self) -> None:
        if self.retired:
            return
        if os.name == "nt":
            assert self.process is not None
            assert (
                _winapi.WaitForSingleObject(self.process, 5000) == _winapi.WAIT_OBJECT_0
            ), "discovery survived MCP closure"
        else:
            assert select.select([self.lifetime], [], [], 5)[0], (
                "discovery survived MCP closure"
            )
            assert self.lifetime.read(1) == b"", "discovery survived MCP closure"
        # macOS consumes FIFO readiness at EOF; retain the confirmed receipt.
        self.retired = True

    def close(self) -> None:
        if self.connection is not None:
            self.connection.close()
        if self.process is not None:
            _winapi.CloseHandle(self.process)
        self.resources.close()


@contextmanager
def discovery_environment(
    *, r_home: Path | None = None
) -> Iterator[tuple[dict[str, str], DiscoveryGate]]:
    with tempfile.TemporaryDirectory(prefix="console discovery ") as temporary:
        root = Path(temporary)
        (root / "failure").write_text("fixture R discovery failed")
        if r_home is not None:
            (root / "r-home").write_text(str(r_home), encoding="utf-8")
        with closing(DiscoveryGate(root)) as gate:
            subprocess.run(
                [
                    "rustc",
                    "--edition=2024",
                    str(
                        Path(__file__).resolve().parents[1]
                        / "fixtures/resolver/gated_r.rs"
                    ),
                    "-o",
                    str(gate.executable),
                ],
                check=True,
            )
            # Relocatable Unix Rscript launchers need these host utilities.
            if os.name != "nt":
                for name in ("sh", "dirname", "readlink"):
                    (root / name).symlink_to(shutil.which(name))
            environment = bare_runtime_environment(os.environ.copy(), root / "library")
            environment["R_PROFILE_USER"] = os.devnull
            environment.pop("R_HOME", None)
            environment["PATH"] = str(root)
            yield environment, gate
            gate.assert_retired()
