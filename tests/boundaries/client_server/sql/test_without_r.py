"""Public MCP SQL coverage with no R executable visible to the local server."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager
from collections.abc import Iterator

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import NATIVE_FIXTURES, POSIX, SQL, requires
from support.assertions import last_result_text, last_tool_text
from support.checkpoints import FifoCheckpoint
from support.installation import installed_console
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.linux_sandbox import retain_system_bwrap
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code, normalize_python_resolution_error
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.snapshots import execution_snapshots
from support.resolvers import expose_uv, write_python_executable

UV_NAME = "uv.exe" if os.name == "nt" else "uv"


def environment(path: Path) -> dict[str, str]:
    retain_system_bwrap(path)
    search_path = [str(path)]
    if os.name == "nt":
        search_path.extend(
            [
                str(Path(sys.executable).parent),
                str(Path(os.environ["SystemRoot"]) / "System32"),
            ]
        )
    env = dict(
        os.environ, PATH=os.pathsep.join(search_path), TMPDIR=str(path.resolve())
    )
    for name in (
        "R_HOME",
        "R_LIBS",
        "R_LIBS_USER",
        "RETICULATE_PYTHON",
        "RETICULATE_UV",
    ):
        env.pop(name, None)
    return env


@contextmanager
def sql_client(
    binary: Path,
    execution: Execution,
    env: dict[str, str],
    current_directory: Path | None = None,
    *,
    record_in_project: bool = True,
    arguments: tuple[str, ...] = (),
) -> Iterator[McpClient]:
    with McpClient(
        installed_console(binary),
        execution.serve(*arguments),
        env,
        current_directory,
        record_in_project=record_in_project,
    ) as client:
        client.initialize_and_list_tools()
        yield client


def managed_environment(root: Path) -> dict[str, str]:
    (root / "home").mkdir()
    expose_uv(root)
    return dict(
        environment(root),
        MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(root / "home/.duckdb/extensions"),
        XDG_CACHE_HOME=str(root / "cache-base"),
    )


@requires(POSIX, SQL)
@executions(DIRECT)
def test_retrieves_native_duckdb_results_without_pandas(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        with sql_client(
            binary,
            execution,
            env,
            root,
            arguments=("-c", "python.managed.packages=[duckdb]"),
        ) as client:
            client.send(
                sql="""CREATE SEQUENCE executions;
                SELECT i, nextval('executions') AS executed,
                    9223372036854775807::BIGINT AS big,
                    12345678901234567890123456789.123456789::DECIMAL(38,9) AS exact,
                    NULL::INTEGER AS missing, repeat('x', 1000) AS long_value
                FROM range(57) AS t(i)"""
            )
            assert "additional rows omitted" in last_tool_text(client)
            client.expect(
                "native DuckDB values retrieved once without pandas\n",
                # fmt: python
                python=code("""
                    from decimal import Decimal
                    import sys

                    first = _console.sql_last_result(5)
                    assert first["columns"] == ("i", "executed", "big", "exact", "missing", "long_value")
                    second = _console.sql_last_result(19)
                    rest = _console.sql_last_result()
                    assert _console.sql_last_result()["rows"] == []
                    rows = first["rows"] + second["rows"] + rest["rows"]
                    assert [row[0] for row in rows] == list(range(57))
                    assert [row[1] for row in rows] == list(range(1, 58))
                    assert all(row[2] == 9223372036854775807 for row in rows)
                    assert all(row[3] == Decimal("12345678901234567890123456789.123456789") for row in rows)
                    assert all(row[4] is None and row[5] == "x" * 1000 for row in rows)
                    connection = _console.sql_connection()
                    assert connection.execute("SELECT currval('executions')").fetchone() == (57,)
                    assert "pandas" not in sys.modules
                    print("native DuckDB values retrieved once without pandas")
                    """),
            )
            client.send(sql="SELECT 42 AS answer")
            client.expect(
                "automatic cleanup keeps the managed catalog\n",
                # fmt: python
                python=code("""
                    result = _console.sql_last_result()
                    assert result == {"columns": ("answer",), "rows": [(42,)]}
                    assert _console.sql_connection() is connection
                    print("automatic cleanup keeps the managed catalog")
                    """),
            )
            return client.finish()


def extension_cache(root: Path, execution: Execution) -> Path:
    return (
        root / "home/.duckdb/extensions"
        if execution is DIRECT
        else root / "cache-base/mcp-console/dependencies/duckdb/extensions"
    )


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_duckdb_major_versions(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    transcripts = {}
    for version in ("1.5.6", "2.0.0"):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            env = managed_environment(root)
            with sql_client(
                binary,
                execution,
                env,
                root,
                arguments=("-c", f"python.managed.packages=[duckdb=={version}]"),
            ) as client:
                client.expect(
                    version + "\n",
                    python="import duckdb; print(duckdb.__version__)",
                )
                client.expect("[prepared]", requirements={"duckdb": ["fts", "parquet"]})
                client.send(
                    sql="CREATE TABLE documents AS SELECT 1 AS id, 'duckdb compatibility' AS body"
                )
                client.send(
                    sql=code("""
                    SET autoinstall_known_extensions = false;
                    LOAD fts; LOAD parquet;
                    PRAGMA create_fts_index('documents', 'id', 'body');
                    SELECT id, NULL AS missing FROM documents
                    WHERE fts_main_documents.match_bm25(id, 'compatibility') IS NOT NULL
                    """)
                )
                assert "Error:" not in last_tool_text(client), last_tool_text(client)
                client.expect(
                    "extensions and native results work\n",
                    # fmt: python
                    python=code("""
                        result = _console.sql_last_result()
                        assert result == {"columns": ("id", "missing"), "rows": [(1, None)]}
                        assert _console.sql_last_result()["rows"] == []
                        print("extensions and native results work")
                        """),
                )
                client.send(
                    sql=code("""
                    BEGIN;
                    CREATE TEMP TABLE transient AS SELECT 42 AS answer;
                    SELECT i FROM range(1000000) AS rows(i)
                    """)
                )
                assert "additional rows omitted" in last_tool_text(client)
                client.expect(
                    "discarding unread rows frees the connection\n",
                    # fmt: python
                    python=code("""
                        import sqlite3

                        managed = _console.sql_connection()
                        first = _console.sql_last_result(5)
                        assert first["rows"] == [(i,) for i in range(5)]
                        selected = sqlite3.connect(":memory:")
                        _console.sql_connection(selected)
                        assert managed.sql("SELECT answer FROM transient").fetchall() == [(42,)]
                        managed.execute("ROLLBACK")
                        assert managed.execute(
                            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'transient'"
                        ).fetchall() == [(0,)]
                        _console.sql_connection(managed)
                        print("discarding unread rows frees the connection")
                        """),
                )
                client.send(sql="SELECT 42 AS answer")
                client.expect(
                    "selected connection stays open\n",
                    # fmt: python
                    python=code("""
                        assert _console.sql_last_result() == {"columns": ("answer",), "rows": [(42,)]}
                        print("selected connection stays open")
                        """),
                )
                client.send(
                    sql=code("""
                    CREATE SEQUENCE writes;
                    BEGIN;
                    SELECT nextval('writes') AS written FROM range(10000)
                    """)
                )
                client.expect(
                    "discarding a write result completes its effects\n",
                    # fmt: python
                    python=code("""
                        _console.sql_connection(managed)
                        assert managed.sql("SELECT currval('writes')").fetchall() == [(10000,)]
                        managed.execute("COMMIT")
                        print("discarding a write result completes its effects")
                        """),
                )
                transcripts[version] = McpTranscript(client.finish())
    return TranscriptWithCompanions(
        transcripts["2.0.0"].transcript,
        {"duckdb-1.yaml": transcripts["1.5.6"]},
    )


# SANDBOXED selects the shared Seatbelt/bubblewrap fixtures. Native Windows
# sandbox coverage uses its separate acceptance suite.
@requires(POSIX, SQL)
@executions(SANDBOXED)
def test_prepares_builtin_extensions_without_downloads(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        uv = expose_uv(root).resolve()
        env = dict(
            environment(root),
            MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(root / "extensions"),
        )
        arguments = ("-c", "cache=host")
        # Prepare the ordinary dependencies before denying downloads. Managed
        # preparation removes UV_OFFLINE, so the fixture supplies --offline
        # through the real uv command rather than relying on cache freshness.
        with sql_client(binary, execution, env, root, arguments=arguments) as client:
            client.expect(
                "(42,)\n",
                python="_console.sql_connection().execute('SELECT 42').fetchone()",
            )
            client.finish()
        assert not list((root / "extensions").glob("**/json.duckdb_extension"))
        assert not list((root / "extensions").glob("**/parquet.duckdb_extension"))
        offline_uv = root / UV_NAME
        offline_uv.unlink()
        offline_receipt = root / "extensions/offline-uv-used"
        offline_receipt.parent.mkdir(parents=True, exist_ok=True)
        # R-free preparation selects uv from PATH and ignores RETICULATE_UV.
        # Replace the exposed link, delegating to the saved real executable.
        # Use its flag because Console removes ambient UV_OFFLINE.
        write_python_executable(
            offline_uv,
            # fmt: python
            code(f"""
                #!{sys.executable} -S
                import os
                import sys
                from pathlib import Path

                Path({str(offline_receipt)!r}).touch()
                os.execv({str(uv)!r}, [{str(uv)!r}, "--offline", *sys.argv[1:]])
                """),
        )
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {"resolver": {"sandbox": {"network": {"proxy": {"domains": {}}}}}}
            )
        )
        with sql_client(binary, execution, env, root, arguments=arguments) as client:
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    retained = 42
                    retained
                    """),
            )
            client.expect("[prepared]", requirements={"duckdb": ["parquet"]})
            assert not list((root / "extensions").glob("**/parquet.duckdb_extension"))
            client.expect(
                "('42',)\n",
                python="_console.sql_connection().execute('SELECT ?::JSON', [retained]).fetchone()",
            )
            assert not list((root / "extensions").glob("**/json.duckdb_extension"))
            assert offline_receipt.is_file(), (
                "R-free preparation did not use the offline uv wrapper"
            )
            return [
                {
                    "scenario": (
                        "Prepare a built-in DuckDB extension while downloads are "
                        "denied, preserving Python state."
                    ),
                    "fixture": (
                        "A private PATH hides R. An earlier session prepares the real "
                        "Python environment; this session wraps real uv with "
                        "--offline and denies every resolver proxy domain. The "
                        "json and parquet extensions are not cached, so success "
                        "cannot depend on downloading either extension."
                    ),
                }
            ] + client.finish()


