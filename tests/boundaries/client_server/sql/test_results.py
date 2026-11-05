"""Retain one native SQL result and consume it through the public helpers."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import R, SQL, requires
from support.suites import run_this_suite


@requires(SQL)
@executions(RUNTIME)
def test_python_consumes_preview_and_bounded_chunks(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary, execution.serve("-c", "python.managed.packages=[]")
    ) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3


                class CountCursor:
                    def __init__(self):
                        self.native = selected.native.cursor()
                        self.closed = False
                        self.fetches = []
                        self.select_on_metadata = False
                        selected.cursors.append(self)

                    def execute(self, source):
                        selected.executions += 1
                        self.native.execute(source)
                        return self

                    @property
                    def description(self):
                        if self.select_on_metadata:
                            self.select_on_metadata = False
                            _console.sql_connection(replacement)
                        return self.native.description

                    def fetchmany(self, n):
                        self.fetches.append(n)
                        rows = self.native.fetchmany(
                            min(n, 2) if getattr(selected, "short", False) else n
                        )
                        self.select_on_metadata = getattr(selected, "select_on_metadata", False)
                        return rows

                    def fetchall(self):
                        self.fetches.append("all")
                        return self.native.fetchall()

                    def close(self):
                        self.closed = True
                        self.native.close()


                class CountConnection:
                    def __init__(self):
                        self.native = sqlite3.connect(":memory:")
                        self.executions = 0
                        self.cursors = []

                    def cursor(self):
                        return CountCursor()


                selected = CountConnection()
                _console.sql_connection(selected)
                """),
        )
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i, 9223372036854775807 AS big, NULL AS missing,
            zeroblob(200) AS payload, 'original' AS c05, 6 AS c06,
            7 AS c07, 8 AS c08, 9 AS c09, 10 AS c10, 11 AS c11,
            12 AS c12, 'complete column' AS c13 FROM rows"""
        )
        assert "additional rows omitted" in last_tool_text(client)
        client.expect(
            "bounded chunks preserve every row and column\n",
            # fmt: python
            python=code("""
                assert selected.executions == 1
                cursor = selected.cursors[-1]
                assert cursor.fetches == [21] and not cursor.closed
                empty = _console.sql_last_result(0)
                assert empty["rows"] == [] and cursor.fetches == [21]
                assert len(empty["columns"]) == 13 and empty["columns"][-1] == "c13"
                first = _console.sql_last_result(5)
                assert cursor.fetches == [21]
                second = _console.sql_last_result(19)
                assert cursor.fetches == [21, 3]
                third = _console.sql_last_result(33)
                assert cursor.fetches == [21, 3, 33]
                assert _console.sql_last_result(1) == empty and cursor.closed
                assert _console.sql_last_result() == empty
                rows = first["rows"] + second["rows"] + third["rows"]
                assert first["columns"] == second["columns"] == third["columns"]
                assert not hasattr(first, "fetch") and not hasattr(first, "close")
                assert [row[0] for row in rows] == list(range(57))
                assert all(row[1] == 9223372036854775807 for row in rows)
                assert all(row[2] is None and row[3] == bytes(200) for row in rows)
                assert all(row[12] == "complete column" for row in rows)
                assert selected.executions == 1
                assert "pandas" not in __import__("sys").modules
                print("bounded chunks preserve every row and column")
                """),
        )
        client.send(sql="SELECT 1 AS id WHERE FALSE")
        client.expect(
            "empty results retain columns and release cursors\n",
            # fmt: python
            python=code("""
                empty = _console.sql_last_result()
                assert empty == {"columns": ("id",), "rows": []}
                assert selected.cursors[-1].closed
                assert first["rows"][0][0] == 0
                print("empty results retain columns and release cursors")
                """),
        )
        client.expect(python="selected.short = True")
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        )
        client.expect(
            "short native batches do not imply exhaustion\n",
            # fmt: python
            python=code("""
                cursor = selected.cursors[-1]
                assert not cursor.closed and cursor.fetches == [21]
                first = _console.sql_last_result(3)["rows"]
                second = _console.sql_last_result(10)["rows"]
                assert len(first) == 3 and len(second) == 2
                assert not cursor.closed and cursor.fetches == [21, 1, 10]
                rest = _console.sql_last_result()["rows"]
                assert first + second + rest == [(i,) for i in range(57)]
                assert cursor.closed
                print("short native batches do not imply exhaustion")
                """),
        )
        client.expect(python="selected.short = False")
        client.send(sql="SELECT 10 AS value UNION ALL SELECT 20")
        client.expect(
            # fmt: python
            python=code("""
                abandoned = selected.cursors[-1]
                saved = _console.sql_last_result(n=1)
                assert saved == {"columns": ("value",), "rows": [(10,)]}
                assert not abandoned.closed
                """),
        )
        client.send(sql="SELECT 30 AS replacement")
        client.expect(
            "replacement cleans up unread rows and preserves returned data\n",
            # fmt: python
            python=code("""
                assert abandoned.closed
                assert _console.sql_last_result() == {"columns": ("replacement",), "rows": [(30,)]}
                assert selected.cursors[-1].closed
                assert saved == {"columns": ("value",), "rows": [(10,)]}
                print("replacement cleans up unread rows and preserves returned data")
                """),
        )
        client.send(
            sql="""WITH RECURSIVE rows(id) AS (
            VALUES (0) UNION ALL SELECT id + 1 FROM rows WHERE id < 150122
            ) SELECT id FROM rows"""
        )
        client.expect(
            "large Python row limits use bounded driver fetches without losing rows\n",
            # fmt: python
            python=code("""
                cursor = selected.cursors[-1]
                data = _console.sql_last_result(10**100)
                assert data == {
                    "columns": ("id",),
                    "rows": [(i,) for i in range(150123)],
                }
                assert max(cursor.fetches) <= 65536 and len(cursor.fetches) > 3
                assert not cursor.closed
                assert _console.sql_last_result(1)["rows"] == [] and cursor.closed
                print("large Python row limits use bounded driver fetches without losing rows")
                """),
        )
        client.expect(
            # fmt: python
            python=code("""
                replacement = sqlite3.connect(":memory:")
                selected.select_on_metadata = True
                """),
        )
        client.send(sql="SELECT 41 AS previous_connection")
        client.expect(
            "selection during metadata reading discards and closes the old result\n",
            # fmt: python
            python=code("""
                assert _console.sql_connection() is replacement
                assert selected.cursors[-1].closed
                try:
                    _console.sql_last_result()
                except RuntimeError as error:
                    assert "No retained" in str(error)
                else:
                    raise AssertionError("metadata callback published a stale result")
                print("selection during metadata reading discards and closes the old result")
                """),
        )
        client.send(sql="SELECT 99 AS selected_connection")
        client.expect(
            # fmt: python
            python=code("""
                assert _console.sql_last_result() == {
                    "columns": ("selected_connection",),
                    "rows": [(99,)],
                }
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_r_duckdb_returns_driver_data_frames(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: r
            r=code("""
                metrics <- new.env()
                metrics$executions <- 0L
                metrics$fetches <- numeric()
                suppressMessages(invisible(trace(
                  "dbSendQueryArrow",
                  where = asNamespace("DBI"),
                  print = FALSE,
                  tracer = quote(
                    .GlobalEnv$metrics$executions <- .GlobalEnv$metrics$executions + 1L
                  )
                )))
                suppressMessages(invisible(trace(
                  "dbFetchArrowChunk",
                  where = asNamespace("DBI"),
                  print = FALSE,
                  tracer = quote(
                    .GlobalEnv$metrics$fetches <- c(
                      .GlobalEnv$metrics$fetches,
                      list(...)$chunk_size
                    )
                  )
                )))
                """),
        )
        client.send(
            sql="""CREATE SEQUENCE executions;
            SELECT i::INTEGER AS id, nextval('executions') AS execution,
                9223372036854775807::BIGINT AS big,
                12345678901234567890123456789.123456789::DECIMAL(38,9) AS exact,
                NULL::VARCHAR AS missing, repeat('x', 1000) AS long_value,
                [i::INTEGER, NULL, i::INTEGER + 2] AS items,
                {'label': 'original', 'id': i::INTEGER} AS person,
                9 AS c09, 10 AS c10, 11 AS c11, 12 AS c12,
                'complete column' AS c13,
                MAP(['key'], [9007199254740993::BIGINT]) AS mapping
            FROM range(57) AS t(i)"""
        )
        assert "9223372036854775807" in last_tool_text(client)
        client.expect(
            "data frames use DuckDB types and retain every row without replay\n",
            # fmt: r
            r=code("""
                zero <- .console$sql_last_result(0L)
                stopifnot(is.data.frame(zero), nrow(zero) == 0L, ncol(zero) == 14L)
                stopifnot(identical(metrics$fetches, 21))
                first <- .console$sql_last_result(5)
                second <- .console$sql_last_result(19L)
                third <- .console$sql_last_result(33L)
                empty <- .console$sql_last_result(1L)
                stopifnot(is.data.frame(first), is.data.frame(second), is.data.frame(third))
                stopifnot(identical(c(nrow(first), nrow(second), nrow(third)), c(5L, 19L, 33L)))
                stopifnot(identical(empty, zero), identical(.console$sql_last_result(), zero))
                stopifnot(metrics$executions == 1L)
                stopifnot(identical(metrics$fetches, c(21, 3, 33, 1)))
                suppressMessages(invisible(untrace("dbSendQueryArrow", where = asNamespace("DBI"))))
                suppressMessages(invisible(untrace("dbFetchArrowChunk", where = asNamespace("DBI"))))
                stopifnot(DBI::dbGetQuery(.console$sql_connection(), "SELECT currval('executions') AS n")$n == 57)
                expected <- function(start, end) DBI::dbGetQuery(.console$sql_connection(), sprintf("
                  SELECT i::INTEGER AS id, i + 1 AS execution,
                    9223372036854775807::BIGINT AS big,
                    12345678901234567890123456789.123456789::DECIMAL(38,9) AS exact,
                    NULL::VARCHAR AS missing, repeat('x', 1000) AS long_value,
                    [i::INTEGER, NULL, i::INTEGER + 2] AS items,
                    {'label': 'original', 'id': i::INTEGER} AS person,
                    9 AS c09, 10 AS c10, 11 AS c11, 12 AS c12,
                    'complete column' AS c13,
                    MAP(['key'], [9007199254740993::BIGINT]) AS mapping
                  FROM range(%d, %d) AS t(i)
                ", start, end))
                stopifnot(identical(first, expected(0L, 5L)))
                stopifnot(identical(second, expected(5L, 24L)))
                stopifnot(identical(third, expected(24L, 57L)))
                stopifnot(identical(zero, expected(0L, 0L)))
                stopifnot(!"arrow" %in% loadedNamespaces())
                writeLines("data frames use DuckDB types and retain every row without replay")
                """),
        )
        client.send(
            sql="""SELECT i::INTEGER AS id FROM range(1, 300006) AS t(i)
            ORDER BY id DESC"""
        )
        client.expect(
            "successive fetches preserve all 300005 rows in source order\n",
            # fmt: r
            r=code("""
                # Each large call spans several Arrow batches; the first also
                # skips the consumed prefix of the saved preview batch.
                sizes <- c(5L, 140000L, 7L, 140000L)
                chunks <- lapply(sizes, function(n) .console$sql_last_result(n)$id)
                chunks[[5L]] <- .console$sql_last_result()$id
                stopifnot(identical(lengths(chunks), c(sizes, 19993L)))
                stopifnot(identical(unlist(chunks, use.names = FALSE), 300005L:1L))
                stopifnot(nrow(.console$sql_last_result()) == 0L)
                writeLines("successive fetches preserve all 300005 rows in source order")
                """),
        )
        client.send(sql="SELECT i::INTEGER AS id FROM range(57) AS t(i)")
        client.expect(
            "large R row limits retrieve the remaining rows\n",
            # fmt: r
            r=code("""
                first <- .console$sql_last_result(5L)
                rest <- .console$sql_last_result(.Machine$double.xmax)
                stopifnot(identical(first$id, 0:4), identical(rest$id, 5:56))
                stopifnot(identical(
                  .console$sql_last_result(.Machine$double.xmax),
                  rest[integer(), , drop = FALSE]
                ))
                writeLines("large R row limits retrieve the remaining rows")
                """),
        )
        client.send(sql="SELECT NULL::DECIMAL(10,2) AS amount WHERE FALSE")
        client.expect(
            "empty results keep the driver's column types\n",
            # fmt: r
            r=code("""
                empty <- .console$sql_last_result()
                expected <- DBI::dbGetQuery(
                  .console$sql_connection(),
                  "SELECT NULL::DECIMAL(10,2) AS amount WHERE FALSE"
                )
                stopifnot(identical(empty, expected))
                writeLines("empty results keep the driver's column types")
                """),
        )
        client.send(
            sql="""SET TimeZone = 'America/New_York';
            SELECT TIMESTAMPTZ '2026-10-18 12:00:00+00' AS zoned FROM range(30)"""
        )
        assert "2026-10-18 08:00:00-04" in last_tool_text(client)
        client.expect(
            "timestamp retrieval uses DuckDB's data-frame conversion\n",
            # fmt: r
            r=code("""
                chunk <- .console$sql_last_result(5L)
                rest <- .console$sql_last_result()
                expected <- DBI::dbGetQuery(.console$sql_connection(),
                  "SELECT TIMESTAMPTZ '2026-10-18 12:00:00+00' AS zoned FROM range(30)")
                stopifnot(identical(rbind(chunk, rest), expected))
                stopifnot(first$id[[1L]] == 0L)
                writeLines("timestamp retrieval uses DuckDB's data-frame conversion")
                """),
        )
        client.send(sql='SELECT 1 AS same, 2 AS same, 3 AS "has space"')
        client.expect(
            "data frames preserve duplicate and quoted column names\n",
            # fmt: r
            r=code("""
                data <- .console$sql_last_result()
                expected <- DBI::dbGetQuery(
                  .console$sql_connection(),
                  'SELECT 1 AS same, 2 AS same, 3 AS "has space"'
                )
                stopifnot(identical(data, expected))
                writeLines("data frames preserve duplicate and quoted column names")
                """),
        )
        client.send(
            sql="""SET memory_limit = '64MB';
            SELECT i FROM range(1000000000000) AS t(i)"""
        )
        client.expect(
            "a bounded data frame does not materialize the complete SQL result\n",
            # fmt: r
            r=code("""
                data <- .console$sql_last_result(3L)
                stopifnot(identical(data$i, as.numeric(0:2)))
                data <- .console$sql_last_result(25L)
                stopifnot(identical(data$i, as.numeric(3:27)))
                .console$sql_connection(NULL)
                writeLines("a bounded data frame does not materialize the complete SQL result")
                """),
        )
        client.send(sql="SELECT union_value(number := 42) AS tagged FROM range(30)")
        client.expect(
            "a driver conversion error discards the retained result\n",
            # fmt: r
            r=code("""
                message <- tryCatch(.console$sql_last_result(1L), error = conditionMessage)
                stopifnot(grepl("UNION", message, fixed = TRUE))
                message <- tryCatch(.console$sql_last_result(), error = conditionMessage)
                stopifnot(grepl("No retained", message, fixed = TRUE))
                writeLines("a driver conversion error discards the retained result")
                """),
        )
        client.expect(
            # fmt: r
            r=code("""
                configured <- DBI::dbConnect(duckdb::duckdb(), bigint = "integer64")
                .console$sql_connection(configured)
                """),
        )
        client.send(
            sql="""SELECT i::INTEGER AS id, 9007199254740993::BIGINT AS big
            FROM range(30) AS t(i)"""
        )
        client.expect(
            "connection type options apply to retrieved data frames\n",
            # fmt: r
            r=code("""
                first <- .console$sql_last_result(5)
                rest <- .console$sql_last_result()
                expected <- DBI::dbGetQuery(configured,
                  "SELECT i::INTEGER AS id, 9007199254740993::BIGINT AS big FROM range(30) AS t(i)")
                stopifnot(inherits(first$big, "integer64"))
                stopifnot(identical(as.character(first$big), rep("9007199254740993", 5)))
                # Binding driver batches can change their implicit row names.
                # Compare all column values and types with the native driver.
                stopifnot(identical(as.list(rbind(first, rest)), as.list(expected)))
                .console$sql_connection(NULL)
                DBI::dbDisconnect(configured, shutdown = TRUE)
                writeLines("connection type options apply to retrieved data frames")
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_dbi_bounds_fetches_and_preserves_transactions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            requirements={"r": ["RSQLite"]},
            # fmt: r
            r=code("""
                library(DBI)
                metrics <- new.env()
                metrics$executions <- 0L
                metrics$fetches <- numeric()
                metrics$clears <- 0L
                metrics$gate <- FALSE
                metrics$fail <- FALSE
                metrics$select_on <- NULL
                metrics$cleanup_error <- FALSE
                setClass(
                  "CountConnection",
                  contains = "DBIConnection",
                  slots = c(native = "ANY")
                )
                setClass("CountResult", contains = "DBIResult", slots = c(native = "ANY"))
                setMethod(
                  "dbSendQuery",
                  c("CountConnection", "character"),
                  function(conn, statement, ...) {
                    metrics$executions <- metrics$executions + 1L
                    new("CountResult", native = DBI::dbSendQuery(conn@native, statement, ...))
                  }
                )
                setMethod("dbFetch", "CountResult", function(res, n = -1, ...) {
                  metrics$fetches <- c(metrics$fetches, n)
                  if (metrics$fail) {
                    stop("native fetch failed")
                  }
                  if (metrics$gate) {
                    metrics$gate <- FALSE
                    readline("fetch> ")
                  }
                  DBI::dbFetch(res@native, n = n, ...)
                })
                setMethod("dbColumnInfo", "CountResult", function(res, ...) {
                  DBI::dbColumnInfo(res@native)
                })
                setMethod("dbIsValid", "CountResult", function(dbObj, ...) {
                  DBI::dbIsValid(dbObj@native)
                })
                setMethod("dbIsValid", "CountConnection", function(dbObj, ...) {
                  DBI::dbIsValid(dbObj@native)
                })
                setMethod("dbHasCompleted", "CountResult", function(res, ...) {
                  completed <- DBI::dbHasCompleted(res@native)
                  if (identical(metrics$select_on, "completed")) {
                    metrics$select_on <- NULL
                    .console$sql_connection(next_connection)
                  }
                  completed
                })
                setMethod("dbClearResult", "CountResult", function(res, ...) {
                  metrics$clears <- metrics$clears + 1L
                  cleared <- DBI::dbClearResult(res@native)
                  if (identical(metrics$select_on, "clear")) {
                    metrics$select_on <- NULL
                    .console$sql_connection(next_connection)
                  }
                  if (metrics$cleanup_error) {
                    metrics$cleanup_error <- FALSE
                    stop("native cleanup failed")
                  }
                  cleared
                })
                sqlite <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
                invisible(DBI::dbExecute(sqlite, "CREATE TABLE transaction_state (id INTEGER)"))
                DBI::dbBegin(sqlite)
                invisible(DBI::dbExecute(sqlite, "INSERT INTO transaction_state VALUES (1)"))
                counted <- new("CountConnection", native = sqlite)
                .console$sql_connection(counted)
                """),
        )
        client.send(
            sql="""WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i, NULL AS missing, 'complete' AS label FROM rows"""
        )
        client.expect(
            "DBI fetch bounds and native transaction retained\n",
            # fmt: r
            r=code("""
                stopifnot(identical(metrics$fetches, 21), metrics$clears == 0L)
                stopifnot(nrow(.console$sql_last_result(0L)) == 0L)
                a <- .console$sql_last_result(5L)
                stopifnot(identical(metrics$fetches, 21))
                b <- .console$sql_last_result(19L)
                stopifnot(identical(metrics$fetches, c(21, 3)))
                c <- .console$sql_last_result(2147483648)
                stopifnot(identical(metrics$fetches, c(21, 3, 65536)))
                rows <- rbind(a, b, c)
                stopifnot(identical(rows$i, 0:56), all(is.na(rows$missing)))
                stopifnot(identical(names(rows), c("i", "missing", "label")))
                stopifnot(metrics$executions == 1L, metrics$clears == 1L)
                stopifnot(nrow(.console$sql_last_result()) == 0L)
                DBI::dbRollback(sqlite)
                stopifnot(DBI::dbGetQuery(sqlite, "SELECT count(*) AS n FROM transaction_state")$n == 0L)
                writeLines("DBI fetch bounds and native transaction retained")
                """),
        )
        client.send(sql="SELECT 1 AS id WHERE FALSE")
        client.expect(
            "empty DBI frame preserves columns\n",
            # fmt: r
            r=code("""
                result <- .console$sql_last_result()
                stopifnot(identical(names(result), "id"), nrow(result) == 0L)
                writeLines("empty DBI frame preserves columns")
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        client.expect(r="metrics$gate <- TRUE")
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "DBI retrieval reached its interrupt gate",
            r=".console$sql_last_result(30L)",
            timeout_ms=0,
        )
        client.expect("\n", control="interrupt")
        client.expect(
            "interrupted DBI fetch closes its result\n",
            # fmt: r
            r=code("""
                stopifnot(grepl("No retained", tryCatch(.console$sql_last_result(1L), error = conditionMessage)))
                stopifnot(metrics$clears == 3L, DBI::dbIsValid(sqlite))
                writeLines("interrupted DBI fetch closes its result")
                """),
        )
        client.send(sql=query)
        client.expect(
            "failed DBI fetch closes its result\n",
            # fmt: r
            r=code("""
                metrics$fail <- TRUE
                message <- tryCatch(.console$sql_last_result(30L), error = conditionMessage)
                stopifnot(identical(message, "native fetch failed"), metrics$clears == 4L)
                metrics$fail <- FALSE
                writeLines("failed DBI fetch closes its result")
                """),
        )
        client.send(sql=query)
        client.expect(
            # fmt: r
            r=code("""
                saved <- .console$sql_last_result(n = 5L)
                stopifnot(identical(saved$i, 0:4), metrics$clears == 4L)
                """),
        )
        client.send(sql="SELECT 99 AS replacement")
        client.expect(
            "DBI replacement clears unread results without changing returned data\n",
            # fmt: r
            r=code("""
                replacement <- .console$sql_last_result()
                stopifnot(identical(replacement$replacement, 99L))
                stopifnot(identical(saved$i, 0:4), metrics$clears == 6L)
                writeLines(
                  "DBI replacement clears unread results without changing returned data"
                )
                """),
        )
        client.expect(
            r='next_connection <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")'
        )
        for callback in ("completed", "clear"):
            client.expect(
                # fmt: r
                r=code(f"""
                    .console$sql_connection(counted)
                    clears_before <- metrics$clears
                    metrics$select_on <- "{callback}"
                    """),
            )
            client.send(sql=query if callback == "completed" else "SELECT 42 AS id")
            client.expect(
                f"DBI {callback} callback discards the old result\n",
                # fmt: r
                r=code(f"""
                    stopifnot(identical(.console$sql_connection(), next_connection))
                    message <- tryCatch(.console$sql_last_result(), error = conditionMessage)
                    stopifnot(is.character(message), grepl("No retained", message, fixed = TRUE))
                    stopifnot(metrics$clears == clears_before + 1L, DBI::dbIsValid(sqlite))
                    writeLines("DBI {callback} callback discards the old result")
                    """),
            )
            client.send(sql="SELECT 123 AS selected_connection")
            client.expect(
                r="stopifnot(identical(.console$sql_last_result()$selected_connection, 123L))"
            )
        for action in ("query", "selection"):
            client.expect(r=".console$sql_connection(counted)")
            client.send(sql=query)
            client.expect(r="metrics$cleanup_error <- TRUE")
            if action == "query":
                client.send(sql="SELECT 88 AS after_cleanup")
                assert "Error: native cleanup failed" in last_tool_text(client)
            else:
                client.expect(
                    "Error: native cleanup failed\n",
                    r=".console$sql_connection(next_connection)",
                )
                client.expect(
                    r="stopifnot(identical(.console$sql_connection(), next_connection))"
                )
                client.send(sql="SELECT 88 AS after_cleanup")
            client.expect(
                f"DBI cleanup error is reported and the {action} continues\n",
                # fmt: r
                r=code(f"""
                    stopifnot(identical(.console$sql_last_result()$after_cleanup, 88L))
                    stopifnot(DBI::dbIsValid(sqlite), DBI::dbIsValid(next_connection))
                    writeLines("DBI cleanup error is reported and the {action} continues")
                    """),
            )
        client.expect(
            # fmt: r
            r=code("""
                .console$sql_connection(NULL)
                DBI::dbDisconnect(sqlite)
                DBI::dbDisconnect(next_connection)
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_bridged_connections_preserve_results_and_transactions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            requirements={"r": ["RSQLite"]},
            # fmt: r
            r=code("""
                selected_r <- DBI::dbConnect(RSQLite::SQLite(), ":memory:")
                invisible(DBI::dbExecute(selected_r, "CREATE TABLE values_table (id INTEGER)"))
                DBI::dbBegin(selected_r)
                invisible(DBI::dbExecute(selected_r, "INSERT INTO values_table VALUES (42)"))
                .console$sql_connection(selected_r)
                """),
        )
        client.expect(None, sql="SELECT id FROM values_table")
        client.expect(
            # fmt: python
            python=code("""
                r = 42
                borrowed_r = _console.sql_connection()
                assert r == 42
                """),
        )
        client.expect(r="stopifnot(identical(.console$sql_last_result()$id, 42L))")
        client.expect(None, sql="SELECT id FROM values_table")
        client.expect(
            # fmt: python
            python=code("""
                _console.sql_connection(borrowed_r)
                assert r == 42
                """),
        )
        client.expect(
            "bridged R selection discards results without committing\n",
            # fmt: r
            r=code("""
                message <- tryCatch(.console$sql_last_result(), error = conditionMessage)
                stopifnot(grepl("No retained", message, fixed = TRUE))
                DBI::dbRollback(selected_r)
                stopifnot(
                  DBI::dbGetQuery(selected_r, "SELECT count(*) AS n FROM values_table")$n == 0
                )
                writeLines("bridged R selection discards results without committing")
                """),
        )
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3

                selected_python = sqlite3.connect(":memory:")
                selected_python.execute("CREATE TABLE values_table (id INTEGER)")
                selected_python.execute("INSERT INTO values_table VALUES (42)")
                _console.sql_connection(selected_python)
                """),
        )
        client.expect(None, sql="SELECT id FROM values_table")
        client.expect(r="borrowed_python <- .console$sql_connection()")
        client.expect(
            python='assert _console.sql_last_result() == {"columns": ("id",), "rows": [(42,)]}'
        )
        client.expect(None, sql="SELECT id FROM values_table")
        client.expect(r=".console$sql_connection(borrowed_python)")
        client.expect(
            "bridged Python selection discards results without committing\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError as error:
                    assert "No retained" in str(error)
                else:
                    raise AssertionError("bridged selection retained unread rows")
                assert selected_python.in_transaction
                selected_python.rollback()
                assert selected_python.execute("SELECT count(*) FROM values_table").fetchone() == (0,)
                print("bridged Python selection discards results without committing")
                """),
        )
        return client.finish()


@requires(R, SQL)
@executions(RUNTIME)
def test_invalidates_closes_and_restarts(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3

                connection = sqlite3.connect(":memory:")
                connection.execute("CREATE TABLE values_table (id INTEGER)")
                connection.execute("INSERT INTO values_table VALUES (42)")
                _console.sql_connection(connection)
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        client.expect(
            "retrieval returns data and preserves the transaction\n",
            # fmt: python
            python=code("""
                previous = _console.sql_last_result(5)
                assert previous == {"columns": ("i",), "rows": [(i,) for i in range(5)]}
                assert connection.in_transaction
                print("retrieval returns data and preserves the transaction")
                """),
        )
        client.send(sql=query)
        client.send(sql="SELECT * FROM missing_table")
        assert "missing_table" in last_tool_text(client)
        client.expect(
            "failed SQL invalidates the previous result\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("stale result survived failed SQL")
                assert previous["rows"] == [(i,) for i in range(5)]
                assert connection.in_transaction
                print("failed SQL invalidates the previous result")
                """),
        )
        client.send(sql=query)
        client.expect(r=".console$sql_connection(NULL)")
        client.expect(
            "R selection invalidates Python results without committing\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result(1)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("stale Python result")
                assert connection.in_transaction
                connection.rollback()
                assert connection.execute("SELECT count(*) FROM values_table").fetchone() == (0,)
                print("R selection invalidates Python results without committing")
                """),
        )
        client.send(sql="SELECT i::INTEGER AS id FROM range(57) AS t(i)")
        client.expect(python="_console.sql_connection(connection)")
        client.expect(
            "Python selection invalidates R results\n",
            # fmt: r
            r=code("""
                message <- tryCatch(.console$sql_last_result(1L), error = conditionMessage)
                stopifnot(grepl("No retained", message))
                writeLines("Python selection invalidates R results")
                """),
        )
        client.expect(
            # fmt: python
            python=code("""
                def select_again():
                    _console.sql_connection(connection)
                    return 41


                connection.create_function("select_again", 0, select_again)
                """),
        )
        client.send(sql="SELECT select_again() AS value")
        client.expect(
            "selection during execution does not retain a stale result\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("selection during SQL retained a stale result")
                print("selection during execution does not retain a stale result")
                """),
        )
        client.expect(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[done]",
            control="restart",
            python="assert 'previous' not in globals()",
        )
        client.expect(
            "restart has no retained result\n",
            # fmt: python
            python=code("""
                try:
                    _console.sql_last_result()
                except RuntimeError as error:
                    assert "No retained" in str(error)
                else:
                    raise AssertionError("result survived restart")
                print("restart has no retained result")
                """),
        )
        return client.finish()


