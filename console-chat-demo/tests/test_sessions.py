"""Acceptance checks against real sandboxed Console processes; no model calls."""

import asyncio
import base64
import contextlib
import os
import shutil
import signal
import sys
import threading
from pathlib import Path

import duckdb
import pytest
import pytest_asyncio
from chatlas.types import ContentImageInline

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from runtime import Pool

HERE = Path(__file__).resolve().parents[1]


def text(result) -> str:
    """Read the public chatlas content value without requiring a model turn."""
    assert result.error is None, result.error
    values = result.value if isinstance(result.value, list) else [result.value]
    return "\n".join(str(value) for value in values)


@pytest_asyncio.fixture
async def pool(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-must-not-reach-console")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://gateway.invalid/anthropic")
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
            sql="SELECT origin, count(*) AS departures FROM flights GROUP BY origin ORDER BY origin"
        )
    )
    assert "EWR" in result and "120835" in result and "LGA" in result
    # Every session's startup selects the same views.
    assert "26115" in text(
        await session.send(session="second", sql="SELECT count(*) AS n FROM weather")
    )
    assert (await session.send(requirements={"action": "get"})).error is None
    other = pool.slots[1] / "cache" / "private.csv"
    other.parent.mkdir()
    other.write_text("secret\nother-client-secret\n")
    denied = text(await session.send(sql=f"SELECT * FROM read_csv_auto('{other}')"))
    assert "other-client-secret" not in denied
    # DuckDB's file glob reports inaccessible paths as having no matches.
    assert "No files found that match the pattern" in denied


@pytest.mark.asyncio
async def test_sessions_share_read_only_flights_data(pool):
    session = await pool.open()
    # R and SQL share the startup connection, so R can query the views lazily.
    assert "carriers 16" in text(
        await session.send(
            r="cat('carriers', DBI::dbGetQuery(nyc, 'SELECT count(*) AS n FROM airlines')$n, '\\n')"
        )
    )
    output = text(
        await session.send(
            python="""
import errno
import pandas as pd
from pathlib import Path
print('planes', len(pd.read_parquet('nycflights13/planes.parquet')))
try:
    Path('nycflights13/flights.parquet').write_bytes(b'')
except OSError as error:
    assert error.errno in (errno.EACCES, errno.EPERM, errno.EROFS), error
    print('data-read-only')
"""
        )
    )
    assert "planes 3322" in output and "data-read-only" in output
    assert (HERE / "data/nycflights13/flights.parquet").stat().st_size > 1_000_000


@pytest.mark.asyncio
async def test_show_data_queries_original_tables_read_only(pool, tmp_path):
    session = await pool.open()
    show = next(t for t in session.chat.get_tools() if t.name == "show_data")
    result = await show.func(
        query="SELECT carrier, count(*) AS n FROM flights GROUP BY carrier ORDER BY n DESC;",
        title="Flights by carrier",
    )
    assert result.error is None, result.error
    assert result.extra["title"] == "Flights by carrier"
    assert result.extra["rows"] == 16
    assert list(result.extra["data"].columns) == ["carrier", "n"]
    assert "UA" in str(result.value) and "58665" in str(result.value)
    big = await show.func(query="FROM flights", title="All flights")
    assert big.extra["rows"] == 336776 and len(big.extra["data"]) == 5000
    # The queries run in the server process, outside Console's sandbox, so they
    # must not expose its environment or other files, or modify the source data.
    secret = tmp_path / "secret.csv"
    secret.write_text("x\nserver-secret\n")
    for query in (
        "SELECT getenv('ANTHROPIC_API_KEY') AS key",
        f"FROM read_csv('{secret}')",
        "DROP TABLE flights",
        "SET enable_external_access = true",
    ):
        with pytest.raises(duckdb.Error):
            await show.func(query=query, title="Denied")
    assert (await show.func(query="FROM planes", title="Planes")).extra["rows"] == 3322
    # Browsing must scan the same Parquet files, not copy their rows into a
    # second database. Only the five bundled files may be read by this tool.
    catalog = await show.func(
        query="SELECT table_name, table_type FROM information_schema.tables WHERE table_schema = 'main' ORDER BY table_name",
        title="Dataset views",
    )
    assert set(catalog.extra["data"]["table_type"]) == {"VIEW"}
    assert len(catalog.extra["data"]) == 5
    for query in (
        "SELECT 1); SET enable_external_access = true; SELECT (1",
        "SELECT 1); SELECT (2",
        f"FROM read_text('{secret}')",
    ):
        with pytest.raises((ValueError, duckdb.Error)):
            await show.func(query=query, title="Denied")


