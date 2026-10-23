"""Lazy R access to the running Python interpreter."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.suites import run_this_suite


@requires(R)
@executions(RUNTIME)
def test_direct_py_access_attaches_on_demand(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for getter in (
        "reticulate::py",
        "py",
        'get("py", envir = as.environment("package:reticulate"))',
    ):
        environment, _ = r_test_environment()
        with McpClient(
            binary,
            execution.serve("-c", f"python={json.dumps(sys.executable)}"),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                client.expect(
                    # fmt: python
                    python=code("""
                        bridge_value = "startup value"
                        bridge_object = object()
                        bridge_identity = id(bridge_object)
                        """),
                )
                client.expect(
                    # fmt: r
                    r=code("""
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        suppressPackageStartupMessages(library(reticulate))
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        """),
                )
                client.expect(
                    '[1] "startup value"\n',
                    # fmt: r
                    r=code("""
                        main <- GETTER
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        stopifnot(
                          identical(main$bridge_value, "startup value"),
                          identical(GETTER$bridge_value, "startup value"),
                          reticulate::py_available(initialize = FALSE)
                        )
                        bridge_from_r <- 42L
                        main$bridge_value
                        """).replace("GETTER", getter),
                )
                client.expect(
                    "bridge state retained\n",
                    # fmt: python
                    python=code("""
                        assert id(bridge_object) == bridge_identity
                        assert bridge_value == "startup value"
                        assert int(r.bridge_from_r) == 42
                        print("bridge state retained")
                        """),
                )
            records.extend(client.finish()[3:])
    return records


@requires(R)
@executions(RUNTIME)
def test_loading_reticulate_does_not_start_python(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["MCP_CONSOLE_LANGUAGES"] = "r"
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.expect(
            "reticulate loaded without Python\n",
            # fmt: r
            r=code("""
                Sys.setenv(RETICULATE_PYTHON = "__console_missing_python__")
                suppressPackageStartupMessages(library(reticulate))
                stopifnot(!reticulate::py_available(initialize = FALSE))
                cat("reticulate loaded without Python\\n")
                """),
        )
        return client.finish()[3:]


@requires(R)
@executions(RUNTIME)
def test_py_reads_in_initialization_hooks_do_not_reenter(
    binary: Path, execution: Execution
) -> Transcript:
    records = []
    for clear_callback in (False, True):
        environment, _ = r_test_environment()
        with McpClient(
            binary,
            execution.serve("-c", f"python={json.dumps(sys.executable)}"),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python='hook_value = "existing Python value"')
            client.expect(
                "initialization callback ran once\n",
                # fmt: r
                r=code("""
                    callback_calls <- 0L
                    after_calls <- 0L
                    options(reticulate.python.beforeInitialized = function() {
                      callback_calls <<- callback_calls + 1L
                      if (callback_calls > 1L) {
                        stop("recursive bridge initialization")
                      }
                      if (CLEAR_CALLBACK) {
                        options(reticulate.python.beforeInitialized = NULL)
                      }
                      stopifnot(
                        is.null(reticulate::py),
                        is.null(py$hook_value),
                        !reticulate::py_available(initialize = FALSE)
                      )
                    })
                    options(reticulate.python.afterInitialized = function() {
                      after_calls <<- after_calls + 1L
                      stopifnot(identical(py$hook_value, "existing Python value"))
                    })
                    stopifnot(
                      identical(reticulate::py$hook_value, "existing Python value"),
                      callback_calls == 1L,
                      after_calls == 1L
                    )
                    options(reticulate.python.beforeInitialized = NULL)
                    options(reticulate.python.afterInitialized = NULL)
                    cat("initialization callback ran once\\n")
                    """).replace(
                    "CLEAR_CALLBACK", "TRUE" if clear_callback else "FALSE"
                ),
            )
            client.expect("'existing Python value'\n", python="hook_value")
            records.extend(client.finish()[3:])
    return records


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_py_reads_during_native_selection_do_not_attach(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_setup import deferred_selection_client

    with deferred_selection_client(binary, execution.serve()) as client:
        client.expect(
            # fmt: r
            r=code("""
                callback_calls <- 0L
                options(reticulate.python.beforeInitialized = function() {
                  callback_calls <<- callback_calls + 1L
                  stopifnot(is.null(reticulate::py), is.null(py$hook_value))
                  reticulate::use_python(
                    Sys.getenv("MCP_CONSOLE_TEST_PYTHON"),
                    required = TRUE
                  )
                })
                """),
        )
        client.expect(python='hook_value = "selected Python value"')
        client.expect(
            "native selection callback ran once\n",
            # fmt: r
            r=code("""
                stopifnot(!reticulate::py_available(initialize = FALSE))
                stopifnot(
                  identical(py$hook_value, "selected Python value"),
                  callback_calls == 1L
                )
                options(reticulate.python.beforeInitialized = NULL)
                cat("native selection callback ran once\\n")
                """),
        )
        return client.finish()[3:]


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_py_access_preserves_initialization_errors_and_retries(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    with McpClient(
        binary,
        execution.serve("-c", f"python={json.dumps(sys.executable)}"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.expect(python='hook_value = "retained Python value"')
        client.expect(
            "initialization error preserved and retried\n",
            # fmt: r
            r=code("""
                callback_calls <- 0L
                options(reticulate.python.beforeInitialized = function() {
                  callback_calls <<- callback_calls + 1L
                  stopifnot(is.null(reticulate::py))
                  if (callback_calls == 1L) stop("first initialization failed")
                })
                failure <- tryCatch(py$hook_value, error = conditionMessage)
                stopifnot(
                  identical(failure, "first initialization failed"),
                  callback_calls == 1L,
                  !reticulate::py_available(initialize = FALSE)
                )
                stopifnot(
                  identical(py$hook_value, "retained Python value"),
                  callback_calls == 2L
                )
                options(reticulate.python.beforeInitialized = NULL)
                cat("initialization error preserved and retried\\n")
                """),
        )
        return client.finish()[3:]


if __name__ == "__main__":
    run_this_suite(__file__)
