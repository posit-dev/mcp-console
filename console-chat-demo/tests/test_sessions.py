"""Acceptance checks against real sandboxed Console processes; no model calls."""

import asyncio
import base64
import contextlib
import os
import shutil
import signal
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from chatlas.types import ContentImageInline

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Pool


def text(result) -> str:
    """Read the public chatlas content value without requiring a model turn."""
    assert result.error is None, result.error
    values = result.value if isinstance(result.value, list) else [result.value]
    return "\n".join(str(value) for value in values)


@pytest_asyncio.fixture
async def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "test-key-must-not-reach-console")
    private = tmp_path / "private.txt"
    private.write_text("private-server-data")
    binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
    assert binary, "Install mcp-console or set CONSOLE_CHAT_BINARY"
    instance = Pool(
        root=tmp_path / "pool",
        binary=Path(binary),
        capacity=2,
        private_paths=(private,),
    )
    try:
        yield instance
    finally:
        await instance.close()


@pytest.mark.asyncio
async def test_full_tool_surface_and_sql(pool):
    session = await pool.open()
    properties = session.tool.schema["function"]["parameters"]["properties"]
    assert {
        "r",
        "python",
        "sql",
        "session",
        "requirements",
        "stdin",
        "control",
        "note",
        "timeout_ms",
    } <= properties.keys()
    result = text(
        await session.send(
            sql="SELECT region, round(sum(revenue), 2) AS total FROM read_csv_auto('data.csv') GROUP BY region ORDER BY region"
        )
    )
    assert "East" in result and "171060" in result and "South" in result
    assert "42" in text(await session.send(session="second", sql="SELECT 42 AS answer"))
    assert (await session.send(requirements={"action": "get"})).error is None
    assert (await session.send(requirements={"python": ["numpy"]})).error is None
    other = pool.slots[1] / "cache" / "private.csv"
    other.parent.mkdir()
    other.write_text("secret\nother-client-secret\n")
    denied = text(await session.send(sql=f"SELECT * FROM read_csv_auto('{other}')"))
    assert "other-client-secret" not in denied
    # DuckDB's file glob reports inaccessible paths as having no matches.
    assert "No files found that match the pattern" in denied


@pytest.mark.asyncio
async def test_persistent_r_and_python(pool):
    session = await pool.open()
    assert "python-created" in text(
        await session.send(python="values = [3, 6, 9]\nprint('python-created')")
    )
    assert "python-total 18" in text(
        await session.send(python="print('python-total', sum(values))")
    )
    assert "r-created" in text(
        await session.send(r="r_values <- c(2, 4, 6); cat('r-created\\n')")
    )
    assert "r-total 12" in text(
        await session.send(r="cat('r-total', sum(r_values), '\\n')")
    )
    for source in (
        {"r": "plot(r_values, main = 'R sample plot')"},
        {"python": "import matplotlib.pyplot as plt\nplt.plot(values)\nplt.show()"},
    ):
        result = await session.send(**source)
        assert result.error is None, result.error
        values = result.value if isinstance(result.value, list) else [result.value]
        images = [item for item in values if isinstance(item, ContentImageInline)]
        if not images:
            pytest.fail(f"Console did not return image content: {text(result)}")
        assert images[0].image_content_type == "image/png"
        assert base64.b64decode(images[0].data).startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_concurrent_clients_and_slot_reuse(pool):
    first = await pool.open()
    await first.send(python="tenant_value = 'first-client'")
    # Open the second client after the first policy is already installed.
    second = await pool.open()
    await second.send(python="tenant_value = 'second-client'")
    with pytest.raises(RuntimeError, match="server is full"):
        await pool.open()
    pending = await first.send(
        python="""
from pathlib import Path
import time
Path('ready').write_text('ready')
while not Path('release').exists():
    time.sleep(0.01)
print('first-finished', tenant_value)
""",
        timeout_ms=0,
    )
    assert "running" in text(pending)
    async with asyncio.timeout(30):
        while not (first.workspace / "ready").exists():
            await asyncio.sleep(0.01)
    try:
        # Completion while the first client is gated proves concurrent progress.
        async with asyncio.timeout(30):
            output = text(await second.send(python="print(tenant_value)"))
        assert "second-client" in output
        assert not (first.workspace / "release").exists()
        foreign = first.workspace / "ready"
        output = text(
            await second.send(
                python=f"""
from pathlib import Path
import errno
try:
    Path({str(foreign)!r}).read_text()
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM), error
    print('peer-read-denied')
else:
    raise AssertionError('another client workspace was readable')
"""
            )
        )
        assert "peer-read-denied" in output
    finally:
        (first.workspace / "release").write_text("release")
    assert "first-finished first-client" in text(await first.send())
    # The earlier client's policy also protects a later client's files.
    later_file = second.workspace / "later.txt"
    later_file.write_text("later-client-data")
    output = text(
        await second.send(
            python="""
import tempfile
temporary_file = Path(tempfile.gettempdir()) / 'private-marker.txt'
temporary_file.write_text('private temporary data')
print('TEMP_PATH=' + str(temporary_file))
"""
        )
    )
    temporary_file = next(
        line.removeprefix("TEMP_PATH=")
        for line in output.splitlines()
        if line.startswith("TEMP_PATH=")
    )
    output = text(
        await first.send(
            python=f"""
for private_path in {[str(later_file), temporary_file]!r}:
    try:
        Path(private_path).read_text()
    except PermissionError:
        pass
    else:
        raise AssertionError('later client file was readable: ' + private_path)
print('later-peer-read-denied')
"""
        )
    )
    assert "later-peer-read-denied" in output
    previous_workspace = first.workspace
    await first.close()
    replacement = await pool.open()
    assert replacement.workspace == previous_workspace
    output = text(
        await replacement.send(
            python="""
from pathlib import Path
assert 'tenant_value' not in globals()
assert not Path('ready').exists()
assert not Path('release').exists()
print('replacement-is-fresh')
"""
        )
    )
    assert "replacement-is-fresh" in output
    assert "second-client" in text(await second.send(python="print(tenant_value)"))