@pytest.mark.asyncio
async def test_follow_up_uses_least_recently_used_runtime(pool):
    session = await pool.open()
    suggest = next(t for t in session.chat.get_tools() if t.name == "suggest_next")
    await session.send(sql="SELECT 1")
    await suggest.func(language="R", question="Estimate uncertainty with a bootstrap?")
    await session.send(r="x <- 1")
    await session.send(r="x + 1")
    # Python has never run; returning to SQL would skip it.
    with pytest.raises(ValueError, match="Python"):
        await suggest.func(language="SQL", question="Compare carriers?")
    await suggest.func(language="Python", question="Model delays using weather?")
    assert session.suggestion[:2] == ("Python", "Model delays using weather?")
    await session.send(python="x = 1")
    assert session.suggestion is None
    with pytest.raises(ValueError, match="SQL"):
        await suggest.func(language="R", question="Estimate uncertainty?")
    await suggest.func(language="SQL", question="Join airlines to flights?")
    show = next(t for t in session.chat.get_tools() if t.name == "show_data")
    await show.func(query="FROM airlines", title="Airlines")
    await suggest.func(language="R", question="Estimate uncertainty?")


@pytest.mark.asyncio
async def test_follow_up_plots_alternate_every_second_suggestion(pool, monkeypatch):
    session = await pool.open()
    suggest = next(t for t in session.chat.get_tools() if t.name == "suggest_next")
    turns = iter(
        [
            ({"sql": "SELECT 1"}, "R", None),
            ({"r": "x <- 1"}, "Python", "Python"),
            ({"python": "x = 1"}, "SQL", None),
            ({"sql": "SELECT 2"}, "R", "R"),
            ({"r": "x + 1"}, "Python", None),
            ({"python": "x + 1"}, "SQL", "Python"),
        ]
    )

    # Replace only the external model stream; all code runs in real Console.
    async def model_stream(prompt, **kwargs):
        async def chunks():
            code, language, plot = next(turns)
            await session.send(**code)
            if plot:
                with pytest.raises(ValueError, match=f"plot.*{plot}"):
                    await suggest.func(language=language, question=prompt)
                other = "R" if plot == "Python" else "Python"
                with pytest.raises(ValueError, match=f"plot.*{plot}"):
                    await suggest.func(language=language, question=prompt, plot=other)
            # Replacing a suggestion within a turn must not advance the cadence.
            for _ in range(2):
                await suggest.func(language=language, question=prompt, plot=plot)
            yield "Analysis complete."

        return chunks()

    monkeypatch.setattr(session.chat, "stream_async", model_stream)
    for language, plot in (
        ("R", None),
        ("Python", "Python"),
        ("SQL", None),
        ("R", "R"),
        ("Python", None),
        ("SQL", "Python"),
    ):
        assert [chunk async for chunk in session.stream("Compare delays?")]
        assert session.suggestion == (language, "Compare delays?", plot)


@pytest.mark.asyncio
async def test_cancelling_table_query_interrupts_before_closing(pool, monkeypatch):
    session = await pool.open()
    show = next(t for t in session.chat.get_tools() if t.name == "show_data")
    started = asyncio.Event()
    released = threading.Event()
    finished = threading.Event()
    interrupted = threading.Event()
    loop = asyncio.get_running_loop()
    connection = pool.catalog.connection

    class GatedCursor:
        """Hold the external SQL operation at an explicit cancellation checkpoint."""

        def extract_statements(self, query):
            return connection.extract_statements(query)

        def execute(self, query):
            loop.call_soon_threadsafe(started.set)
            released.wait()
            finished.set()
            raise duckdb.InterruptException("query cancelled")

        def interrupt(self):
            interrupted.set()
            released.set()

        def close(self):
            assert finished.is_set(), "Closed while the query was still running"

    class QueryConnection:
        def cursor(self):
            return GatedCursor()

    with monkeypatch.context() as patch:
        patch.setattr(pool.catalog, "connection", QueryConnection())
        query = asyncio.create_task(show.func(query="SELECT 1", title="Waiting"))
        try:
            await asyncio.wait_for(started.wait(), timeout=5)
            query.cancel()
            with pytest.raises(asyncio.CancelledError):
                await query
            assert interrupted.is_set()
        finally:
            released.set()
            await asyncio.to_thread(finished.wait)


