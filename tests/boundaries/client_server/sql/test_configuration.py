#!/usr/bin/env -S uv run --script

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, SQL, requires
from support.suites import run_this_suite

SQL_R_REQUIREMENTS = (
    "reticulate",
    "DBI",
    "duckdb",
    "nanoarrow",
    "pillar",
    "tibble",
    "tzdb",
)


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_selected_r_connection_does_not_initialize_python(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_setup import deferred_selection_client

    with deferred_selection_client(
        binary, execution.serve(), r_requirements=SQL_R_REQUIREMENTS
    ) as client:
        client.expect(
            # fmt: r
            r=code("""
                original_python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
                Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")
                retained_pid <- Sys.getpid()
                retained_state <- new.env()
                retained_state$answer <- 42L
                native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
                DBI::dbBegin(native)
                invisible(DBI::dbExecute(native, "UPDATE selected SET answer = 42"))
                .console$sql_connection(native)
                stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 42)
                invisible()
                """),
        )
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
            sql="SELECT answer FROM selected",
        )
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1      43\n",
            sql="UPDATE selected SET answer = answer + 1 RETURNING answer",
        )
        client.expect(
            # fmt: r
            r=code("""
                stopifnot(
                  Sys.getpid() == retained_pid,
                  retained_state$answer == 42L,
                  identical(.console$sql_connection(), native),
                  !reticulate::py_available(initialize = FALSE),
                  DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 43
                )
                DBI::dbRollback(native)
                stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                if (is.na(original_python)) {
                  Sys.unsetenv("RETICULATE_PYTHON")
                } else {
                  Sys.setenv(RETICULATE_PYTHON = original_python)
                }
                """),
        )
        # Starting Python later must preserve the earlier native R selection.
        client.expect(python="import sqlite3")
        client.expect(
            "# A tibble: 1 × 1\n   answer\n  <int32>\n1       1\n",
            sql="SELECT answer FROM selected",
        )
        client.expect(
            # fmt: python
            python=code("""
                user = sqlite3.connect(":memory:")
                _ = user.execute("CREATE TABLE selected AS SELECT 7 AS answer")
                _console.sql_connection(user)
                """),
        )
        client.expect("answer\n------\n7\n", sql="SELECT answer FROM selected")
        client.expect(
            # fmt: r
            r=code("""
                stopifnot(DBI::dbIsValid(native))
                borrowed <- .console$sql_connection()
                stopifnot(inherits(borrowed, "python.builtin.object"))
                stopifnot(reticulate::py_id(borrowed) == reticulate::py_id(py$user))
                """),
        )
        client.finish()
    return [
        {
            "selected_r_connection_with_missing_python": True,
            "python_remains_uninitialized": True,
            "native_identity_transaction_and_worker_state_retained": True,
            "selection_survives_later_python_initialization": True,
            "later_python_selection_takes_precedence": True,
        }
    ]