@requires(SQL)
@executions(RUNTIME)
def test_slow_fetch_polls_interrupts_and_releases_cursors(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                import sqlite3


                class GatedCursor:
                    def __init__(self):
                        self.native = connection.native.cursor()
                        self.calls = 0
                        self.closed = False
                        connection.latest = self

                    def execute(self, source):
                        self.native.execute(source)
                        return self

                    @property
                    def description(self):
                        return self.native.description

                    def fetchmany(self, n):
                        self.calls += 1
                        if self.calls > 1:
                            input("fetch> ")
                        return self.native.fetchmany(n)

                    def close(self):
                        self.closed = True
                        self.native.close()


                class GatedConnection:
                    def __init__(self):
                        self.native = sqlite3.connect(":memory:")

                    def cursor(self):
                        return GatedCursor()


                connection = GatedConnection()
                _console.sql_connection(connection)
                """),
        )
        query = """WITH RECURSIVE rows(i) AS (
            VALUES (0) UNION ALL SELECT i + 1 FROM rows WHERE i < 56
        ) SELECT i FROM rows"""
        client.send(sql=query)
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "retrieval entered its native fetch gate",
            python="data = _console.sql_last_result(30)",
            timeout_ms=0,
        )
        client.expect(stdin="continue\n")
        client.expect(
            "resumed retrieval keeps the preview prefix\n",
            # fmt: python
            python=code("""
                assert data == {"columns": ("i",), "rows": [(i,) for i in range(30)]}
                assert connection.latest.calls == 2
                _console.sql_connection(connection)
                assert connection.latest.closed
                print("resumed retrieval keeps the preview prefix")
                """),
        )
        client.send(sql=query)
        wait_for_evaluation_output(
            client,
            '[input requested: "fetch> "]\n[waiting for stdin]',
            "retrieval entered its interrupt gate",
            python="_console.sql_last_result(30)",
            timeout_ms=0,
        )
        client.send(control="interrupt")
        assert "KeyboardInterrupt" in last_tool_text(client)
        client.expect(
            "interrupted retrieval releases the cursor\n",
            # fmt: python
            python=code("""
                assert connection.latest.closed
                try:
                    _console.sql_last_result(1)
                except RuntimeError as error:
                    assert "No retained" in str(error)
                else:
                    raise AssertionError("interrupted cursor remained usable")
                print("interrupted retrieval releases the cursor")
                """),
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