@pytest.mark.asyncio
async def test_report_renders_beside_the_data(pool, tmp_path):
    session = await pool.open()
    await session.send(note="**Question:** How many flights left each airport?")
    # Notes wait for code to give them a worker session to record into.
    assert session.reports() == {}
    await session.send(sql="SELECT origin, count(*) AS n FROM flights GROUP BY origin")
    await session.send(
        r="busiest <- DBI::dbGetQuery(nyc, 'SELECT max(n) AS n FROM (SELECT count(*) AS n FROM flights GROUP BY origin)')$n\ncat('busiest', busiest, '\\n')"
    )
    await session.send(python="print('python cell')")
    await session.send(note="EWR had the most departures.")
    report = session.reports()["analysis.qmd"]
    assert report.index("**Question:**") < report.index("GROUP BY origin")
    assert report.index("python cell") < report.index("EWR had the most")
    # The exported document recreates the startup connection from Parquet files
    # beside it, so its R and SQL chunks run outside Console.
    (tmp_path / "report").mkdir()
    (tmp_path / "report" / "nycflights13").symlink_to(HERE / "data" / "nycflights13")
    (tmp_path / "report" / "analysis.qmd").write_text(report)
    knit = await asyncio.create_subprocess_exec(
        "Rscript",
        "-e",
        "knitr::knit('analysis.qmd', quiet = TRUE)",
        cwd=tmp_path / "report",
        env=os.environ | {"RETICULATE_PYTHON": sys.executable},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await knit.communicate()
    assert knit.returncode == 0, stdout.decode()
    output = (tmp_path / "report" / "analysis.md").read_text()
    assert "120835" in output and "busiest 120835" in output, output
    assert output.count("python cell") == 2, output
    assert "Error" not in output, output


@pytest.mark.asyncio
async def test_sql_only_report_uses_r_setup(pool):
    session = await pool.open()
    await session.send(note="Count airlines")
    await session.send(sql="SELECT count(*) FROM airlines")
    assert "engine: knitr\n" in session.reports()["analysis.qmd"].split("---\n")[1]


@pytest.mark.asyncio
async def test_export_keeps_independent_sessions_separate(pool):
    session = await pool.open()
    await session.send(r="first_worker_only <- 42")
    await session.send(session="second", r="stopifnot(!exists('first_worker_only'))")
    reports = session.reports()
    assert len(reports) == 2
    assert "first_worker_only <- 42" in reports["analysis.qmd"]
    assert "stopifnot" not in reports["analysis.qmd"]
    assert "stopifnot" in reports["analysis-2.qmd"]
    assert "first_worker_only <- 42" not in reports["analysis-2.qmd"]


@pytest.mark.asyncio
async def test_duckdb_connections_do_not_ask_for_a_home(pool, tmp_path, monkeypatch):
    # Posit Connect's HOME has no ~/.duckdb, so an interactive R would ask
    # whether to create one. Keep the R packages visible from the new HOME.
    libraries = await asyncio.create_subprocess_exec(
        "Rscript", "-e", "cat(.libPaths(), sep = ':')", stdout=asyncio.subprocess.PIPE
    )
    stdout, _ = await libraries.communicate()
    monkeypatch.setenv("R_LIBS", stdout.decode())
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    session = await pool.open()
    output = text(
        await session.send(
            r="con <- DBI::dbConnect(duckdb::duckdb())\n"
            "cat('rows', DBI::dbGetQuery(nyc, 'SELECT count(*) AS n FROM planes')$n,\n"
            "  DBI::dbGetQuery(con, 'SELECT 1 AS n')$n, '\\n')"
        )
    )
    assert "rows 3322 1" in output, output
    assert not (home / ".duckdb").exists()


@pytest.mark.asyncio
async def test_prepared_environments_are_the_default(pool, tmp_path, monkeypatch):
    # Posit Connect restores the bundle's R packages into R_LIBS and its
    # Python requirements into the app's own environment.
    library = tmp_path / "connect-r-library"
    library.mkdir()
    monkeypatch.setenv("R_LIBS", str(library))
    session = await pool.open()
    assert f"prefix {sys.prefix}" in text(
        await session.send(python="import sys, shiny\nprint('prefix', sys.prefix)")
    )
    assert "library TRUE" in text(
        await session.send(
            r=f"cat('library', normalizePath('{library}') %in% .libPaths(), '\\n')"
        )
    )
    with pytest.raises(RuntimeError, match="managed Python requirements are disabled"):
        await session.send(requirements={"python": ["humanize"]})


@pytest.mark.asyncio
async def test_dynamic_resolution_prepares_managed_environments(pool):
    session = await pool.open(dynamic=True)
    assert (await session.send(requirements={"python": ["humanize"]})).error is None
    assert "12,345" in text(
        await session.send(python="import humanize\nprint(humanize.intcomma(12345))")
    )
    assert "app-only absent" in text(
        await session.send(
            python="import importlib.util\n"
            "print('app-only', 'absent' if importlib.util.find_spec('shiny') is None else 'present')"
        )
    )


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
assert not os.environ.get('ANTHROPIC_API_KEY')
assert not os.environ.get('ANTHROPIC_BASE_URL')
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
            r="stopifnot(Sys.getenv('ANTHROPIC_API_KEY') == ''); cat('r-key-absent\\n')"
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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-by-this-test")
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
async def test_explicit_r_executable_outside_path(tmp_path, monkeypatch):
    # Posit Connect installs R under /opt/R/<version> without adding it to PATH.
    r = shutil.which("R")
    assert r
    binary = os.environ.get("CONSOLE_CHAT_BINARY") or shutil.which("mcp-console")
    assert binary
    monkeypatch.setenv(
        "PATH",
        os.pathsep.join(
            entry
            for entry in os.environ["PATH"].split(os.pathsep)
            if not (Path(entry) / "R").exists()
        ),
    )
    pool = Pool(tmp_path / "pool", Path(binary), capacity=1, r=Path(r))
    try:
        session = await pool.open()
        assert "r-selected" in text(await session.send(r="cat('r-selected\\n')"))
    finally:
        await pool.close()


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
    monkeypatch.setenv("ANTHROPIC_API_KEY", "unused-by-this-test")
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