@requires(POSIX, SQL, R)
@executions(DIRECT, SANDBOXED)
def test_interrupted_selection_replay_preserves_r_connection(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_setup import deferred_selection_client

    with tempfile.TemporaryDirectory() as temporary:
        modules = Path(temporary)
        # Pause the first replay before it can publish the R selection. Disable
        # the profile hook before input so retry has no second checkpoint.
        # fmt: python
        checkpoint = code("""
            import __main__
            import sys

            def selection_checkpoint(frame, event, argument):
                if (event == "call" and frame.f_code.co_name == "use_r"
                        and frame.f_globals.get("__name__") == "_mcp_console_sql"):
                    sys.setprofile(None)
                    __main__.runtime_identity = object()
                    __main__.runtime_identity_id = id(__main__.runtime_identity)
                    input("SQL selection replay> ")

            if sys.argv[0] != "-c":
                sys.setprofile(selection_checkpoint)
            """)
        (modules / "sitecustomize.py").write_text(
            f"exec(compile({json.dumps(checkpoint)}, '<SQL selection checkpoint>', 'exec'))"
        )
        with deferred_selection_client(
            binary, execution.serve(), r_requirements=SQL_R_REQUIREMENTS
        ) as client:
            client.expect(
                r=f"Sys.setenv(RETICULATE_PYTHONPATH = {json.dumps(str(modules))})"
            )
            client.expect(
                # fmt: r
                r=code("""
                    retained_pid <- Sys.getpid()
                    retained_state <- new.env()
                    retained_state$answer <- 42L
                    native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                    invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
                    DBI::dbBegin(native)
                    invisible(DBI::dbExecute(native, "UPDATE selected SET answer = 43"))
                    .console$sql_connection(native)
                    stopifnot(!reticulate::py_available(initialize = FALSE))
                    """),
            )
            client.expect(
                '[input requested: "SQL selection replay> "]\n[waiting for stdin]',
                python="never_run = True",
            )
            client.send(control="interrupt", timeout_ms=0)
            interrupted = last_tool_text(client)
            assert "KeyboardInterrupt" in interrupted, interrupted
            client.expect(
                # fmt: python
                python=code("""
                    assert "never_run" not in globals()
                    assert id(runtime_identity) == runtime_identity_id
                    """),
            )
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1      43\n",
                sql="SELECT answer FROM selected",
            )
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      Sys.getpid() == retained_pid,
                      retained_state$answer == 42L,
                      identical(.console$sql_connection(), native),
                      DBI::dbIsValid(native),
                      DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 43
                    )
                    DBI::dbRollback(native)
                    stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                    """),
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(modules), "<startup modules>")
            )


def python_setup_checkpoint() -> str:
    # Module defaults run after SQL installation. Keep the second callback
    # observable so native SQL must complete setup and R SQL must bypass it.
    # fmt: python
    return code("""
        import __main__
        import numpy as np

        __main__.runtime_identity = object()
        __main__.runtime_identity_id = id(__main__.runtime_identity)
        original_get_printoptions = np.get_printoptions

        def resume_configuration():
            np.get_printoptions = original_get_printoptions
            assert id(__main__.runtime_identity) == __main__.runtime_identity_id
            print("Python setup resumed with same objects")
            return original_get_printoptions()

        def configuration_checkpoint():
            np.get_printoptions = resume_configuration
            input("Python SQL setup> ")
            return original_get_printoptions()

        np.get_printoptions = configuration_checkpoint
        """)


@requires(POSIX, SQL, R)
@executions(DIRECT, SANDBOXED)
def test_selected_r_bypasses_incomplete_python_setup(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_setup import deferred_selection_client

    with tempfile.TemporaryDirectory() as temporary:
        modules = Path(temporary)
        (modules / "sitecustomize.py").write_text(
            f"exec(compile({json.dumps(python_setup_checkpoint())}, '<SQL setup checkpoint>', 'exec'))"
        )
        with deferred_selection_client(
            binary, execution.serve(), r_requirements=SQL_R_REQUIREMENTS
        ) as client:
            client.expect(
                r=f"Sys.setenv(RETICULATE_PYTHONPATH = {json.dumps(str(modules))})"
            )
            client.expect(
                # fmt: r
                r=code("""
                    retained_pid <- Sys.getpid()
                    native <- DBI::dbConnect(duckdb::duckdb(), dbdir = ":memory:")
                    invisible(DBI::dbExecute(native, "CREATE TABLE selected AS SELECT 1 AS answer"))
                    DBI::dbBegin(native)
                    .console$sql_connection(native)
                    """),
            )
            client.expect(
                '[input requested: "Python SQL setup> "]\n[waiting for stdin]',
                python="never_run = True",
            )
            client.send(control="interrupt", timeout_ms=0)
            assert "KeyboardInterrupt" in last_tool_text(client), client.transcript[-1]
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1      43\n",
                sql="UPDATE selected SET answer = 43 RETURNING answer",
            )
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      Sys.getpid() == retained_pid,
                      identical(.console$sql_connection(), native)
                    )
                    DBI::dbRollback(native)
                    stopifnot(DBI::dbGetQuery(native, "SELECT answer FROM selected")[[1L]] == 1)
                    """),
            )
            client.expect(
                "Python setup resumed with same objects\n",
                python='assert "never_run" not in globals()',
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(modules), "<startup modules>")
            )


if __name__ == "__main__":
    run_this_suite(__file__)
