#!/usr/bin/env -S uv run --script

import os
import re
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.requirements import FILE_READ_DENIAL, POSIX, R, SQL, requires
from support.assertions import last_tool_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import build_interposer
from support.normalization import (
    code,
    normalize_duckdb_progress,
    normalize_process_diagnostic,
    normalize_trailing_spaces,
)
from support.r import isolated_r_home, r_test_environment
from support.records import McpTranscript, Transcript, TranscriptWithCompanions
from support.previews import assert_preview, cell_text, normalize_preview_paths
from support.suites import run_this_suite
from boundaries.client_server.server.test_no_r import no_r_environment


@requires(R, SQL, FILE_READ_DENIAL)
@executions(DIRECT)
def test_prepares_extensions_while_unrelated_metadata_is_inaccessible(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    transcripts = {}
    for provider in ("python", "r"):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary)
            # Captured cache paths may contain SQL string delimiters.
            cache = workspace / "extensions'cache"
            environment = (
                os.environ.copy() if provider == "r" else no_r_environment(workspace)
            )
            environment["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(cache)
            with McpClient(binary, execution.serve(), environment, workspace) as client:
                client.initialize_and_list_tools()
                if provider == "r":
                    # fmt: r
                    check = code(r"""
                        con <- .console$sql_connection()
                        result <- DBI::dbGetQuery(con, "SELECT 42 AS answer")
                        stopifnot(result$answer == 42L)
                        cat("connection retained\n")
                        """)
                else:
                    # fmt: python
                    check = code("""
                        assert _console.sql_connection().execute("SELECT 42").fetchone() == (42,)
                        print("connection retained")
                        """)
                client.expect("connection retained\n", **{provider: check})
                installed = list(cache.glob("v*/*/sqlite_scanner.duckdb_extension"))
                assert len(installed) == 1, installed
                extension = installed[0].with_name(
                    "vss.duckdb_extension.tmp-fixture.duckdb_extension"
                )
                extension.write_bytes(b"unrelated in-progress installation")
                metadata = extension.with_name(extension.name + ".info")
                metadata.write_bytes(b"unrelated in-progress installation metadata")
                # A concurrent installer owns this unrelated metadata. Windows
                # sharing/byte-range locks and Unix permissions both deny reads.
                with metadata.open("r+b") as held:
                    if os.name == "nt":
                        import msvcrt

                        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        metadata.chmod(0)
                    try:
                        client.expect(
                            "[prepared]", requirements={"duckdb": ["core_functions"]}
                        )
                        client.expect("connection retained\n", **{provider: check})
                    finally:
                        if os.name == "nt":
                            held.seek(0)
                            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
                        else:
                            metadata.chmod(0o600)
                transcripts[f"{provider}.yaml"] = McpTranscript(
                    [
                        {
                            "scenario": f"Prepare an extension through the {provider} provider.",
                            "fixture": "An unrelated installer metadata file remains unreadable while preparation runs.",
                        },
                        *client.finish(),
                    ]
                )
    return TranscriptWithCompanions(transcripts.pop("r.yaml").transcript, transcripts)


def extension_failure_environment(workspace: Path) -> dict[str, str]:
    environment, real_rscript = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    # Concurrent real-library cases can install or hold locks on extension info.
    environment["MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY"] = str(
        workspace / "extensions"
    )
    selected_home = isolated_r_home(workspace, environment)
    selected_rscript = selected_home / "bin/Rscript"
    selected_rscript.unlink()
    selected_rscript.symlink_to(
        Path(__file__).resolve().parents[3] / "fixtures/fail_duckdb_rscript.py"
    )
    # Give transaction rollback a deterministic materializer failure.
    environment["MCP_CONSOLE_TEST_REAL_RSCRIPT"] = str(real_rscript)
    environment["MCP_CONSOLE_TEST_DUCKDB_FAIL_EXTENSION"] = (
        "not_a_real_duckdb_extension"
    )
    return environment


@requires(R, SQL, POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_adds_r_and_duckdb_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = extension_failure_environment(workspace)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = str(ambient_library)
        environment["R_LIBS_SITE"] = str(ambient_library)
        environment["R_LIBS_USER"] = str(ambient_library)
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        client.expect("[done]", r="restart_marker <- 42L")

        client.send(
            control="restart",
            requirements={
                "r": ["praise"],
                "duckdb": ["not_a_real_duckdb_extension"],
            },
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert failure.endswith(
            "fixture DuckDB extension not_a_real_duckdb_extension is unavailable"
        ), failure
        result["content"][0]["text"] = normalize_process_diagnostic(failure)

        client.send(r="identical(restart_marker, 42L)")
        assert last_tool_text(client) == "[1] TRUE\n"

        client.send(
            control="restart",
            requirements={"r": ["praise"], "duckdb": ["fts"]},
        )
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        client.send(
            r=(
                "!exists('restart_marker') && "
                "requireNamespace('praise', quietly = TRUE)"
            )
        )
        assert last_tool_text(client) == "[1] TRUE\n"

        sql = code(r"""
            CREATE TABLE restart_documents AS
            SELECT 1 AS id, 'duckdb restart requirements' AS body
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        client.send(sql="PRAGMA create_fts_index('restart_documents', 'id', 'body')")
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            SELECT count(*) AS matches
            FROM restart_documents
            WHERE fts_main_restart_documents.match_bm25(id, 'duckdb') IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", "1"]
        return client.finish()


@requires(R, SQL, POSIX)
@executions(DIRECT, SANDBOXED)
def test_prepares_and_loads_duckdb_extensions(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment = extension_failure_environment(workspace)
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()

        sql = code(r"""
            CREATE TABLE retained_state AS
            SELECT
              CAST(42 AS INTEGER) AS answer,
              'duckdb extension preparation' AS body
            UNION ALL
            SELECT 7, 'other words'
            """)
        client.expect("[done]", sql=sql)

        client.send(
            requirements={"duckdb": ["json"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.send(
            requirements={"r": ["praise"]},
        )
        assert last_tool_text(client) == "[prepared]"

        client.send(
            sql="CREATE TABLE failed_requirement_cell(answer INTEGER)",
            requirements={"duckdb": ["not_a_real_duckdb_extension"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        failure = result["content"][0]["text"]
        assert failure.startswith("DuckDB extension resolution failed with "), failure
        assert failure.endswith(
            "fixture DuckDB extension not_a_real_duckdb_extension is unavailable"
        ), failure
        result["content"][0]["text"] = normalize_process_diagnostic(failure)

        client.send(
            sql=(
                "SELECT count(*) AS side_effects FROM duckdb_tables() "
                "WHERE table_name = 'failed_requirement_cell'"
            )
        )
        assert last_tool_text(client).splitlines()[-1].split() == ["1", "0"]

        sql = code(r"""
            PRAGMA create_fts_index('retained_state', 'answer', 'body')
            """)
        client.send(sql=sql, requirements={"duckdb": ["fts"]})
        assert last_tool_text(client) == "[done]"

        client.send(
            requirements={"duckdb": ["fts"]},
        )
        assert last_tool_text(client) == "[prepared]"

        sql = code(r"""
            SELECT
              'loaded' AS verified
            FROM retained_state
            CROSS JOIN duckdb_extensions() AS extensions
            WHERE retained_state.answer = 42
              AND extensions.extension_name = 'fts'
              AND extensions.loaded
              AND fts_main_retained_state.match_bm25(
                retained_state.answer,
                'duckdb'
              ) IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", '"loaded"']

        client.send(control="restart")
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )

        client.send(sql="LOAD fts")
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            CREATE TABLE replacement_state AS
            SELECT
              CAST(42 AS INTEGER) AS answer,
              'duckdb extension preparation' AS body
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            PRAGMA create_fts_index('replacement_state', 'answer', 'body')
            """)
        client.send(sql=sql)
        assert last_tool_text(client) == "[done]"
        sql = code(r"""
            SELECT 'loaded' AS verified
            FROM replacement_state
            CROSS JOIN duckdb_extensions() AS extensions
            WHERE extensions.extension_name = 'fts'
              AND extensions.loaded
              AND fts_main_replacement_state.match_bm25(
                replacement_state.answer,
                'duckdb'
              ) IS NOT NULL
            """)
        client.send(sql=sql)
        preview = last_tool_text(client)
        assert preview.splitlines()[-1].split() == ["1", '"loaded"']
        return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_sends_sql_cell_with_initial_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()

    sql = code(r"""
        LOAD fts;
        SELECT count(*) AS loaded
        FROM duckdb_extensions()
        WHERE extension_name = 'fts' AND loaded
        """)
    client.expect(
        "# A tibble: 1 × 1\n   loaded\n  <int64>\n1       1\n",
        sql=sql,
        requirements={"duckdb": ["fts"]},
    )
    return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_queries_a_ragnar_store_created_in_r(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    # These R/SQL operations do not use ragnar's Python document converters.
    environment["MCP_CONSOLE_LANGUAGES"] = "r,sql"
    temporary = tempfile.TemporaryDirectory()
    workspace = Path(temporary.name)
    client = McpClient(
        binary,
        execution.serve(),
        environment,
        current_directory=workspace,
    )
    client.initialize_and_list_tools()

    sql = code(r"""
        CREATE TEMP TABLE before_prepare AS
        SELECT CAST(42 AS INTEGER) AS value
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    client.send(
        requirements={"r": ["ragnar"]},
    )
    assert last_tool_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
        stopifnot(
          identical(
            DBI::dbGetQuery(
              .console$sql_connection(),
              "SELECT value FROM before_prepare"
            )$value,
            42L
          ),
          identical(dirname(find.package("ragnar")), .libPaths()[[managed_index]])
        )
        embed_banana <- function(x) {
          out <- matrix(1, nrow = length(x), ncol = 2L)
          out[, 1L] <- grepl("banana", x, ignore.case = TRUE)
          out
        }
        store_path <- file.path(tempdir(), "knowledge.ragnar.duckdb")
        store <- suppressMessages(ragnar::ragnar_store_create(
          location = store_path,
          embed = embed_banana,
          embedding_size = 2L
        ))
        documents <- list(
          ragnar::MarkdownDocument(
            "# Alpha\n\nApples are red fruit.",
            origin = "alpha.md"
          ),
          ragnar::MarkdownDocument(
            "# Beta\n\nBananas are yellow fruit.",
            origin = "beta.md"
          )
        )
        for (document in documents) {
          chunks <- ragnar::markdown_chunk(
            document,
            target_size = 1000L,
            target_overlap = 0
          )
          ragnar::ragnar_store_insert(store, chunks)
        }
        reader <- ragnar::ragnar_store_connect(store_path, read_only = FALSE)
        stopifnot(!reticulate::py_available(initialize = FALSE))
        writeLines("ragnar store ready")
        """)
    client.send(r=r)
    assert normalize_duckdb_progress(client) == "ragnar store ready\n"
    client.expect("[prepared]", requirements={"duckdb": ["fts", "vss"]})
    # fmt: r
    r = code(r"""
        stopifnot(
          DBI::dbIsValid(store@con),
          DBI::dbIsValid(reader@con),
          DBI::dbGetQuery(store@con, "SELECT count(*) AS n FROM chunks")$n == 2,
          DBI::dbGetQuery(
            .console$sql_connection(),
            "SELECT value FROM before_prepare"
          )$value ==
            42L
        )
        ragnar::ragnar_store_build_index(store, type = c("vss", "fts"))
        retrieved <- ragnar::ragnar_retrieve(store, "bananas", top_k = 1L)
        stopifnot(identical(retrieved$origin, "beta.md"))
        .console$sql_connection(reader@con)
        stopifnot(identical(.console$sql_connection(), reader@con))
        writeLines("selected the open ragnar reader")
        """)
    client.send(r=r)
    assert normalize_duckdb_progress(client) == "selected the open ragnar reader\n"
    client.send(sql="SELECT origin FROM chunks ORDER BY origin")
    preview = normalize_trailing_spaces(client)
    assert [line.split() for line in preview.splitlines()[-2:]] == [
        ["1", '"alpha.md"'],
        ["2", '"beta.md"'],
    ]
    # fmt: r
    r = code(r"""
        .console$sql_connection(NULL)
        connection <- .console$sql_connection()
        stopifnot(
          !identical(connection, store@con),
          !identical(connection, reader@con)
        )
        # Windows requires the other DuckDB instances to release the file.
        DBI::dbDisconnect(reader@con)
        DBI::dbDisconnect(store@con)
        invisible(DBI::dbExecute(
          connection,
          paste(
            "ATTACH",
            DBI::dbQuoteString(connection, store_path),
            "AS knowledge (READ_ONLY)"
          )
        ))
        invisible(DBI::dbExecute(connection, "USE knowledge"))
        stopifnot(!reticulate::py_available(initialize = FALSE))
        writeLines("ragnar store ready")
        """)
    client.send(r=r)
    marker = "ragnar store ready\n"
    assert normalize_duckdb_progress(client) == marker

    sql = code(r"""
        LOAD fts;
        LOAD vss;
        SELECT
          document.origin AS vss_origin,
          nearest.distance,
          (
            SELECT origin
            FROM chunks
            WHERE fts_main_chunks.match_bm25(chunk_id, 'bananas') IS NOT NULL
            ORDER BY origin
            LIMIT 1
          ) AS fts_origin,
          (SELECT value FROM before_prepare) AS retained
        FROM (
          SELECT
            doc_id,
            array_cosine_distance(
              embedding,
              [1, 1]::FLOAT[2]
            ) AS distance
          FROM embeddings
          ORDER BY distance
          LIMIT 1
        ) AS nearest
        JOIN documents AS document USING (doc_id)
        ORDER BY nearest.distance
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert preview.splitlines()[-1].split() == [
        "1",
        '"beta.md"',
        "0.0",
        '"beta.md"',
        "42",
    ]
    assert '"alpha.md"' not in preview
    transcript = client.finish()
    temporary.cleanup()
    return transcript


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_evaluates_queries_in_a_persistent_catalog(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment = os.environ.copy()
        environment["R_LIBS"] = str(ambient_library)
        environment["R_LIBS_SITE"] = str(ambient_library)
        environment["R_LIBS_USER"] = str(ambient_library)
        environment["RETICULATE_PYTHON"] = ""
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        sql = code(r"""
            CREATE TABLE answers AS SELECT CAST(42 AS INTEGER) AS answer
            """)
        client.send(sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            INSERT INTO answers VALUES (7)
            """)
        client.send(sql=sql)
        output = last_tool_text(client)
        assert output == "[done]", output

        sql = code(r"""
            SELECT answer FROM answers
            ORDER BY answer DESC
            """)
        client.send(sql=sql)
        return client.finish()


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
def test_interrupts_running_sql_query(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary_path = Path(temporary_directory)
        environment, _ = r_test_environment()
        environment["TMPDIR"] = temporary_directory
        environment["MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"] = str(
            build_interposer(temporary_path, "sql_interrupt_checkpoint")
        )
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=temporary_path,
        )
        passed = False
        started = None
        try:
            client.initialize_and_list_tools()
            # Place the FIFO in the worker's writable temporary directory.
            # fmt: r
            r = code(r"""
                interrupt_started <- file.path(tempdir(), "sql-interrupt-started")
                cat(interrupt_started, "\n", sep = "")
                """)
            client.send(r=r)
            started = FifoCheckpoint.create(Path(last_tool_text(client).strip()))
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<SQL interrupt checkpoint>\n"
            )
            sql = code(r"""
                CREATE TABLE interrupt_state AS
                SELECT CAST(42 AS INTEGER) AS answer
                """)
            client.send(sql=sql)
            assert last_tool_text(client) == "[done]"

            # fmt: r
            r = code(r"""
                dyn.load(Sys.getenv("MCP_CONSOLE_SQL_INTERRUPT_LIBRARY"))
                invisible(DBI::dbExecute(.console$sql_connection(), "SET threads = 1"))
                invisible(DBI::dbExecute(
                  .console$sql_connection(),
                  "SET enable_progress_bar = true"
                ))
                invisible(DBI::dbExecute(
                  .console$sql_connection(),
                  "SET progress_bar_time = 0"
                ))
                query_started <- FALSE
                options(duckdb.progress_display = function(percentage) {
                  if (!query_started && percentage < 100) {
                    query_started <<- TRUE
                    invisible(.C(
                      "wait_for_sql_interrupt",
                      interrupt_started
                    ))
                  }
                })
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]"

            sql = code(r"""
                SELECT sum(i) AS total FROM range(1000000000000) AS t(i)
                """)
            client.send(sql=sql, timeout_ms=0)
            assert (
                without_elapsed(last_tool_text(client))
                == "\n[running; poll with an empty send]"
            )
            # A marker in a preceding statement can race DuckDB's reset of its
            # interrupt flag. Hold this query until its active handler has run.
            started.wait("DuckDB query reached its progress callback")
            result = client.send(
                control="interrupt",
                timeout_ms=30_000,
            )
            assert result["isError"] is False, result
            output = last_tool_text(client)
            assert output in {"\n", "\n\n"}, repr(output)
            # DuckDB and R can each publish the native interrupt newline.
            result["content"][0]["text"] = "\n"

            client.send(sql="SELECT answer FROM interrupt_state")
            assert "42" in last_tool_text(client)
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)
            if started is not None:
                started.close()


@requires(R, SQL)
@executions(SANDBOXED)
def test_resolves_r_bindings(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        measurements <- data.frame(
          label = c("a", "b"),
          value = c(2L, 5L)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT label, value * 10 AS scaled
        FROM measurements
        ORDER BY label
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"a"' in preview and "20" in preview
    assert '"b"' in preview and "50" in preview

    # fmt: r
    r = code(r"""
        rebound_measurements <- data.frame(value = 2L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        CREATE VIEW live_rebound_measurements AS
        SELECT value FROM rebound_measurements
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        rebound_measurements <- data.frame(value = 7L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT value FROM live_rebound_measurements
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert preview.splitlines()[-1].split() == ["1", "7"]

    # fmt: r
    r = code(r"""
        values <- data.frame(origin = "r")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        CREATE TABLE values AS SELECT 'sql' AS origin;
        SELECT origin FROM values
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"sql"' in preview
    assert '"r"' not in preview

    # fmt: r
    r = code(r"""
        connection <- data.frame(name = "connection")
        source <- data.frame(name = "source")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT name FROM connection
        UNION ALL
        SELECT name FROM source
        ORDER BY name
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert '"connection"' in preview
    assert '"source"' in preview
    return client.finish()


@requires(R, SQL)
@executions(SANDBOXED)
def test_exposes_catalog_as_lazy_r_relations(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        CREATE TABLE sql_values AS
        SELECT * FROM (VALUES ('a', 2), ('b', 5)) AS values(label, value);
        CREATE VIEW live_sql_values AS SELECT * FROM sql_values
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        connection <- .console$sql_connection()
        table_values <- dplyr::tbl(connection, "sql_values")
        lazy_values <- dplyr::tbl(connection, "live_sql_values") |>
          dplyr::mutate(doubled = value * 2L)
        cat(
          c(
            "same connection: ",
            identical(connection, .console$sql_connection()),
            "\n"
          ),
          c("lazy table: ", inherits(table_values, "tbl_lazy"), "\n"),
          c("lazy view: ", inherits(lazy_values, "tbl_lazy"), "\n"),
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == (
        """same connection: TRUE
lazy table: TRUE
lazy view: TRUE
"""
    )

    sql = code(r"""
        INSERT INTO sql_values VALUES ('c', 11)
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        values <- lazy_values |>
          dplyr::arrange(label) |>
          dplyr::collect()
        writeLines(paste(values$label, values$value, values$doubled, sep = ":"))
        """)
    client.send(r=r)
    assert (
        last_tool_text(client)
        == """a:2:4
b:5:10
c:11:22
"""
    )
    # fmt: r
    r = code(r"""
        rm(list = ls())
        values <- DBI::dbGetQuery(
          .console$sql_connection(),
          "SELECT label, value FROM sql_values ORDER BY label"
        )
        writeLines(paste(values$label, values$value, sep = ":"))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "a:2\nb:5\nc:11\n"
    return client.finish()


@requires(R, SQL)
@executions(SANDBOXED)
def test_recovers_from_sql_errors(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        SELECT * FROM table_that_does_not_exist
        """)
    client.send(sql=sql)
    result = client.transcript[-1]["result"]
    assert result.get("isError") is not True, result
    output = result["content"][0]["text"]
    assert output.startswith("Error: "), output
    assert "Catalog Error:" in output, output
    assert "table_that_does_not_exist" in output, output

    sql = code(r"""
        SELECT CAST(42 AS INTEGER) AS answer
        """)
    client.send(sql=sql)
    return client.finish()


@requires(R, SQL)
@executions(SANDBOXED)
def test_avoids_private_preview_name_collisions(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        CREATE TABLE __mcp_console_preview_e2 AS
        SELECT CAST(999 AS INTEGER) AS column_01
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT CAST(42 AS INTEGER) AS answer
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert "42" in preview
    assert "999" not in preview

    sql = code(r"""
        SELECT column_01 AS catalog_value
        FROM __mcp_console_preview_e2
        """)
    client.send(sql=sql)
    assert "999" in last_tool_text(client)
    return client.finish()


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_preserves_utf8_preview_in_c_locale(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(
        binary,
        execution.serve(),
        environment={**os.environ, "LC_ALL": "C"},
    )
    client.initialize_and_list_tools()
    client.send(sql="SELECT 'façade 漢字' AS label")
    preview = last_tool_text(client)
    assert preview.startswith("# A tibble: 1 × 1\n"), preview
    assert '"façade 漢字"' in preview, preview

    sql = code(r"""
        SELECT
          E'line\nbreak\t"quoted"' AS controls,
          'literal \u6f22 and \path' AS backslashes,
          '' AS empty,
          CAST(NULL AS VARCHAR) AS missing
        """)
    client.send(sql=sql)
    preview = last_tool_text(client)
    assert r'"line\nbreak\t\"quoted\""' in preview, preview
    assert r'"literal \\u6f22 and \\path"' in preview, preview
    assert '""' in preview and "NULL" in preview, preview
    return client.finish()


@requires(R, SQL)
@executions(SANDBOXED)
def test_previews_schema_and_exact_values(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(options(
          width = 20L,
          max.print = 1L,
          digits = 2L,
          scipen = -9L,
          OutDec = ",",
          cli.num_colors = 256L,
          cli.unicode = FALSE,
          pillar.advice = TRUE,
          pillar.bidi = TRUE,
          pillar.bold = TRUE,
          pillar.min_title_chars = 1000L,
          pillar.width = 20L,
          pillar.print_max = 1L,
          pillar.max_extra_cols = 0L,
          pillar.superdigit_sep = "XYZ",
          pillar.subtle = TRUE
        ))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT
          CAST(NULL AS VARCHAR) AS missing,
          CAST('9223372036854775807' AS BIGINT) AS big,
          CAST(
            '12345678901234567890123456789.123456789'
            AS DECIMAL(38, 9)
          ) AS exact,
          CAST([1, NULL, 3] AS INTEGER[]) AS items,
          {'name': 'Ada', 'active': true} AS person
        """)
    client.send(sql=sql)
    values = last_tool_text(client)

    sql = code(r"""
        SELECT
          1 AS column_name_01_is_deliberately_long,
          2 AS column_name_02_is_deliberately_long,
          3 AS column_name_03_is_deliberately_long,
          4 AS column_name_04_is_deliberately_long,
          5 AS column_name_05_is_deliberately_long,
          6 AS column_name_06_is_deliberately_long,
          7 AS column_name_07_is_deliberately_long,
          8 AS column_name_08_is_deliberately_long,
          9 AS column_name_09_is_deliberately_long,
          10 AS column_name_10_is_deliberately_long,
          11 AS column_name_11_is_deliberately_long,
          12 AS column_name_12_is_deliberately_long
        """)
    client.send(sql=sql)
    long_names = last_tool_text(client)

    sql = code(r"""
        SELECT
          CAST(NULL AS INTEGER) AS id,
          CAST(NULL AS DECIMAL(10, 2)) AS amount,
          CAST(NULL AS VARCHAR[]) AS tags
        WHERE FALSE
        """)
    client.send(sql=sql)
    empty = last_tool_text(client)
    transcript = client.finish()

    assert "missing" in values
    assert "<int64>" in values
    assert "decimal128(38, 9)" in values
    assert "NULL" in values
    assert "9223372036854775807" in values
    assert "12345678901234567890123456789.123456789" in values
    assert "[1, NULL, 3]" in values
    assert "Ada" in values and "true" in values
    for column in range(1, 13):
        assert f"column_name_{column:02d}_is_deliberately_long" in long_names
    assert "abbreviated names" in long_names
    assert "0 rows" in empty
    assert "id" in empty and "amount" in empty and "tags" in empty
    assert "int32" in empty
    assert "decimal128(10, 2)" in empty
    return transcript


@requires(R, SQL)
@executions(SANDBOXED)
def test_uses_200_column_default(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        SELECT
          1 AS column_name_01_abcdefghijkl,
          2 AS column_name_02_abcdefghijkl,
          3 AS column_name_03_abcdefghijkl,
          4 AS column_name_04_abcdefghijkl,
          5 AS column_name_05_abcdefghijkl,
          6 AS column_name_06_abcdefghijkl
        """)
    client.send(sql=sql)
    output = last_tool_text(client)
    for column in range(1, 7):
        assert f"column_name_{column:02}_abcdefghijkl" in output
    assert "abbreviated name" not in output
    header = next(
        line for line in output.splitlines() if line.startswith("  column_name_01")
    )
    assert 160 < len(header) <= 200, repr(header)
    return client.finish()


@requires(R, SQL)
@executions(SANDBOXED)
def test_bounds_query_previews_without_materializing_results(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        SELECT
          repeat('a', 1000) AS c01,
          repeat('b', 1000) AS c02,
          repeat('c', 1000) AS c03,
          repeat('d', 1000) AS c04,
          repeat('e', 1000) AS c05,
          repeat('f', 1000) AS c06,
          repeat('g', 1000) AS c07,
          repeat('h', 1000) AS c08,
          repeat('i', 1000) AS c09,
          repeat('j', 1000) AS c10,
          repeat('k', 1000) AS c11,
          repeat('l', 1000) AS c12,
          repeat('m', 1000) AS c13,
          repeat('n', 1000) AS c14
        FROM range(21)
        """)
    client.send(sql=sql)
    wide = last_tool_text(client)

    sql = code(r"""
        SELECT repeat('z', 1000) AS value
        """)
    client.send(sql=sql)
    long_cell = last_tool_text(client)

    sql = code(r"""
        SELECT value
        FROM range(1000000000000) AS values(value)
        WHERE value % 97 = 0
        """)
    client.send(sql=sql, timeout_ms=1000)
    large = last_tool_text(client)
    normalize_preview_paths(client)
    transcript = client.finish()

    assert len(wide.encode("utf-8")) <= 8 * 1024
    assert "[additional rows omitted]" in wide
    assert "[2 additional columns omitted]" in wide
    assert "[cell values truncated to 160 characters]" in wide
    assert "a" * 161 not in wide
    assert f'"{"z" * 159}…"' in long_cell
    assert "[cell values truncated to 160 characters]" in long_cell
    assert without_elapsed(large) != "\n[running; poll with an empty send]"
    assert len(large.encode("utf-8")) <= 8 * 1024
    assert "[additional rows omitted]" in large
    return transcript


@requires(R, SQL)
@executions(SANDBOXED)
def test_keeps_repeated_previews_deterministic(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    sql = code(r"""
        CREATE VIEW wide_values AS SELECT
          repeat('😀漢é', 2000) AS c01,
          repeat('界🚀', 2000) AS c02,
          repeat('á', 5000) AS c03,
          repeat('🙂', 5000) AS c04,
          repeat('字', 5000) AS c05,
          repeat('é', 5000) AS c06,
          repeat('🚧', 5000) AS c07,
          repeat('語', 5000) AS c08,
          repeat('ñ', 5000) AS c09,
          repeat('🌐', 5000) AS c10,
          repeat('ß', 5000) AS c11,
          repeat('文', 5000) AS c12,
          repeat('extra', 2000) AS c13
        FROM range(21)
        """)
    client.send(sql=sql)
    assert last_tool_text(client) == "[done]"

    sql = code(r"""
        SELECT * FROM wide_values
        """)
    outputs = []
    retained = []
    for call_id in range(2, 5):
        client.send(sql=sql)
        output = last_tool_text(client)
        raw = cell_text(client, call_id)
        assert_preview(output, raw)
        retained.append(raw)
        outputs.append(output.replace(f"call-{call_id:06}.log", "call-<cell>.log"))

    assert retained[0] == retained[1] == retained[2]
    assert outputs[0] == outputs[1] == outputs[2]
    assert outputs[0].startswith("# A tibble:")
    normalize_preview_paths(client)
    return client.finish()


def normalize_duckdb_extension_error(client: McpClient) -> str:
    output = normalize_duckdb_progress(client)
    output, download_urls = re.subn(
        r'(?<= at URL )"https?://[^"]+"',
        '"<DuckDB extension URL>"',
        output,
        count=1,
    )
    output, troubleshooting_urls = re.subn(
        r"https://duckdb\.org/docs/stable/extensions/troubleshooting\?\S+",
        "<DuckDB extension troubleshooting URL>",
        output,
        count=1,
    )
    assert (download_urls, troubleshooting_urls) == (1, 1), output
    client.transcript[-1]["result"]["content"][0]["text"] = output
    return output


if __name__ == "__main__":
    run_this_suite(__file__)