@requires(POSIX, SQL)
@executions(DIRECT)
def test_managed_python_requires_home_for_default_extensions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        env = dict(environment(root), UV_CACHE_DIR=str(root / "uv-cache"))
        env.pop("HOME", None)
        env.pop("XDG_CACHE_HOME", None)
        with sql_client(
            binary,
            execution,
            env,
            current_directory=root,
            record_in_project=False,
            arguments=("-c", "cache=host"),
        ) as client:
            failure = client.send(
                sql="SELECT 42 AS answer",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert failure.get("isError"), failure
            diagnostic = (
                "resolver.environment: preparation requires an absolute HOME or "
                "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY; configure one when "
                "inherit_environment is false"
                if execution is SANDBOXED
                else "DuckDB extension preparation requires an absolute HOME at server startup"
            )
            assert last_result_text(client) == diagnostic, failure
            assert client.send(requirements={"action": "get"}) == failure
            client.request("ping")
            transcript, errors = client.finish_with_standard_error(
                expected_exit_status=1
            )
            assert errors == diagnostic + "\n", errors
            return transcript[3:] + [{"stderr": errors}]


@requires(SQL)
@executions(SANDBOXED)
def test_resolver_requires_home_for_default_extensions(
    binary: Path, execution: Execution
) -> Transcript:
    return test_managed_python_requires_home_for_default_extensions(binary, execution)


@requires(SQL)
@executions(DIRECT)
@requires(NATIVE_FIXTURES)
def test_default_extension_failure_preserves_close_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        marker = root / "closing"
        env = dict(
            environment(root),
            UV_CACHE_DIR=str(root / "uv-cache"),
            MCP_CONSOLE_TEST_CLOSE_MARKER=str(marker),
        )
        env[LOADER_VARIABLE] = str(build_interposer(root, "preparation_close_failure"))
        # Fail extension preparation after Python selection, then inject a Close failure.
        env.pop("HOME", None)
        if execution is SANDBOXED:
            cache = root / "invalid-cache"
            cache.write_text("not a directory")
            config = root / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text(
                json.dumps(
                    {
                        "resolver": {
                            "environment": {
                                "HOME": os.environ["HOME"],
                                "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY": str(cache),
                                LOADER_VARIABLE: env[LOADER_VARIABLE],
                            },
                            "sandbox": {
                                "filesystem": {
                                    "read_only": ["/"],
                                    "read_write": [str(root)],
                                }
                            },
                        }
                    }
                )
            )
        with sql_client(
            binary,
            execution,
            env,
            current_directory=root,
            record_in_project=False,
            arguments=("-c", "cache=host"),
        ) as client:
            failure = client.send(
                sql="SELECT 42 AS answer",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            if execution is SANDBOXED:
                # A clean default-extension failure is recoverable. The owner
                # remains live until shutdown, where its Close still must fail.
                assert not failure.get("isError"), failure
                diagnostic = last_result_text(client)
                assert diagnostic.startswith(
                    "skipping default DuckDB extension `sqlite`: DuckDB extension resolution failed"
                ), diagnostic
                assert (
                    f'Failed to create directory "{cache}": File exists' in diagnostic
                ), diagnostic
                assert diagnostic.endswith("answer\n------\n42\n"), diagnostic
                transcript, errors = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                assert marker.exists(), ("resolver did not receive Close", errors)
                assert errors == (
                    "resolver input closed; local resolver sandbox exited with exit status: 47\n"
                ), errors
                records = transcript[3:] + [{"stderr": errors}]
                return json.loads(json.dumps(records).replace(str(root), "<temporary>"))
            assert failure.get("isError"), failure
            diagnostic = (
                "DuckDB extension preparation requires an absolute HOME at server startup; "
                "resolver input closed"
            )
            assert last_result_text(client) == diagnostic, failure
            assert marker.exists(), ("resolver did not receive Close", diagnostic)
            assert client.send(requirements={"action": "get"}) == failure
            client.request("ping")
            transcript, errors = client.finish_with_standard_error(
                expected_exit_status=1
            )
            assert errors == diagnostic + "\n", errors
            records = transcript[3:] + [{"stderr": errors}]
            return json.loads(json.dumps(records).replace(str(root), "<temporary>"))


@requires(SQL)
@executions(SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_invalid_extension_cache_preserves_close_failure(
    binary: Path, execution: Execution
) -> Transcript:
    return test_default_extension_failure_preserves_close_failure(binary, execution)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
@execution_snapshots
def test_prepares_extension_before_first_worker_and_loads_from_cache(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        host_temp = root / "host-temp"
        host_temp.mkdir()
        env["TMPDIR"] = str(host_temp)
        cache = extension_cache(root, execution)
        assert not cache.exists()
        shadow = root / "duckdb.py"
        shadow.write_text(
            "raise RuntimeError('workspace DuckDB shadow was imported')\n"
        )
        env["PYTHONPATH"] = str(root)
        if execution == DIRECT:
            env[LOADER_VARIABLE] = str(build_interposer(root, "deny_worker_connect"))
            env["MCP_CONSOLE_TEST_DENY_WORKER_NETWORK"] = "1"
        with sql_client(binary, execution, env) as client:
            client.expect("[prepared]", requirements={"duckdb": ["fts"]})
            (extension,) = cache.glob("v*/**/fts.duckdb_extension")
            shadow.unlink()
            client.send(
                # fmt: python
                python=code("""
                    import errno
                    import socket

                    try:
                        # Linux may deny socket creation before connect is reached.
                        with socket.socket() as probe:
                            probe.settimeout(1)
                            probe.connect(("203.0.113.1", 443))
                    except OSError as error:
                        assert error.errno in (errno.EACCES, errno.EPERM), error
                    else:
                        raise AssertionError("worker network connection succeeded")
                    print("worker network denied")
                    """)
            )
            assert last_tool_text(client) == "worker network denied\n", last_tool_text(
                client
            )
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                sql="SELECT installed, loaded FROM duckdb_extensions() WHERE extension_name = 'fts'"
            )
            assert "true" in last_tool_text(client).lower()
            client.send(python="import os; print(os.environ['TMPDIR'])")
            first_temporary = Path(last_tool_text(client).strip())
            (root / UV_NAME).unlink()
            client.send(
                control="restart",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert "Error:" not in last_tool_text(client)
            assert first_temporary.exists() is (execution is DIRECT)
            assert extension.is_file(), "restart removed the shared extension cache"
            client.send(python="import os; print(os.environ['TMPDIR'])")
            second_temporary = Path(last_tool_text(client).strip())
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            assert second_temporary.exists() is (execution is DIRECT)
            assert extension.is_file(), (
                "crash replacement removed the shared extension cache"
            )
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "fts",
                "icu",
                "json",
                "sqlite",
            ]
            return _replace_paths(
                client.finish()[3:], [first_temporary, second_temporary]
            )


@requires(SQL)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_adds_extensions_to_idle_worker_without_losing_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        cache = extension_cache(root, execution)
        if execution == DIRECT:
            env[LOADER_VARIABLE] = str(build_interposer(root, "deny_worker_connect"))
            env["MCP_CONSOLE_TEST_DENY_WORKER_NETWORK"] = "1"
        with sql_client(binary, execution, env) as client:
            client.expect(
                "Count\n-----\n1\n",
                sql="CREATE TABLE retained AS SELECT 42 AS value",
            )
            client.send(
                # fmt: python
                python=code("""
                    import os
                    import sqlite3
                    import sys

                    identity = object()
                    identity_id = id(identity)
                    worker_pid = os.getpid()
                    interpreter = sys.executable
                    managed = _console.sql_connection()
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (17)")
                    _console.sql_connection(selected)
                    print("state ready")
                    """)
            )
            assert last_tool_text(client) == "state ready\n"
            client.expect(
                "[prepared]", requirements={"duckdb": ["fts"], "python": ["duckdb"]}
            )
            assert len(list(cache.glob("v*/**/fts.duckdb_extension"))) == 1
            client.send(
                # fmt: python
                python=code("""
                    assert os.getpid() == worker_pid
                    assert sys.executable == interpreter
                    assert id(identity) == identity_id
                    assert _console.sql_connection() is selected
                    assert managed.execute("SELECT value FROM retained").fetchone() == (42,)
                    assert selected.execute("SELECT value FROM chosen").fetchone() == (17,)
                    _console.sql_connection(None)
                    assert _console.sql_connection() is managed
                    print("state retained")
                    """)
            )
            assert last_tool_text(client) == "state retained\n"
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                requirements={"duckdb": ["excel"]},
                sql="SET autoinstall_known_extensions = false; LOAD excel; SELECT value FROM retained",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert "42" in last_tool_text(client)
            client.expect(
                "Python cell retained\n",
                requirements={"duckdb": ["parquet"]},
                python="assert os.getpid() == worker_pid and id(identity) == identity_id; print('Python cell retained')",
            )
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "excel",
                "fts",
                "icu",
                "json",
                "parquet",
                "sqlite",
            ]
            client.send(
                control="restart",
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert "Error:" not in last_tool_text(client)
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD excel")
            assert "Error:" not in last_tool_text(client)
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_combines_python_and_extension_candidates_across_duckdb_versions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        cache = extension_cache(root, execution)
        with sql_client(binary, execution, env) as client:
            client.expect(
                "first candidate\n",
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.4.4", "six"],
                    "duckdb": ["fts"],
                },
                python="import duckdb, six; assert duckdb.__version__ == '1.4.4'; print('first candidate')",
            )
            assert len(list(cache.glob("v1.4.4/**/fts.duckdb_extension"))) == 1
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"] == {
                "r": [],
                "python": ["duckdb==1.4.4", "six"],
                "duckdb": ["fts"],
                "python_version": [],
                "exclude_newer": None,
            }
            second = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.6", "six"],
                    "duckdb": ["fts"],
                },
                sql="SET autoinstall_known_extensions = false; LOAD fts",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not second.get("isError"), second
            assert "Error:" not in last_tool_text(client)
            assert len(list(cache.glob("v1.5.6/**/fts.duckdb_extension"))) == 1
            client.send(python="import duckdb, six; print(duckdb.__version__)")
            assert last_tool_text(client) == "1.5.6\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["six"], "duckdb": []},
                python="sentinel = object(); original = sentinel",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            added = client.send(
                requirements={"python": ["duckdb==1.5.6"], "duckdb": ["fts"]},
                sql="SET autoinstall_known_extensions = false; LOAD fts",
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert not added.get("isError"), added
            assert "Error:" not in last_tool_text(client), client.transcript[-1]
            client.send(
                python="assert sentinel is original; print('candidate prepared together')"
            )
            assert last_tool_text(client) == "candidate prepared together\n"
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_combined_preparation_precedes_first_sql_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        cache = extension_cache(root, execution)
        with sql_client(binary, execution, env) as client:
            result = client.send(
                requirements={"python": ["six"], "duckdb": ["fts"]},
                sql="SET autoinstall_known_extensions = false; LOAD fts",
            )
            assert not result.get("isError"), result
            assert "Error:" not in last_tool_text(client)
            client.send(python="import six; print('combined SQL preparation')")
            assert last_tool_text(client) == "combined SQL preparation\n"
            assert len(list(cache.glob("v*/**/fts.duckdb_extension"))) == 1
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_extension_actions_replace_and_reset_declarations(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        cache = extension_cache(root, execution)
        with sql_client(binary, execution, env) as client:

            def declaration():
                inspected = client.send(
                    requirements={"action": "get"},
                    timeout_ms=int(client.response_timeout * 1_000),
                )
                assert not inspected.get("isError"), inspected
                return inspected["structuredContent"]["requirements"]

            assert declaration()["duckdb"] == ["icu", "json", "sqlite"]
            client.expect("[prepared]", requirements={"duckdb": ["fts"]})
            assert declaration()["duckdb"] == ["fts", "icu", "json", "sqlite"]
            client.expect("[prepared]", requirements={"duckdb": ["parquet"]})
            assert declaration()["duckdb"] == [
                "fts",
                "icu",
                "json",
                "parquet",
                "sqlite",
            ]
            client.expect(
                "[prepared]",
                requirements={
                    "action": "set",
                    "python": ["duckdb"],
                    "duckdb": ["fts"],
                },
            )
            assert declaration()["python"] == ["duckdb"]
            assert declaration()["duckdb"] == ["fts"]
            (extension,) = cache.glob("v*/**/fts.duckdb_extension")
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["duckdb"]},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert declaration()["duckdb"] == []
            assert extension.is_file(), (
                "removing a declaration uninstalled the extension"
            )
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                control="restart",
                requirements={"action": "reset"},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            assert declaration()["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
            ]
            assert declaration()["duckdb"] == ["icu", "json", "sqlite"]
            return client.finish()[3:]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_failed_and_live_extension_changes_preserve_worker_and_selected_connection(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        uv_cache = root / "uv-cache"
        with sql_client(
            binary,
            execution,
            dict(env, UV_CACHE_DIR=str(uv_cache), RUST_LOG="error"),
            arguments=("-c", "cache=host"),
        ) as client:
            client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.6"],
                    "duckdb": ["fts"],
                }
            )
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(
                # fmt: python
                python=code("""
                    import sqlite3

                    identity = object()
                    identity_id = id(identity)
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (17)")
                    _console.sql_connection(selected)
                    print("selected SQLite")
                    """)
            )
            assert last_tool_text(client) == "selected SQLite\n"
            client.send(requirements={"duckdb": ["fts"]})
            client.send(sql="SELECT value FROM chosen")
            assert "17" in last_tool_text(client)
            failed = client.send(
                requirements={"duckdb": ["not_a_real_duckdb_extension"]},
                stdin="retained input\n",
                sql="DROP TABLE retained",
            )
            assert failed.get("isError"), failed
            failure = last_result_text(client)
            assert "not_a_real_duckdb_extension" in failure
            failure = re.sub(
                r"https://duckdb\.org/docs/stable/extensions/troubleshooting\?\S+",
                "<DuckDB extension troubleshooting URL>",
                failure,
            )
            failed["content"][0]["text"] = re.sub(
                r'https?://[^"\s]+', "<DuckDB extension URL>", failure
            )
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(sql="SELECT value FROM chosen")
            assert "17" in last_tool_text(client)
            client.send(python="assert id(identity) == identity_id; print(input())")
            assert "retained input" not in last_tool_text(client)
            assert "[waiting for stdin]" in last_tool_text(client)
            # Enqueuing stdin can return before Python consumes it.
            client.expect("fresh input\n", stdin="fresh input\n")
            client.send(python="_console.sql_connection(None)")
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            missing = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "python": ["six"],
                    "duckdb": ["fts"],
                },
                python="identity = None",
            )
            assert missing.get("isError"), missing
            assert "include duckdb in requirements.python" in last_result_text(client)
            client.send(
                python="assert id(identity) == identity_id; print('old worker intact')"
            )
            assert last_tool_text(client) == "old worker intact\n"
            live = client.send(
                requirements={
                    "duckdb": ["json"],
                    "python": ["absent-fixture-distribution"],
                },
                python="identity = None",
            )
            assert live.get("isError"), live
            assert "absent-fixture-distribution" in last_result_text(client)
            live["content"][0]["text"], paths = re.subn(
                rf'"python": "{re.escape(str(uv_cache))}/archive-v0/[^"]+/bin/python"',
                '"python": "<selected Python>"',
                last_result_text(client),
                count=1,
            )
            assert paths == 1, last_result_text(client)
            for requirements in (
                {"duckdb": ["json"], "python_version": [">=3.11"]},
                {"duckdb": ["json"], "exclude_newer": "2026-01-01"},
                {
                    "action": "set",
                    "python": ["duckdb==1.5.6"],
                    "duckdb": ["fts", "json"],
                },
                {"action": "reset"},
            ):
                denied = client.send(requirements=requirements)
                assert denied.get("isError"), denied
                assert 'control="restart"' in last_result_text(client)
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == ["fts"]
            client.send(
                python="assert id(identity) == identity_id; print('still intact')"
            )
            assert last_tool_text(client) == "still intact\n"
            unchanged = client.send(
                requirements={
                    "action": "set",
                    "python": ["duckdb==1.5.6"],
                    "duckdb": ["fts"],
                }
            )
            assert not unchanged.get("isError"), unchanged
            client.send(
                requirements={"duckdb": ["fts"]},
                python="assert id(identity) == identity_id; print('no-op')",
            )
            assert last_tool_text(client) == "no-op\n"
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_live_extension_additions_require_an_idle_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        with sql_client(binary, execution, env) as client:
            client.send(requirements={"duckdb": ["json"]})
            client.send(
                python="identity = object(); identity_id = id(identity); answer = input('answer> ')",
            )
            assert "[waiting for stdin]" in last_tool_text(client)
            busy = client.send(
                requirements={"duckdb": ["fts"]},
                python="identity = None",
            )
            assert busy.get("isError"), busy
            assert "already evaluating a cell" in last_result_text(client)
            retained = client.send(requirements={"duckdb": ["json"]})
            assert not retained.get("isError"), retained
            # Enqueuing stdin can return before Python consumes it.
            client.expect(stdin="kept\n")
            client.send(
                python="assert id(identity) == identity_id and answer == 'kept'; print('input retained')"
            )
            assert last_tool_text(client) == "input retained\n"

            client.send(
                python="import pdb; pdb.set_trace(); print('debugger continued')"
            )
            assert "[waiting for stdin]" in last_tool_text(client)
            busy = client.send(requirements={"duckdb": ["fts"]})
            assert busy.get("isError"), busy
            assert "already evaluating a cell" in last_result_text(client)
            denied = client.send(
                control="interrupt",
                requirements={"duckdb": ["json"]},
                python="identity = None",
            )
            assert denied.get("isError"), denied
            client.expect("debugger continued\n", stdin="continue\n")
            assert "debugger continued" in last_result_text(client)
            client.send(
                python="assert id(identity) == identity_id; print('still live')"
            )
            assert last_tool_text(client) == "still live\n"
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                "icu",
                "json",
                "sqlite",
            ]
            return client.finish()[3:]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_interrupts_extension_preparation_before_worker_retirement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        env = managed_environment(root)
        checkpoints = root / "uv-cache"
        checkpoints.mkdir()
        started = FifoCheckpoint.create(checkpoints / "started")
        release = FifoCheckpoint.create(checkpoints / "release")
        env = dict(
            env,
            UV_CACHE_DIR=str(checkpoints),
            MCP_CONSOLE_TEST_DUCKDB_INTERRUPT_ROOT=str(checkpoints),
        )
        try:
            with sql_client(
                binary, execution, env, arguments=("-c", "cache=host")
            ) as client:
                client.expect(
                    "Count\n-----\n1\n",
                    sql="CREATE TABLE retained AS SELECT 42 AS value",
                )
                client.send(
                    python="import os, sysconfig; site = sysconfig.get_paths()['purelib']; identity = object(); identity_id = id(identity); pid = os.getpid(); print(site)"
                )
                site = Path(last_tool_text(client).strip())
                hook = site / "sitecustomize.py"
                hook.write_text(
                    code("""
                        import os
                        import signal
                        import sys
                        from pathlib import Path

                        if sys.flags.isolated:
                            signal.signal(signal.SIGINT, lambda *_: os._exit(130))
                            root = Path(os.environ["MCP_CONSOLE_TEST_DUCKDB_INTERRUPT_ROOT"])
                            with (root / "started").open("wb", buffering=0) as marker:
                                marker.write(b"1")
                            with (root / "release").open("rb", buffering=0) as gate:
                                gate.read(1)
                        """)
                )
                try:
                    pending = client.start_send(
                        requirements={"duckdb": ["fts"]},
                        sql="DROP TABLE retained",
                    )
                    started.wait("Python DuckDB resolver entered")
                    inspected = client.send(requirements={"action": "get"})
                    assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                        "icu",
                        "json",
                        "sqlite",
                    ]
                    interrupt = client.start_send(control="interrupt")
                    client.receive_many([pending, interrupt])
                    assert pending["result"].get("isError"), pending
                    assert "exit status: 130" in str(pending["result"]), pending
                    assert not interrupt["result"].get("isError"), interrupt
                finally:
                    release.release()
                    hook.unlink()
                inspected = client.send(requirements={"action": "get"})
                assert inspected["structuredContent"]["requirements"]["duckdb"] == [
                    "icu",
                    "json",
                    "sqlite",
                ]
                client.send(sql="SELECT value FROM retained")
                assert "42" in last_tool_text(client)
                client.send(
                    python="assert id(identity) == identity_id and os.getpid() == pid; print('still live')"
                )
                assert last_tool_text(client) == "still live\n"
                records = client.finish()[3:]
                for record in records:
                    for content in record.get("result", {}).get("content", []):
                        if content.get("type") == "text":
                            text = content["text"].replace(str(root), "<fixture>")
                            content["text"] = re.sub(
                                r"<fixture>/uv-cache/archive-v0/[^/]+",
                                "<managed Python>",
                                text,
                            )
                return records
        finally:
            started.close()
            release.close()


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_sql_is_the_first_cell(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        with sql_client(binary, execution, environment(root)) as client:
            result = client.send(sql="SELECT 42 AS answer")
            assert not result.get("isError"), result
            assert "42" in last_tool_text(client)
            client.send(sql="CREATE TABLE retained AS SELECT 7 AS value")
            assert "Error:" not in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import pandas as pd

                    managed = _console.sql_connection()
                    assert managed.execute("SELECT value FROM retained").fetchall() == [(7,)]
                    frame = pd.DataFrame({"value": [3, 4]})
                    managed.register("registered", frame)
                    unregistered = pd.DataFrame({"value": [99]})
                    print("shared connection")
                    """)
            )
            assert last_tool_text(client) == "shared connection\n", last_tool_text(
                client
            )
            client.send(sql="SELECT sum(value) AS total FROM registered")
            assert "7" in last_tool_text(client)
            client.send(sql="SELECT * FROM unregistered")
            assert "Error:" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import builtins
                    import sqlite3

                    assert "sql_connection" not in vars(builtins)
                    assert "console_sql_connection" not in vars(builtins)
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE chosen(value INTEGER)")
                    selected.execute("INSERT INTO chosen VALUES (11)")
                    assert _console.sql_connection(connection=selected) is None
                    assert _console.sql_connection() is selected
                    print("selected SQLite")
                    """)
            )
            assert last_tool_text(client) == "selected SQLite\n"
            client.send(sql="SELECT value FROM chosen")
            assert "11" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    assert _console.sql_connection(None) is None
                    assert _console.sql_connection() is managed
                    assert selected.execute("SELECT value FROM chosen").fetchone() == (11,)
                    print("managed restored; SQLite remains open")
                    """)
            )
            assert last_tool_text(client) == "managed restored; SQLite remains open\n"
            client.send(sql="SELECT value FROM retained")
            assert "7" in last_tool_text(client)
            client.send(sql="SELECT i FROM range(100) AS values(i)")
            preview = last_tool_text(client)
            assert "[additional rows omitted]" in preview
            assert len(preview.encode()) < 12 * 1024
            client.send(sql="SELECT * FROM missing_table")
            assert "Error:" in last_tool_text(client)
            client.send(
                python="managed.execute('SELECT value FROM retained').fetchone()"
            )
            assert last_tool_text(client) == "(7,)\n"
            client.send(
                # fmt: python
                python=code("""
                    _console.sql_connection(selected)
                    try:
                        _console.sql_connection(object())
                    except TypeError as error:
                        print(error)
                    assert _console.sql_connection() is selected
                    selected.close()
                    """)
            )
            assert last_tool_text(client) == (
                "`connection` must provide a callable cursor() method or be None\n"
            )
            client.send(sql="SELECT value FROM chosen")
            assert (
                last_tool_text(client)
                == "Error: Cannot operate on a closed database.\n"
            )
            client.send(
                python="assert _console.sql_connection() is selected; _console.sql_connection(None)"
            )
            assert last_tool_text(client) == "[done]"
            client.send(
                python="del managed, selected; assert _console.sql_connection().execute('SELECT value FROM retained').fetchone() == (7,)"
            )
            assert last_tool_text(client) == "[done]"
            client.send(python="_console.sql_connection(sqlite3.connect(':memory:'))")
            assert last_tool_text(client) == "[done]"
            client.send(
                control="restart", python="assert callable(_console.sql_connection)"
            )
            assert last_tool_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[done]"
            ), last_tool_text(client)
            client.send(sql="SELECT value FROM retained")
            assert "Table with name retained does not exist" in last_tool_text(client)
            return client.finish()[3:]


def _replace_paths(records: Transcript, paths: list[Path]) -> Transcript:
    for record in records:
        for content in record.get("result", {}).get("content", []):
            if content.get("type") == "text":
                for index, path in enumerate(paths):
                    content["text"] = content["text"].replace(
                        str(path), f"<worker temporary {index + 1}>"
                    )
    return records


@requires(SQL)
@executions(SANDBOXED)
def test_catalog_and_private_storage_follow_worker_lifetime(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        with sql_client(binary, execution, environment(root)) as client:
            client.send(
                # fmt: python
                python=code("""
                    import os, sys
                    from pathlib import Path

                    first_temporary = Path(os.environ["TMPDIR"])
                    assert "duckdb" in sys.modules
                    assert not (first_temporary / "mcp-console-duckdb").exists()
                    print(first_temporary)
                    """)
            )
            first_temporary = Path(last_tool_text(client).strip())
            client.send(sql="CREATE TABLE state AS SELECT 41 AS value")
            client.send(
                # fmt: python
                python=code("""
                    connection = _console.sql_connection()
                    spill = Path(
                        connection.execute("SELECT current_setting('temp_directory')").fetchone()[0]
                    )
                    secrets = Path(
                        connection.execute("SELECT current_setting('secret_directory')").fetchone()[0]
                    )
                    assert spill == first_temporary / "mcp-console-duckdb/spill"
                    assert secrets == first_temporary / "mcp-console-duckdb/stored-secrets"
                    assert connection.execute(
                        "SELECT current_setting('python_enable_replacements')"
                    ).fetchone() == (False,)
                    spill.mkdir(parents=True)
                    secrets.mkdir(parents=True)
                    (spill / "retired").touch()
                    (secrets / "retired").touch()
                    print("private DuckDB storage")
                    """)
            )
            assert last_tool_text(client) == "private DuckDB storage\n"
            client.send(control="restart", sql="SELECT * FROM state")
            assert "Error:" in last_tool_text(client)
            assert not first_temporary.exists(), "retired worker storage remains"
            client.send(
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    second_temporary = Path(os.environ["TMPDIR"])
                    assert second_temporary != first_temporary
                    print(second_temporary)
                    """)
            )
            second_temporary = Path(last_tool_text(client).strip())
            client.send(sql="CREATE TABLE crash_state AS SELECT 42 AS value")
            client.send(python="import os; os._exit(47)")
            assert "status 47" in last_result_text(client)
            client.send(sql="SELECT * FROM crash_state")
            assert "Error:" in last_tool_text(client)
            assert not second_temporary.exists(), "crashed worker storage remains"
            client.send(python="import os; print(os.environ['TMPDIR'])")
            third_temporary = Path(last_tool_text(client).strip())
            records = client.finish()[3:]
            assert not third_temporary.exists(), "shutdown worker storage remains"
            return _replace_paths(
                records, [first_temporary, second_temporary, third_temporary]
            )


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_requirements_and_missing_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        uv = root / "uv"
        uv.write_text(
            """#!/bin/sh
for argument do
    if [ "$argument" = py-yaml12 ]; then
        echo 'fixture Python resolution failed' >&2
        exit 81
    fi
done
exec "$MCP_CONSOLE_TEST_REAL_UV" "$@"
"""
        )
        uv.chmod(0o755)
        env = dict(environment(root), MCP_CONSOLE_TEST_REAL_UV=shutil.which("uv"))
        with sql_client(binary, execution, env) as client:

            def declaration():
                result = client.send(requirements={"action": "get"})
                assert not result.get("isError"), result
                return result["structuredContent"]["requirements"]["python"]

            assert declaration() == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
            ]
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(python="identity = object(); identity_id = id(identity)")
            failed = client.send(
                control="restart",
                requirements={"python": ["py-yaml12"]},
                sql="DROP TABLE retained",
            )
            assert failed.get("isError")
            assert "fixture Python resolution failed" in last_result_text(client)
            failed["content"][0]["text"] = normalize_python_resolution_error(
                failed["content"][0]["text"], "fixture Python resolution failed"
            )
            assert declaration() == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
            ]
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            client.send(python="assert id(identity) == identity_id; print('retained')")
            assert last_tool_text(client) == "retained\n"

            client.send(control="restart", requirements={"action": "set"})
            assert declaration() == []
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sqlite3

                    assert importlib.util.find_spec("duckdb") is None
                    selected = sqlite3.connect(":memory:")
                    _console.sql_connection(selected)
                    print("Python remains available")
                    """)
            )
            assert last_tool_text(client) == "Python remains available\n"
            client.send(sql="SELECT 7 AS custom_value")
            assert "7" in last_tool_text(client)
            client.send(
                python="_console.sql_connection(None); selected.execute('SELECT 1').fetchone()"
            )
            assert last_tool_text(client) == "(1,)\n"
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            assert declaration() == []
            client.send(control="restart", requirements={"action": "reset"})
            assert declaration() == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
            ]
            client.send(sql="SELECT 9 AS restored")
            assert "9" in last_tool_text(client)
            return client.finish()[3:]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_selected_environment_uses_custom_connection_without_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", workspace / ".venv"],
            check=True,
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text("python: .venv/bin/python\n")
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        with sql_client(binary, execution, environment(bin_dir), workspace) as client:
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            client.send(
                # fmt: python
                python=code("""
                    import importlib.util
                    import sqlite3

                    assert importlib.util.find_spec("duckdb") is None
                    selected = sqlite3.connect(":memory:")
                    selected.execute("CREATE TABLE custom(value INTEGER)")
                    selected.execute("INSERT INTO custom VALUES (21)")
                    _console.sql_connection(selected)
                    assert _console.sql_connection() is selected
                    print("custom connection selected")
                    """)
            )
            assert last_tool_text(client) == "custom connection selected\n"
            client.send(sql="SELECT value * 2 AS answer FROM custom")
            assert "42" in last_tool_text(client)
            client.send(
                python="_console.sql_connection(None); selected.execute('SELECT value FROM custom').fetchone()"
            )
            assert last_tool_text(client) == "(21,)\n"
            client.send(sql="SELECT 1")
            assert "DuckDB is unavailable" in last_tool_text(client)
            return client.finish()[3:]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_bootstrap_ignores_workspace_duckdb_shadows(
    binary: Path, execution: Execution
) -> Transcript:
    for package in (False, True):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            workspace.mkdir()
            python = workspace / ".venv/bin/python"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", python.parent.parent],
                check=True,
            )
            subprocess.run(
                ["uv", "pip", "install", "--python", python, "duckdb"],
                check=True,
                capture_output=True,
            )
            config = workspace / ".agents/console/config.yaml"
            config.parent.mkdir(parents=True)
            config.write_text("python: .venv/bin/python\n")
            shadow = workspace / "duckdb.py"
            if package:
                (workspace / "duckdb").mkdir()
                shadow = workspace / "duckdb/__init__.py"
            shadow.write_text(
                "raise RuntimeError('workspace DuckDB shadow was imported')\n"
            )
            bin_dir = root / "bin"
            bin_dir.mkdir()
            env = dict(environment(bin_dir), MCP_CONSOLE_LANGUAGES="python,sql")
            with sql_client(binary, execution, env, workspace) as client:
                client.expect(
                    "Python remains available; DuckDB shadow remains unimported\n",
                    # fmt: python
                    python=code("""
                        import sqlite3
                        import sys

                        assert "duckdb" not in sys.modules
                        selected = sqlite3.connect(":memory:")
                        selected.execute("CREATE TABLE custom(value INTEGER)")
                        selected.execute("INSERT INTO custom VALUES (42)")
                        _console.sql_connection(selected)
                        print("Python remains available; DuckDB shadow remains unimported")
                        """),
                )
                client.send(sql="SELECT value FROM custom")
                assert "42" in last_tool_text(client), last_tool_text(client)
                client.finish()
    return [{"workspace_duckdb_module_and_package_remain_unimported": True}]


@requires(POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_selected_environment_uses_preinstalled_duckdb(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        python = workspace / ".venv/bin/python"
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", python.parent.parent],
            check=True,
        )
        subprocess.run(
            ["uv", "pip", "install", "--python", python, "duckdb"],
            check=True,
            capture_output=True,
        )
        home = root / "home"
        home.mkdir()
        subprocess.run(
            [
                python,
                "-I",
                "-c",
                "import duckdb; connection = duckdb.connect(':memory:'); connection.install_extension('fts')",
            ],
            check=True,
            capture_output=True,
            env=dict(os.environ, HOME=str(home)),
        )
        config = workspace / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True)
        config.write_text(
            json.dumps(
                {
                    "python": ".venv/bin/python",
                    "resolver": {"environment": {"HOME": str(root / "resolver-home")}},
                }
            )
        )
        bin_dir = root / "bin"
        bin_dir.mkdir()
        uv = bin_dir / "uv"
        uv.write_text("#!/bin/sh\nexit 87\n")
        uv.chmod(0o755)
        with sql_client(
            binary,
            execution,
            dict(environment(bin_dir), HOME=str(home)),
            workspace,
        ) as client:
            client.send(sql="CREATE TABLE selected_state AS SELECT 42 AS value")
            assert "Error:" not in last_tool_text(client)
            client.send(sql="SELECT value FROM selected_state")
            assert "42" in last_tool_text(client)
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client)
            client.send(
                python="_console.sql_connection().execute('SELECT value FROM selected_state').fetchone()"
            )
            assert last_tool_text(client) == "(42,)\n"
            client.send(control="restart")
            client.send(sql="SET autoinstall_known_extensions = false; LOAD fts")
            assert "Error:" not in last_tool_text(client), last_tool_text(client)
            inspected = client.send(requirements={"action": "get"})
            assert inspected["structuredContent"]["requirements"]["python"] == []
            refused = client.send(requirements={"duckdb": ["fts"]})
            assert refused.get("isError"), refused
            assert "user-selected Python environment" in last_result_text(client)
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_records_managed_sql_cells(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        env = managed_environment(root)
        with sql_client(binary, execution, env, workspace) as client:
            client.send(sql="SELECT 42 AS recorded")
            added = client.send(requirements={"duckdb": ["json"]})
            assert not added.get("isError"), added
            client.send(
                python='_console.sql_connection().execute("SELECT 1").fetchone()'
            )
            records = client.finish()[3:]
        (session,) = (workspace / ".agents/console/sessions").iterdir()
        markdown = (session / "transcript.md").read_text()
        quarto = (session / "transcript.qmd").read_text()
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        additions = [
            event
            for event in events
            if event["event"] == "tool_call"
            and event["request"].get("arguments", {}).get("requirements")
            == {"duckdb": ["json"]}
        ]
        assert len(additions) == 1, additions
        assert any(
            event["event"] == "tool_result"
            and event["call_id"] == additions[0]["call_id"]
            for event in events
        )
        assert not any(event["event"] == "requirements_selected" for event in events)
        assert '"duckdb": [\n      "json"\n' in markdown
        assert "[prepared]" in markdown
        assert "Requirements selected" not in markdown
        assert "execute:\n  eval: false" not in quarto
        assert "\nknitr:" in quarto and "\nir:" in quarto
        assert "```sql\nSELECT 42 AS recorded\n```" in markdown
        assert "```{sql}\nSELECT 42 AS recorded\n```" in quarto
        assert (
            """  python-packages:
    - numpy
    - pandas
    - matplotlib
    - plotnine
    - duckdb