@pytest.mark.asyncio
async def test_credentials_and_host_files_are_protected(pool, tmp_path):
    session = await pool.open()
    private = tmp_path / "private.txt"
    outside = tmp_path / "outside.txt"
    output = text(
        await session.send(
            python=f"""
import errno
import os
from pathlib import Path
assert not os.environ.get('OPENAI_API_KEY')
try:
    Path({str(private)!r}).read_text()
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM), error
else:
    raise AssertionError('private server file was readable')
try:
    Path({str(outside)!r}).write_text('unauthorized')
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
else:
    raise AssertionError('host file write succeeded')
print('secrets-and-host-protected')
"""
        )
    )
    assert "secrets-and-host-protected" in output
    assert not outside.exists()
    assert "r-key-absent" in text(
        await session.send(
            r="stopifnot(Sys.getenv('OPENAI_API_KEY') == ''); cat('r-key-absent\\n')"
        )
    )
    connected = asyncio.Event()

    def accept_connection(reader, writer):
        connected.set()
        writer.close()

    # A real listener distinguishes a policy denial from an unavailable service.
    async with await asyncio.start_server(accept_connection, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        output = text(
            await session.send(
                python=f"""
import socket
try:
    socket.create_connection(('127.0.0.1', {port}), timeout=3)
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM), error
    print('network-denied')
else:
    raise AssertionError('worker connected to the host network')
"""
            )
        )
        assert "network-denied" in output
        assert not connected.is_set()


@pytest.mark.asyncio
async def test_cancelled_startup_retires_before_releasing_slot(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-by-this-test")
    binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
    assert binary
    ready = tmp_path / "wrapper-ready"
    release = tmp_path / "release-startup"
    wrapper = tmp_path / "gated-console"
    wrapper.write_text(
        f"""#!{sys.executable}
import os
from pathlib import Path
import sys
import time
Path({str(ready)!r}).write_text(str(os.getpid()))
while not Path({str(release)!r}).exists():
    time.sleep(0.01)
os.execv({binary!r}, [{binary!r}, *sys.argv[1:]])
"""
    )
    wrapper.chmod(0o700)
    pool = Pool(tmp_path / "pool", wrapper, capacity=1)
    opening = asyncio.create_task(pool.open())
    pid = None
    try:
        async with asyncio.timeout(30):
            while not ready.exists():
                await asyncio.sleep(0.01)
        pid = int(ready.read_text())
        opening.cancel()
        # Startup cannot settle until the explicit release checkpoint. This
        # bounded observation detects early completion without releasing it.
        done, _ = await asyncio.wait({opening}, timeout=0.5)
        assert not done, (
            "Cancelled startup released its slot before MCP registration settled"
        )
        with pytest.raises(RuntimeError, match="server is full"):
            await pool.open()
        release.write_text("continue")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(opening, timeout=30)
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
        replacement = await pool.open()
        assert "fresh-after-cancel" in text(
            await replacement.send(python="print('fresh-after-cancel')")
        )
    finally:
        release.write_text("continue")
        with contextlib.suppress(asyncio.CancelledError, TimeoutError):
            await asyncio.wait_for(asyncio.shield(opening), timeout=30)
        await pool.close()
        # The red path exposes an unregistered background MCP process. Retire
        # that exact owned PID too, rather than leaving it after the assertion.
        if pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGTERM)
            async with asyncio.timeout(10):
                while True:
                    try:
                        os.kill(pid, 0)
                    except ProcessLookupError:
                        break
                    await asyncio.sleep(0.01)


@pytest.mark.asyncio
async def test_missing_private_path_is_rejected(tmp_path):
    binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
    assert binary
    pool = None
    try:
        with pytest.raises(FileNotFoundError):
            pool = Pool(
                tmp_path / "pool",
                Path(binary),
                capacity=1,
                private_paths=(tmp_path / "misspelled-secret-directory",),
            )
    finally:
        if pool is not None:
            await pool.close()


@pytest.mark.asyncio
async def test_capacity_reduction_keeps_previous_slot_private(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "unused-by-this-test")
    binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
    assert binary
    root = tmp_path / "pool"
    previous_workspace = root / "slots" / "7" / "workspace"
    previous_workspace.mkdir(parents=True)
    previous_file = previous_workspace / "private.txt"
    previous_file.write_text("previous-client-data")
    pool = Pool(root, Path(binary), capacity=1)
    try:
        session = await pool.open()
        result = await session.send(
            python=f"""
from pathlib import Path
try:
    Path({str(previous_file)!r}).read_text()
except PermissionError:
    print('previous-slot-protected')
else:
    raise AssertionError('previous client workspace was readable')
"""
        )
        assert "previous-slot-protected" in text(result)
        assert previous_file.read_text() == "previous-client-data"
    finally:
        await pool.close()
