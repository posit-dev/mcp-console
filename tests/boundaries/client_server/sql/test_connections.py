#!/usr/bin/env -S uv run --script

import os
import json
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.requirements import NATIVE_FIXTURES, POSIX, R, SQL, requires
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.native import SHARED_LIBRARY_FLAG, build_interposer
from support.records import Transcript
from support.suites import run_this_suite


@requires(R, SQL)
@executions(SANDBOXED)
def test_prepares_builtin_extensions_without_downloads(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment, _ = r_test_environment()
        # This R-only offline fixture needs no managed Python environment.
        environment["RETICULATE_PYTHON"] = sys.executable
        environment["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(root / "extensions")
        arguments = execution.serve("-c", "cache=host")
        with McpClient(binary, arguments, environment, root) as client:
            client.initialize_and_list_tools()
            client.expect(r="invisible(.console$sql_connection())")
            client.finish()
        assert not list((root / "extensions").glob("**/parquet.duckdb_extension"))
        config = root / ".agents/console/config.yaml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(
            json.dumps(
                {"resolver": {"sandbox": {"network": {"proxy": {"domains": {}}}}}}
            )
        )
        environment["UV_OFFLINE"] = "1"
        with McpClient(binary, arguments, environment, root) as client:
            client.initialize_and_list_tools()
            client.expect(
                # fmt: r
                r=code(r"""
                    retained_connection <- .console$sql_connection()
                    invisible(DBI::dbExecute(
                      retained_connection,
                      "CREATE TABLE retained AS SELECT 42 AS answer"
                    ))
                    """),
            )
            client.expect("[prepared]", requirements={"duckdb": ["parquet"]})
            client.expect(
                "[1] TRUE\n",
                r="identical(.console$sql_connection(), retained_connection)",
            )
            client.expect(
                "[1] 42\n",
                r="DBI::dbGetQuery(.console$sql_connection(), 'SELECT answer FROM retained')$answer",
            )
            return client.finish()[3:]


@requires(R, SQL)
@executions(RUNTIME)
def test_gets_selects_and_resets_the_active_native_connection(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            managed <- .console$sql_connection()
            stopifnot(inherits(managed, "duckdb_connection"))
            stopifnot(!exists("sql_connection"), !exists("console_sql_connection"))
            invisible(DBI::dbExecute(
              managed,
              "CREATE TABLE retained AS SELECT 42 AS value"
            ))
            sqlite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
            stopifnot(is.null(.console$sql_connection(connection = sqlite)))
            stopifnot(identical(.console$sql_connection(), sqlite))
            writeLines("selected native DBI identity")
            """)
        client.expect(
            "selected native DBI identity\n", r=r, requirements={"r": ["RSQLite"]}
        )
        client.send(sql="CREATE TABLE chosen AS SELECT 11 AS value")
        assert last_tool_text(client) == "[done]"
        # fmt: r
        r = code(r"""
            stopifnot(DBI::dbGetQuery(sqlite, "SELECT value FROM chosen")$value == 11)
            closed <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
            invisible(DBI::dbDisconnect(closed))
            message <- tryCatch(.console$sql_connection(closed), error = conditionMessage)
            stopifnot(identical(.console$sql_connection(), sqlite))
            writeLines(message)
            """)
        client.send(r=r)
        assert (
            last_tool_text(client)
            == "`connection` must be a valid DBIConnection or NULL\n"
        )
        # fmt: python
        python = code("""
            import builtins
            import sqlite3

            assert "_console" not in globals()
            assert "sql_connection" not in vars(builtins)
            assert "console_sql_connection" not in vars(builtins)
            try:
                _console.sql_connection()
            except RuntimeError as error:
                print(error)
            selected = sqlite3.connect(":memory:")
            assert _console.sql_connection(connection=selected) is None
            assert _console.sql_connection() is selected
            _ = selected.execute("CREATE TABLE chosen AS SELECT 23 AS value")
            """)
        client.send(python=python)
        assert last_tool_text(client) == (
            "The active SQL connection belongs to R; use .console$sql_connection() in R\n"
        )
        # fmt: r
        r = code(r"""
            writeLines(tryCatch(.console$sql_connection(), error = conditionMessage))
            stopifnot(DBI::dbIsValid(sqlite))
            """)
        client.send(r=r)
        assert last_tool_text(client) == (
            "The active SQL connection belongs to Python; use _console.sql_connection() in Python\n"
        )
        client.send(sql="SELECT value FROM chosen")
        assert "23" in last_tool_text(client)
        # fmt: python
        python = code("""
            try:
                _console.sql_connection(object())
            except TypeError as error:
                print(error)
            assert _console.sql_connection() is selected
            selected.close()
            """)
        client.send(python=python)
        assert last_tool_text(client) == (
            "`connection` must provide a callable cursor() method or be None\n"
        )
        client.send(sql="SELECT value FROM chosen")
        assert last_tool_text(client) == "Error: Cannot operate on a closed database.\n"
        # fmt: python
        python = code("""
            assert _console.sql_connection() is selected
            selected = sqlite3.connect(":memory:")
            selected.execute("CREATE TABLE still_open AS SELECT 7 AS value")
            _console.sql_connection(selected)
            assert _console.sql_connection(None) is None
            assert selected.execute("SELECT value FROM still_open").fetchone() == (7,)
            try:
                _console.sql_connection()
            except RuntimeError as error:
                print(error)
            """)
        client.send(python=python)
        assert last_tool_text(client) == (
            "The active SQL connection belongs to R; use .console$sql_connection() in R\n"
        )
        # fmt: r
        r = code(r"""
            stopifnot(identical(.console$sql_connection(), managed))
            stopifnot(DBI::dbIsValid(sqlite))
            stopifnot(is.null(.console$sql_connection(sqlite)))
            stopifnot(
              DBI::dbGetQuery(
                .console$sql_connection(),
                "SELECT value FROM chosen"
              )$value ==
                11
            )
            stopifnot(is.null(.console$sql_connection(NULL)))
            stopifnot(identical(.console$sql_connection(), managed), DBI::dbIsValid(sqlite))
            writeLines("reset retained managed identity and user connection")
            """)
        client.send(r=r)
        assert (
            last_tool_text(client)
            == "reset retained managed identity and user connection\n"
        )
        client.send(sql="SELECT value FROM retained")
        assert "42" in last_tool_text(client)
        client.send(python="_console.sql_connection(selected)")
        assert last_tool_text(client) == "[done]"
        client.send(r=".console$sql_connection(NULL)")
        assert last_tool_text(client) == "[done]"
        client.send(
            python="selected.execute('SELECT value FROM still_open').fetchone()"
        )
        assert last_tool_text(client) == "(7,)\n"
        # Restart reinstalls the namespace and drops both selection and catalog.
        client.send(python="_console.sql_connection(selected)")
        assert last_tool_text(client) == "[done]"
        client.send(control="restart", sql="SELECT value FROM retained")
        output = last_tool_text(client)
        assert "Table with name retained does not exist" in output, output
        assert "Context: rapi_prepare" in output and "Error type: CATALOG" in output, (
            output
        )
        client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
            "\ni Context:", "\nℹ Context:"
        ).replace("\ni Error type:", "\nℹ Error type:")
        client.send(
            r='stopifnot(inherits(.console$sql_connection(), "duckdb_connection")); writeLines("new managed generation")'
        )
        assert last_tool_text(client) == "new managed generation\n"
        return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_routes_sql_cells_to_a_selected_dbi_connection(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_values AS SELECT 'managed' AS origin")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        sqlite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
        .console$sql_connection(connection = sqlite)
        cat(
          c("selected: ", identical(.console$sql_connection(), sqlite), "\n"),
          c("valid: ", DBI::dbIsValid(.console$sql_connection()), "\n"),
          sep = ""
        )
        """)
    client.expect("selected: TRUE\nvalid: TRUE\n", r=r, requirements={"r": ["RSQLite"]})

    # fmt: r
    r = code(r"""
        selected <- .console$sql_connection()
        message <- tryCatch(
          .console$sql_connection("not a connection"),
          error = conditionMessage
        )
        cat(
          c("rejected: ", message, "\n"),
          c("unchanged: ", identical(.console$sql_connection(), selected), "\n"),
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        "rejected: `connection` must be a valid DBIConnection or NULL\n"
        "unchanged: TRUE\n"
    )

    client.send(
        sql=("CREATE TABLE custom_values (label TEXT NOT NULL, value INTEGER NOT NULL)")
    )
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('a', 2), ('b', 5)")
    assert last_tool_text(client) == "[done]"

    client.send(r="previous_warn <- getOption('warn'); options(warn = 2); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('RETURNING', 7)")
    assert last_tool_text(client) == "[done]"
    client.send(
        sql=(
            "WITH incoming(label, value) AS (VALUES ('cte', 9)) "
            "INSERT INTO custom_values SELECT label, value FROM incoming"
        )
    )
    assert last_tool_text(client) == "[done]"
    client.send(sql="PRAGMA user_version = 3")
    assert last_tool_text(client) == "[done]"
    client.send(r="invisible(options(warn = previous_warn))")
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        -- The selected DBI backend handles this query.
        SELECT label, value
        FROM custom_values
        ORDER BY label
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"a"' in preview and "2" in preview
    assert '"b"' in preview and "5" in preview
    assert '"RETURNING"' in preview and "7" in preview
    assert '"cte"' in preview and "9" in preview
    assert '"managed"' not in preview

    client.send(sql="SELECT missing FROM missing_values")
    assert last_tool_text(client) == "Error: no such table: missing_values\n"

    client.send(sql="SELECT value FROM custom_values WHERE FALSE")
    preview = last_tool_text(client)
    assert "value" in preview and "<int32>" in preview, preview
    assert "[0 rows]" in preview

    sql = code(r"""
        WITH RECURSIVE sequence(value) AS (
          SELECT 1
          UNION ALL
          SELECT value + 1 FROM sequence WHERE value < 25
        )
        SELECT value FROM sequence
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "20" in preview
    assert "[additional rows omitted]" in preview

    client.send(
        sql=("INSERT INTO custom_values VALUES ('c', 11) RETURNING label, value")
    )
    preview = last_tool_text(client)
    assert '"c"' in preview and "11" in preview

    # fmt: r
    r = code(r"""
        selected <- .console$sql_connection()
        invisible(DBI::dbDisconnect(selected))
        cat(
          c("disconnected: ", !DBI::dbIsValid(selected), "\n"),
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "disconnected: TRUE\n"

    client.send(sql="SELECT label FROM custom_values")
    assert last_tool_text(client) == (
        "Error: The selected SQL connection is no longer valid; "
        "call .console$sql_connection(NULL) to restore DuckDB\n"
    )

    # fmt: r
    r = code(r"""
        .console$sql_connection(NULL)
        cat(
          c("restored: ", !identical(.console$sql_connection(), selected), "\n"),
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "restored: TRUE\n"

    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert '"a"' not in preview
    return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_python_restores_managed_connection_before_r_reads_it(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()

    client.expect(sql="CREATE TABLE managed_values AS SELECT 42 AS value")

    # fmt: r
    r = code(r"""
        lite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
        .console$sql_connection(lite)
        invisible()
        """)
    client.expect(r=r, requirements={"r": ["RSQLite"]})

    client.send(python="_console.sql_connection(None)")
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        restored <- .console$sql_connection()
        DBI::dbDisconnect(lite)
        cat(
          c("managed: ", inherits(restored, "duckdb_connection"), "\n"),
          c(
            "value: ",
            DBI::dbGetQuery(
              .console$sql_connection(),
              "SELECT value FROM managed_values"
            )$value,
            "\n"
          ),
          sep = ""
        )
        """)
    client.send(r=r)
    output = last_tool_text(client)
    assert output == "managed: TRUE\nvalue: 42\n", output

    # fmt: python
    python = code("""
        import _mcp_console_sql
        import sys

        use_r_code = _mcp_console_sql.use_r.__code__


        def reject_repeated_restore(frame, event, argument):
            if event == "call" and frame.f_code is use_r_code:
                raise SystemExit("repeated managed restoration")
            return reject_repeated_restore


        sys.settrace(reject_repeated_restore)
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="SELECT value FROM managed_values")
    preview = last_tool_text(client)
    assert "value" in preview and "42" in preview, preview
    client.send(python="sys.settrace(None)")
    assert last_tool_text(client) == "[done]"

    # Python can call R again before its own cell finishes.
    # fmt: r
    r = code(r"""
        another <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
        .console$sql_connection(another)
        managed_in_r <- function() inherits(.console$sql_connection(), "duckdb_connection")
        invisible()
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    # fmt: python
    python = code("""
        _console.sql_connection(None)
        assert r.managed_in_r()
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"
    client.send(r="DBI::dbDisconnect(another); invisible()")
    assert last_tool_text(client) == "[done]"
    return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_routes_sql_cells_to_a_selected_python_dbapi_connection(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_values AS SELECT 'managed' AS origin")
    assert last_tool_text(client) == "[done]"

    # fmt: python
    python = code("""
        import sqlite3


        class CursorOnlyConnection:
            def __init__(self, connection):
                self.connection = connection

            def cursor(self):
                return self.connection.cursor()


        sqlite = sqlite3.connect(":memory:")
        connection = CursorOnlyConnection(sqlite)
        _console.sql_connection(connection)
        del sqlite
        del connection
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    # Invalid selections leave the current provider unchanged.
    # fmt: python
    python = code("""
        try:
            _console.sql_connection(object())
        except TypeError as error:
            print(error)
        """)
    client.send(python=python)
    assert last_tool_text(client) == (
        "`connection` must provide a callable cursor() method or be None\n"
    )

    client.send(sql="CREATE TABLE custom_values (label TEXT, value INTEGER)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="INSERT INTO custom_values VALUES ('a', 2), ('b', NULL)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT label, value FROM custom_values ORDER BY label")
    preview = last_tool_text(client)
    assert "label" in preview and "value" in preview
    assert "'a'" in preview and "2" in preview
    assert "'b'" in preview and "NULL" in preview
    assert "managed" not in preview

    client.send(sql="SELECT missing FROM missing_values")
    assert last_tool_text(client) == "Error: no such table: missing_values\n"

    control_alias = "tab\theader\nnext"
    client.send(sql=f'SELECT 1 AS "{control_alias}"')
    preview = last_tool_text(client)
    assert "\t" not in preview and control_alias not in preview
    assert "\\t" in preview and "\\n" in preview
    assert max(map(display_width, preview.splitlines())) <= 200

    alias = "é" * 240
    client.send(sql=f'SELECT 1 AS "{alias}"')
    preview = last_tool_text(client)
    assert len(preview.encode("utf-8")) <= 12 * 1024, len(preview.encode("utf-8"))
    assert max(map(display_width, preview.splitlines())) <= 200
    assert alias not in preview

    columns = ", ".join(
        (f"replace(printf('%040d', 0), '0', '🐍') AS value_{index}")
        for index in range(12)
    )
    client.send(
        sql=(
            "WITH RECURSIVE sequence(value) AS ("
            "SELECT 1 UNION ALL SELECT value + 1 FROM sequence WHERE value < 20"
            f") SELECT {columns} FROM sequence"
        )
    )
    preview = last_tool_text(client)
    assert len(preview.encode("utf-8")) <= 12 * 1024, len(preview.encode("utf-8"))
    assert max(map(display_width, preview.splitlines())) <= 200

    sql = code("""
        WITH RECURSIVE sequence(value) AS (
          SELECT 1
          UNION ALL
          SELECT value + 1 FROM sequence WHERE value < 25
        )
        SELECT value FROM sequence
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "20" in preview
    assert "[additional rows omitted]" in preview

    # Driver-specific values may return arbitrary text from repr().
    # fmt: python
    python = code("""
        class ControlValue:
            def __repr__(self):
                return "tab\\tline\\nreturn\\rcontrol\\x01" + "\\t" * 80


        class ControlCursor:
            description = (("value",),)

            def execute(self, source):
                return self

            def fetchmany(self, size):
                return [(ControlValue(),)][:size]

            def close(self):
                pass


        class ControlConnection:
            def cursor(self):
                return ControlCursor()


        _console.sql_connection(ControlConnection())
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="CONTROL VALUE")
    preview = last_tool_text(client)
    assert all(control not in preview for control in ("\t", "\r", "\x01"))
    assert all(escape in preview for escape in ("\\t", "\\n", "\\r", "\\x01"))
    assert len(preview.splitlines()) == 4
    assert max(map(display_width, preview.splitlines())) <= 200
    assert "[cell values truncated to 160 characters]" in preview

    client.send(r=".console$sql_connection(NULL); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert "'a'" not in preview

    client.send(python="_console.sql_connection(sqlite3.connect(':memory:'))")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT 42 AS python_value")
    assert "42" in last_tool_text(client)

    client.send(python="_console.sql_connection(None)")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT origin FROM managed_values")
    preview = last_tool_text(client)
    assert '"managed"' in preview
    assert "'a'" not in preview
    return client.finish()


@requires(SQL)
@executions(RUNTIME)
def test_preserves_selected_python_duckdb_connection_state(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import duckdb

        connection = duckdb.connect(":memory:")
        connection.execute("CREATE TEMP TABLE before_selection AS SELECT 41 AS value")
        _console.sql_connection(connection)
        del connection
        """)
    client.expect(
        python=python,
        requirements={"python": ["duckdb==1.5.5"]},
    )

    client.send(sql="SELECT value + 1 AS answer FROM before_selection")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview

    client.send(sql="CREATE TEMP TABLE later_state AS SELECT 'retained' AS value")
    assert "Error:" not in last_tool_text(client)
    client.send(sql="SELECT value FROM later_state")
    preview = last_tool_text(client)
    assert "value" in preview and "'retained'" in preview
    client.send(control="restart")
    assert (
        last_tool_text(client)
        == "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
    ), last_tool_text(client)
    client.send(python='print("new interpreter")')
    assert last_tool_text(client) == "new interpreter\n"
    return client.finish()


@requires(SQL)
@executions(RUNTIME)
def test_reports_python_dbapi_cursor_cleanup_failures(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        class CleanupCursor:
            description = (("answer",),)

            def execute(self, source):
                if source == "ERROR":
                    raise RuntimeError("selected DB-API execution failure")
                return self

            def fetchmany(self, size):
                return [(42,)][:size]

            def close(self):
                raise RuntimeError("selected DB-API cleanup failure")


        class CleanupConnection:
            def cursor(self):
                return CleanupCursor()


        _console.sql_connection(CleanupConnection())
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(sql="ANSWER")
    output = last_tool_text(client)
    assert "answer" in output and "42" in output
    assert "Error: selected DB-API cleanup failure" in output

    client.send(sql="ERROR")
    output = last_tool_text(client)
    execution = "Error: selected DB-API execution failure"
    cleanup = "Error: selected DB-API cleanup failure"
    assert execution in output and cleanup in output
    assert output.index(execution) < output.index(cleanup)
    return client.finish()


@requires(SQL)
@executions(RUNTIME)
def test_recovers_when_python_dbapi_connection_raises_base_exception(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        class RecoverableCursor:
            description = None

            def __init__(self, connection):
                self.connection = connection

            def execute(self, source):
                if source == "EXIT":
                    raise SystemExit("selected DB-API failure")
                self.description = (("answer",),)
                return self

            def fetchmany(self, size):
                return [(42,)][:size]

            def close(self):
                if self.connection.close_failure:
                    self.connection.close_failure = False
                    raise SystemExit("selected DB-API cleanup failure")


        class RecoverableConnection:
            def __init__(self):
                self.close_failure = True

            def cursor(self):
                return RecoverableCursor(self)


        _console.sql_connection(RecoverableConnection())
        """)
    client.expect(python=python)

    client.send(sql="EXIT")
    output = last_tool_text(client)
    assert "SystemExit: selected DB-API failure" in output
    assert "SystemExit: selected DB-API cleanup failure" in output

    client.send(sql="ANSWER")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview
    return client.finish()


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_allows_python_dbapi_callbacks_to_select_an_r_connection(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        client = McpClient(binary, execution.serve(), environment)
        checkpoints: list[FifoCheckpoint] = []
        passed = False
        try:
            client.initialize_and_list_tools()
            client.send(sql="CREATE TABLE managed_values AS SELECT 42 AS value")
            assert last_tool_text(client) == "[done]"

            # Create checkpoint paths inside the worker's writable directory.
            # fmt: r
            r = code(r"""
                callback_started <- tempfile("mcp-console-sql-callback-started-")
                callback_release <- tempfile("mcp-console-sql-callback-release-")
                cat(callback_started, callback_release, sep = "\n")
                """)
            client.send(r=r)
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = "<callback started>\n<callback release>"
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            # The SQLite UDF re-enters R while the Python DB-API provider is
            # evaluating the current cell, then selects managed DuckDB for
            # later SQL cells.
            # fmt: r
            r = code(r"""
                select_r_sql <- function() {
                  started <- fifo(callback_started, open = "wb", blocking = TRUE)
                  writeBin(charToRaw("1"), started)
                  close(started)
                  gate <- fifo(callback_release, open = "rb", blocking = TRUE)
                  stopifnot(identical(
                    readBin(gate, "raw", n = 1L),
                    charToRaw("1")
                  ))
                  close(gate)
                  .console$sql_connection(NULL)
                  41L
                }
                invisible()
                """)
            client.send(r=r)
            output = last_tool_text(client)
            assert output == "[done]", output

            # fmt: python
            python = code("""
                import sqlite3

                connection = sqlite3.connect(":memory:")
                connection.create_function("select_r_sql", 0, r.select_r_sql)
                _console.sql_connection(connection)
                """)
            client.send(python=python)
            output = last_tool_text(client)
            assert output == "[done]", output

            evaluation = client.start_send(
                sql="SELECT select_r_sql() AS callback_value",
                timeout_ms=0,
            )
            started.wait("Python DB-API callback entered R")
            client.receive(evaluation)
            assert without_elapsed(evaluation["result"]["content"][0]["text"]) == (
                "\n[running; poll with an empty send]"
            )

            release.release()
            client.send(timeout_ms=3_000)
            preview = last_tool_text(client)
            assert "callback_value" in preview and "41" in preview, preview

            client.send(sql="SELECT value FROM managed_values")
            preview = last_tool_text(client)
            assert "value" in preview and "42" in preview

            transcript = client.finish()
            passed = True
            return transcript
        finally:
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_interrupts_selected_python_dbapi_connection(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"] = str(
            build_interposer(temporary_path, "python_probe_checkpoint")
        )
        client = McpClient(binary, execution.serve(), environment)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()
            # fmt: python
            python = code(r"""
                import ctypes
                import os
                import signal
                from pathlib import Path

                checkpoint_library = ctypes.PyDLL(os.environ["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"])
                wait_for_interrupt = checkpoint_library.wait_for_probe_interrupt
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
                    def __init__(self):
                        self.description = None
                        self.rows = []

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
                        self.description = (("answer",),)
                        self.rows = [(42,)]
                        return self

                    def fetchmany(self, size):
                        return self.rows[:size]


                _console.sql_connection(InterruptibleConnection())
                print(started_path, release_path, sep="\n")
                """)
            wait_for_evaluation_output(
                client,
                None,
                "native SQL interrupt checkpoint paths",
                python=python,
                completion_timeout_seconds=client.response_timeout,
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
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            started.wait("SQL execution entered native interrupt checkpoint")

            client.send(control="interrupt", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            release.release()
            release = None
            client.send()
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(sql="ANSWER")
            preview = last_tool_text(client)
            assert "answer" in preview and "42" in preview
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_interrupts_python_dbapi_provider_probe(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        library = Path(temporary_directory) / "python-probe-checkpoint.dylib"
        source = (
            Path(__file__).resolve().parents[3]
            / "fixtures"
            / "native"
            / "python_probe_checkpoint.c"
        )
        subprocess.run(
            [
                "cc",
                SHARED_LIBRARY_FLAG,
                "-fPIC",
                "-std=c11",
                "-Wall",
                "-Wextra",
                "-Werror",
                "-o",
                library,
                source,
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        environment = os.environ.copy()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_SQL_PROBE_LIBRARY"] = str(library)
        client = McpClient(binary, execution.serve(), environment)
        checkpoints: list[FifoCheckpoint] = []
        release = None
        passed = False
        try:
            client.initialize_and_list_tools()

            # Create the checkpoints inside the worker's writable directory.
            # fmt: r
            r = code(r"""
                probe_started <- tempfile("mcp-console-sql-probe-started-")
                probe_release <- tempfile("mcp-console-sql-probe-release-")
                cat(probe_started, probe_release, sep = "\n")
                """)
            wait_for_evaluation_output(
                client,
                None,
                "SQL provider probe checkpoint paths",
                r=r,
                completion_timeout_seconds=client.response_timeout,
            )
            setup = client.transcript[-1]["result"]
            paths = setup["content"][0]["text"].splitlines()
            assert len(paths) == 2, setup
            setup["content"][0]["text"] = "<probe started>\n<probe release>"
            started, release = [FifoCheckpoint.create(Path(path)) for path in paths]
            checkpoints.extend((started, release))

            # fmt: python
            python = code("""
                import ctypes
                import os
                import signal
                import sys


                # The native checkpoint accepts Python's pending signal while
                # PyDLL still holds the GIL and owns the Python call frame.
                checkpoint_library = ctypes.PyDLL(os.environ["MCP_CONSOLE_SQL_PROBE_LIBRARY"])
                wait_for_probe_interrupt = checkpoint_library.wait_for_probe_interrupt
                wait_for_probe_interrupt.argtypes = (
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_int,
                    ctypes.c_void_p,
                )
                wait_for_probe_interrupt.restype = ctypes.c_int


                class ProbeConnection:
                    description = (("answer",),)

                    def cursor(self):
                        return self

                    def execute(self, source):
                        return self

                    def fetchmany(self, size):
                        return [(42,)][:size]


                def pause_provider_probe(frame, event, argument):
                    if event == "call" and frame.f_globals.get("__name__") == "_mcp_console_sql":
                        sys.settrace(None)
                        wakeup_read, wakeup_write = os.pipe()
                        os.set_blocking(wakeup_write, False)
                        previous_wakeup = signal.set_wakeup_fd(wakeup_write)
                        try:
                            with (
                                open(r.probe_started, "wb", buffering=0) as started,
                                open(r.probe_release, "rb", buffering=0) as release,
                            ):
                                assert (
                                    wait_for_probe_interrupt(
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
                    return pause_provider_probe


                _console.sql_connection(ProbeConnection())
                sys.settrace(pause_provider_probe)
                """)
            client.send(python=python)
            assert last_tool_text(client) == "[done]"

            evaluation = client.start_send(sql="ANSWER", timeout_ms=0)
            started.wait("Python DB-API provider probe started")
            client.receive(evaluation)
            assert without_elapsed(evaluation["result"]["content"][0]["text"]) == (
                "\n[running; poll with an empty send]"
            )

            client.send(control="interrupt", timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            release.release()
            release = None
            client.send(timeout_ms=30_000)
            assert "KeyboardInterrupt" in last_tool_text(client)

            client.send(sql="ANSWER")
            preview = last_tool_text(client)
            assert "answer" in preview and "42" in preview
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if release is not None:
                release.release()
            for checkpoint in checkpoints:
                checkpoint.close()
            if not passed:
                stop_client(client)


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_recovers_when_python_sql_dispatch_trace_raises_system_exit(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    # fmt: python
    python = code("""
        import _mcp_console_sql
        import sys


        class StatefulConnection:
            description = (("answer",),)

            def __init__(self):
                self.answer = 42

            def cursor(self):
                return self

            def execute(self, source):
                return self

            def fetchmany(self, size):
                return [(self.answer,)][:size]


        dispatch_code = _mcp_console_sql.dispatch.__code__


        def exit_sql_dispatch(frame, event, argument):
            if event == "call" and frame.f_code is dispatch_code:
                raise SystemExit("selected SQL dispatch exit")
            return exit_sql_dispatch


        connection = StatefulConnection()
        _console.sql_connection(connection)
        sys.settrace(exit_sql_dispatch)
        """)
    client.expect(python=python)

    client.send(sql="ANSWER")
    assert "SystemExit: selected SQL dispatch exit" in last_tool_text(client)

    client.send(python="connection.answer")
    assert last_tool_text(client) == "42\n"

    client.send(sql="ANSWER")
    preview = last_tool_text(client)
    assert "answer" in preview and "42" in preview
    return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_recovers_when_r_provider_switch_trace_raises_system_exit(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()

    client.send(sql="CREATE TABLE managed_value AS SELECT 7 AS value")
    assert last_tool_text(client) == "[done]"

    # fmt: python
    python = code("""
        import _mcp_console_sql
        import sqlite3
        import sys


        connection = sqlite3.connect(":memory:")
        connection.execute("CREATE TABLE python_value AS SELECT 42 AS value")
        _console.sql_connection(connection)
        use_r_code = _mcp_console_sql.use_r.__code__


        def exit_use_r(frame, event, argument):
            if event == "call" and frame.f_code is use_r_code:
                raise SystemExit("R provider switch exit")
            return exit_use_r


        sys.settrace(exit_use_r)
        """)
    client.send(python=python)
    assert last_tool_text(client) == "[done]"

    client.send(r=".console$sql_connection(NULL); invisible()")
    assert "SystemExit: R provider switch exit" in last_tool_text(client)

    client.send(
        python="connection.execute('SELECT value FROM python_value').fetchone()[0]"
    )
    assert last_tool_text(client) == "42\n"

    client.send(sql="SELECT value FROM python_value")
    preview = last_tool_text(client)
    assert "value" in preview and "42" in preview

    client.send(r=".console$sql_connection(NULL); invisible()")
    assert last_tool_text(client) == "[done]"
    client.send(sql="SELECT value FROM managed_value")
    preview = last_tool_text(client)
    assert "value" in preview and "7" in preview
    return client.finish()


def display_width(text: str) -> int:
    return sum(
        0
        if unicodedata.combining(character)
        else 2
        if unicodedata.east_asian_width(character) in {"F", "W"}
        else 1
        for character in text
    )


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_interrupts_sql_warmup_without_losing_worker(
    binary: Path, execution: Execution
) -> Transcript:
    for with_r, behavior in (
        (False, "interrupt"),
        (False, "metadata-interrupt"),
        (True, "interrupt"),
        (True, "setup-interrupt"),
        (True, "setup-interrupt-disconnect-error"),
        (True, "probe-interrupt-DBI"),
        (True, "probe-interrupt-duckdb"),
    ):
        with startup_sql_client(binary, execution, with_r, behavior) as (
            client,
            _,
            _,
        ):
            wait_for_evaluation_output(
                client,
                '[input requested: "SQL warmup> "]\n[waiting for stdin]',
                "managed SQL warmup interruption checkpoint",
                sql="CREATE TABLE withheld_cell AS SELECT 1",
                timeout_ms=0,
                completion_timeout_seconds=client.response_timeout,
            )
            client.send(control="interrupt", timeout_ms=30_000)
            result = client.transcript[-1]["result"]
            assert result.get("isError") is not True, result
            assert "[worker" not in last_tool_text(client), result
            assert "[running; poll with an empty send]" not in last_tool_text(client), (
                result
            )
            if behavior == "setup-interrupt-disconnect-error":
                assert "optional SQL disconnect failed" in last_tool_text(client), (
                    result
                )
            # The interrupt receipt can precede completion of the early cell's
            # admission. Observe that completion before submitting another cell.
            wait_for_evaluation_output(
                client,
                None,
                "interrupted startup cell completes",
                completion_timeout_seconds=client.response_timeout,
            )
            if with_r:
                client.expect(r="stopifnot(startup_sql_pid == Sys.getpid())")
                if behavior.startswith("setup-interrupt"):
                    client.expect(
                        # fmt: r
                        r=code("""
                            stopifnot(
                              length(startup_setup_connections) == 1L,
                              !DBI::dbIsValid(startup_setup_connections[[1L]])
                            )
                            startup_allow_sql_setup <- TRUE
                            """),
                    )
                    client.expect(
                        r='stopifnot(!"withheld_cell" %in% DBI::dbListTables(.console$sql_connection()))'
                    )
            else:
                client.expect(python="assert startup_sql_pid == os.getpid()")
            client.send(
                sql="SELECT count(*) AS withheld FROM information_schema.tables WHERE table_name = 'withheld_cell'"
            )
            assert "0" in last_tool_text(client), client.transcript[-1]
            client.send(sql="SELECT answer FROM startup_catalog")
            assert "42" in last_tool_text(client), client.transcript[-1]
            client.finish()
    return [{"sql_warmup_interrupt_withholds_cell_and_retains_worker": True}]


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_optional_sql_warmup_failure_preserves_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    for with_r, behavior in (
        (True, "error"),
        (True, "setup-error-disconnect-error"),
        (False, "error"),
        (False, "import-error"),
        (False, "metadata-error"),
        (False, "system-exit"),
    ):
        with startup_sql_client(binary, execution, with_r, behavior) as (client, _, _):
            if with_r:
                client.send(
                    r='stopifnot(startup_sql_pid == Sys.getpid()); cat("R available after SQL warmup failure\\n")'
                )
            else:
                client.send(
                    python='assert startup_sql_pid == os.getpid(); print("Python available after SQL warmup failure")'
                )
            result = client.transcript[-1]["result"]
            assert result.get("isError") is not True, result
            output = last_tool_text(client)
            if behavior == "setup-error-disconnect-error":
                assert "optional SQL warmup setup failed" in output, result
                assert "optional SQL disconnect failed" in output, result
                client.expect(
                    # fmt: r
                    r=code("""
                        stopifnot(!DBI::dbIsValid(startup_setup_connections[[1L]]))
                        startup_allow_sql_setup <- TRUE
                        """),
                )
            else:
                assert "optional SQL warmup failed" in output, result
            assert "available after SQL warmup failure" in output, result
            assert "[worker" not in output, result
            if behavior == "system-exit":
                assert "SystemExit: optional SQL warmup failed" in output, result
            if not with_r:
                client.expect(
                    python="import sqlite3; selected = sqlite3.connect(':memory:'); _console.sql_connection(selected)"
                )
                client.send(sql="SELECT 42 AS answer")
                assert "42" in last_tool_text(client), client.transcript[-1]
                client.expect(python="_console.sql_connection(None)")
            client.send(sql="SELECT 42 AS answer")
            assert "42" in last_tool_text(client), client.transcript[-1]
            client.finish()
    return [{"optional_sql_warmup_failure_preserves_runtime_and_later_sql": True}]


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_closes_provisional_connections_after_sql_setup_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with startup_sql_client(binary, execution, True, "setup-error") as (client, _, _):
        for attempt in (1, 2):
            if attempt == 2:
                client.send(sql="SELECT 42 AS answer")
                assert "optional SQL warmup setup failed" in last_tool_text(client)
            client.send(
                # fmt: r
                r=code(f"""
                    stopifnot(
                      startup_sql_pid == Sys.getpid(),
                      length(startup_setup_connections) == {attempt}L,
                      vapply(startup_setup_connections, function(conn) !DBI::dbIsValid(conn), logical(1L))
                    )
                    cat("Failed connections closed\\n")
                    """),
            )
            result = client.transcript[-1]["result"]
            assert result.get("isError") is not True, result
            assert "Failed connections closed" in last_tool_text(client), result
        client.expect(r="startup_allow_sql_setup <- TRUE")
        client.send(sql="SELECT 42 AS answer")
        assert "42" in last_tool_text(client), client.transcript[-1]
        client.expect(
            # fmt: r
            r=code("""
                stopifnot(
                  length(startup_setup_connections) == 3L,
                  DBI::dbIsValid(startup_setup_connections[[3L]]),
                  identical(.console$sql_connection(), startup_setup_connections[[3L]])
                )
                """),
        )
        client.finish()
    return [{"failed_sql_setup_connections_closed_and_retry_retained": True}]


@contextmanager
def startup_sql_client(
    binary: Path,
    execution: Execution,
    with_r: bool,
    behavior: str,
) -> Iterator[tuple[McpClient, FifoCheckpoint, FifoCheckpoint]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        started = FifoCheckpoint.create(root / "sql-started")
        release = FifoCheckpoint.create(root / "sql-release")
        environment, rscript = r_test_environment()
        try:
            if with_r:
                library = root / "library"
                library.mkdir()
                fixture = Path(__file__).resolve().parents[3] / "fixtures/early_sql"
                subprocess.run(
                    [
                        rscript.with_name("R"),
                        "CMD",
                        "INSTALL",
                        f"--library={library}",
                        fixture,
                    ],
                    env=environment,
                    capture_output=True,
                    check=True,
                )
                environment.update(
                    R_LIBS=os.pathsep.join(
                        filter(None, (str(library), environment.get("R_LIBS")))
                    ),
                    R_DEFAULT_PACKAGES="datasets,utils,grDevices,graphics,stats,methods,mcpconsoleearlysql",
                )
            else:
                from support.linux_sandbox import retain_system_bwrap

                native_bin = root / "bin"
                native_bin.mkdir()
                retain_system_bwrap(native_bin, environment.get("PATH"))
                (native_bin / "uv").symlink_to(shutil.which("uv"))
                environment["PATH"] = str(native_bin)
                for name in (
                    "R_HOME",
                    "R_LIBS",
                    "R_LIBS_USER",
                    "RETICULATE_UV",
                    "RETICULATE_PYTHON",
                ):
                    environment.pop(name, None)
                shutil.copyfile(
                    Path(__file__).resolve().parents[3]
                    / "fixtures/early_sql/sitecustomize.py",
                    root / "sitecustomize.py",
                )
                environment["RETICULATE_PYTHONPATH"] = str(root)
            environment.update(
                MCP_CONSOLE_TEST_SQL_STARTED=str(started.path),
                MCP_CONSOLE_TEST_SQL_RELEASE=str(release.path),
                MCP_CONSOLE_TEST_SQL_BEHAVIOR=behavior,
            )
            args = (
                execution.serve("--writable-root", str(root))
                if execution == SANDBOXED
                else execution.serve()
            )
            with McpClient(binary, args, environment, root) as client:
                client.initialize_and_list_tools()
                yield client, started, release
        finally:
            release.release()
            started.close()
            release.close()


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_creates_managed_connection_without_a_send(
    binary: Path, execution: Execution
) -> Transcript:
    for with_r in (True, False):
        with startup_sql_client(binary, execution, with_r, "observe") as (
            client,
            started,
            release,
        ):
            started.wait("actual managed connection without send", timeout=60)
            client.request("ping")
            release.release()
            client.send(sql="SELECT answer FROM startup_catalog")
            assert "42" in last_tool_text(client), client.transcript[-1]
            client.finish()
    return [
        {
            "r_and_python_managed_connections_open_before_send": True,
            "startup_catalog_retained": True,
            "ping_available_during_warmup": True,
        }
    ]


if __name__ == "__main__":
    run_this_suite(__file__)