"""
            in quarto
        )
        assert "ir:\n  isolated: true\n  packages: []\n  python-packages:\n" in quarto
        working_directory = events[0]["working_directory"]
        assert Path(working_directory).samefile(workspace)
        return TranscriptWithCompanions(
            records, {"qmd": quarto.replace(working_directory, "<workspace>")}
        )


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_language_restriction_still_disables_sql(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        env = dict(environment(root), MCP_CONSOLE_LANGUAGES="python")
        with sql_client(binary, execution, env) as client:
            schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
            assert "sql" not in schema["properties"]
            client.send(python="retained = 42; retained")
            assert last_tool_text(client) == "42\n"
            result = client.send(sql="SELECT 1")
            assert result.get("isError")
            assert "disabled by `MCP_CONSOLE_LANGUAGES`" in last_result_text(client)
            client.send(python="retained")
            assert last_tool_text(client) == "42\n"
            return client.finish()[3:]


@requires(SQL)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_interrupt_preserves_sql_and_python_state(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        expose_uv(root)
        env = environment(root)
        env["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"] = str(
            build_interposer(root, "python_probe_checkpoint")
        )
        client = McpClient(installed_console(binary), execution.serve(), env)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE retained AS SELECT 42 AS value")
            client.send(
                # fmt: python
                python=code(r"""
                    import ctypes
                    import os
                    import signal
                    from pathlib import Path

                    library = ctypes.PyDLL(os.environ["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"])
                    wait_for_interrupt = library.wait_for_probe_interrupt
                    wait_for_interrupt.argtypes = (
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_int,
                        ctypes.c_void_p,
                    )
                    wait_for_interrupt.restype = ctypes.c_int
                    started_path = Path(os.environ["TMPDIR"], "sql-interrupt-started")
                    release_path = Path(os.environ["TMPDIR"], "sql-interrupt-release")


                    class InterruptibleConnection:
                        description = (("answer",),)

                        def cursor(self):
                            return self

                        def execute(self, source):
                            if source == "WAIT":
                                wakeup_read, wakeup_write = os.pipe()
                                os.set_blocking(wakeup_write, False)
                                previous_wakeup = signal.set_wakeup_fd(wakeup_write)
                                try:
                                    with (
                                        started_path.open("wb", buffering=0) as started,
                                        release_path.open("rb", buffering=0) as release,
                                    ):
                                        assert (
                                            wait_for_interrupt(
                                                started.fileno(),
                                                release.fileno(),
                                                wakeup_read,
                                                ctypes.pythonapi.PyErr_CheckSignals,
                                            )
                                            == 0
                                        )
                                finally:
                                    signal.set_wakeup_fd(previous_wakeup)
                                    os.close(wakeup_read)
                                    os.close(wakeup_write)
                            return self

                        def fetchmany(self, size):
                            return [(7,)]


                    _console.sql_connection(InterruptibleConnection())
                    print(started_path, release_path, sep="\n")
                    """)
            )
            setup = client.transcript[-1]["result"]
            paths = last_tool_text(client).splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = (
                "<SQL interrupt started>\n<SQL interrupt release>\n"
            )
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            client.send(sql="WAIT", timeout_ms=0)
            assert "[running; poll with an empty send]" in last_tool_text(client)
            started.wait("sans-R SQL entered the interrupt checkpoint")
            client.send(control="interrupt", timeout_ms=0)
            release.release()
            release = None
            client.send()
            assert "KeyboardInterrupt" in last_tool_text(client)
            client.send(
                python="_console.sql_connection(None); print('Python remains usable')"
            )
            assert last_tool_text(client) == "Python remains usable\n"
            client.send(sql="SELECT value FROM retained")
            assert "42" in last_tool_text(client)
            records = client.finish()[3:]
            passed = True
            return records
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)
