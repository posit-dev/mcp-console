"""R-backed SQL reports driver failures without repeating message formatting."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, SQL, requires
from support.suites import run_this_suite


def dbi_error_case(
    binary: Path,
    execution: Execution,
    declaration: str,
    message: str,
    expected_formatter_calls: int,
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-sql-errors-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r=declaration)
            client.expect(
                # fmt: r
                r=code("""
                    library(DBI)
                    sentinel <- new.env()
                    original <- sentinel
                    setClass(
                      "ControlErrorConnection",
                      contains = "DBIConnection",
                      slots = c(native = "ANY")
                    )
                    setMethod(
                      "dbIsValid",
                      "ControlErrorConnection",
                      function(dbObj, ...) DBI::dbIsValid(dbObj@native)
                    )
                    setMethod(
                      "dbSendQuery",
                      c("ControlErrorConnection", "character"),
                      function(conn, statement, ...) {
                        if (identical(statement, "SELECT fail")) {
                          fail_query()
                        }
                        DBI::dbSendQuery(conn@native, statement, ...)
                      }
                    )
                    selected <- new("ControlErrorConnection", native = .console$sql_connection())
                    .console$sql_connection(selected)
                    """),
            )
            with ControlClient(endpoint) as control:
                failed = control.request(
                    op="evaluate",
                    language="sql",
                    code="SELECT fail",
                    busy="wait",
                    wait_ms=30000,
                )
                assert failed == {
                    "id": "1",
                    "result": {
                        "resultType": "complete",
                        "content": [{"type": "text", "text": f"Error: {message}\n"}],
                        "isError": False,
                        "execution_error": {
                            "ename": "SQLError",
                            "evalue": message,
                            "traceback": [],
                        },
                    },
                }, failed
                # The failed SQL reply belongs to control. Its driver state and
                # the selected connection remain in the same worker for MCP.
                client.expect("\n[idle]")
                client.expect(
                    f"{expected_formatter_calls} TRUE TRUE\n",
                    # fmt: r
                    r=code("""
                        cat(
                          paste(
                            formatter_calls,
                            identical(sentinel, original),
                            identical(.console$sql_connection(), selected)
                          ),
                          "\\n",
                          sep = ""
                        )
                        """),
                )
                succeeded = control.request(
                    op="evaluate",
                    language="sql",
                    code="SELECT 42 AS answer",
                    busy="wait",
                    wait_ms=30000,
                )
                assert succeeded == {
                    "id": "2",
                    "result": {
                        "resultType": "complete",
                        "content": [
                            {
                                "type": "text",
                                "text": "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
                            }
                        ],
                        "isError": False,
                        "execution_error": None,
                    },
                }, succeeded
                client.expect("\n[idle]")
                return TranscriptWithCompanions(
                    client.finish(), {"control.yaml": control.transcript}
                )


@requires(POSIX, R, SQL)
@executions(RUNTIME)
def test_formats_dbi_error_message_once(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    # fmt: r
    declaration = code("""
        formatter_calls <- 0L
        registerS3method(
          "conditionMessage",
          "SingleUseSqlError",
          function(condition) {
            formatter_calls <<- formatter_calls + 1L
            # stop(condition) formats once before signaling. The SQL bridge
            # may format the delivered condition once for its diagnostic.
            if (formatter_calls > 2L) {
              stop("condition message formatted again")
            }
            "driver failure"
          },
          envir = baseenv()
        )
        fail_query <- function() {
          stop(structure(
            list(message = "unused", call = NULL),
            class = c("SingleUseSqlError", "error", "condition")
          ))
        }
        """)
    return dbi_error_case(binary, execution, declaration, "driver failure", 2)


@requires(POSIX, R, SQL)
@executions(RUNTIME)
def test_preserves_empty_dbi_error_message(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    # fmt: r
    declaration = code("""
        formatter_calls <- 0L
        fail_query <- function() stop("")
        """)
    return dbi_error_case(binary, execution, declaration, "", 0)


if __name__ == "__main__":
    run_this_suite(__file__)
