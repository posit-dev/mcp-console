"""The four R helpers share Console's Python state and requirements owner."""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_setup import deferred_selection_client
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.suites import run_this_suite


@requires(R)
@executions(RUNTIME)
def test_names_and_help_leave_python_uninitialized(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["MCP_CONSOLE_LANGUAGES"] = "r"
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.expect(
            "four lazy helpers with reticulate help\n",
            # fmt: r
            r=code(r"""
                local({
                  user_names <- ls(globalenv(), all.names = TRUE)
                  tools <- as.environment("tools:mcp-console")
                  helpers <- c("py_import", "py_eval", "py_run_string", "py_require")
                  stopifnot(
                    sum(search() == "tools:mcp-console") == 1L,
                    identical(
                      sort(ls(tools, all.names = TRUE)),
                      sort(c(
                        ".console",
                        "py",
                        helpers
                      ))
                    ),
                    !reticulate::py_available(initialize = FALSE)
                  )
                  topics <- c("import", "py_eval", "py_run_string", "py_require")
                  for (i in seq_along(helpers)) {
                    helper <- get(helpers[[i]], envir = tools, inherits = FALSE)
                    stopifnot(
                      is.function(helper),
                      identical(helper, getExportedValue("reticulate", topics[[i]])),
                      length(help(topics[[i]], package = "reticulate")) == 1L
                    )
                  }
                  stopifnot(
                    !reticulate::py_available(initialize = FALSE),
                    identical(ls(globalenv(), all.names = TRUE), user_names)
                  )
                  cat("four lazy helpers with reticulate help\n")
                })
                """),
        )
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_missing_python_preserves_diagnostic_and_r_operation(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        client.expect(r='Sys.setenv(RETICULATE_PYTHON = "/mcp-console-missing-python")')
        client.expect(
            "Error in py_discover_config(required_module, use_environment) : \n"
            "  Python specified in RETICULATE_PYTHON "
            "(/mcp-console-missing-python) does not exist\n",
            r='py_eval("1 + 1")',
        )
        client.expect(
            "[1] 42\n",
            # fmt: r
            r=code("""
                stopifnot(!reticulate::py_available(initialize = FALSE))
                6L * 7L
                """),
        )
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_r_first_helpers_share_interpreter_and_conversion(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        client.expect(
            "R helpers initialized shared Python\n",
            # fmt: r
            r=code(r"""
                stopifnot(!reticulate::py_available(initialize = FALSE))
                reticulate::use_python(Sys.getenv("MCP_CONSOLE_TEST_PYTHON"), required = TRUE)
                math <- py_import("math", as = "math_for_r", convert = TRUE, delay_load = FALSE)
                stopifnot(identical(math$sqrt(49), 7))
                result <- withVisible(py_run_string(paste(
                  "bridge_value = 40",
                  "bridge_object = object()",
                  "bridge_identity = id(bridge_object)",
                  sep = "\n"
                )))
                stopifnot(!result$visible, identical(py_eval("bridge_value + 2"), 42L))
                main <- py_import("__main__", convert = FALSE)
                stopifnot(identical(reticulate::py_to_r(main$bridge_value), 40L))
                raw <- py_eval("[1, 2]", convert = FALSE)
                stopifnot(
                  inherits(raw, "python.builtin.list"),
                  identical(reticulate::py_to_r(raw), c(1L, 2L))
                )
                local_result <- py_run_string("local_value = 7", local = TRUE, convert = FALSE)
                stopifnot(
                  identical(reticulate::py_to_r(local_result$local_value), 7L),
                  !py_eval("'local_value' in globals()")
                )
                py_run_string("bridge_value += 2")
                cat("R helpers initialized shared Python\n")
                """),
        )
        client.expect(
            "R-first state retained\n",
            # fmt: python
            python=code("""
                assert bridge_value == 42
                assert id(bridge_object) == bridge_identity
                assert "local_value" not in globals()
                assert r.py_eval("bridge_value") == 42
                print("R-first state retained")
                """),
        )
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_python_first_helpers_mask_and_restart(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    with McpClient(
        binary,
        execution.serve("-c", f"python={json.dumps(sys.executable)}"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.expect(
            # fmt: python
            python=code("""
                bridge_value = "Python first"
                bridge_object = object()
                bridge_identity = id(bridge_object)
                """),
        )
        client.expect(
            "Python-first state attached; user mask preserved\n",
            # fmt: r
            r=code(r"""
                stopifnot(!reticulate::py_available(initialize = FALSE))
                main <- py_import("__main__")
                stopifnot(identical(main$bridge_value, "Python first"))
                py_run_string("bridge_value = 'updated from R'")
                stopifnot(py_eval("id(bridge_object) == bridge_identity"))
                py_eval <- function(...) "user helper"
                stopifnot(
                  identical(py_eval("1 + 1"), "user helper"),
                  identical(
                    get("py_eval", as.environment("tools:mcp-console")),
                    reticulate::py_eval
                  )
                )
                cat("Python-first state attached; user mask preserved\n")
                """),
        )
        client.expect(
            "Python-first state retained\n",
            # fmt: python
            python=code("""
                assert bridge_value == "updated from R"
                assert id(bridge_object) == bridge_identity
                print("Python-first state retained")
                """),
        )
        client.send(control="restart")
        client.expect(
            "helpers reinstalled with fresh state\n",
            # fmt: r
            r=code(r"""
                stopifnot(
                  !exists("py_eval", globalenv(), inherits = FALSE),
                  !exists("main", globalenv(), inherits = FALSE),
                  sum(search() == "tools:mcp-console") == 1L,
                  identical(py_eval, reticulate::py_eval),
                  identical(py_import, reticulate::import),
                  identical(py_run_string, reticulate::py_run_string),
                  identical(py_require, reticulate::py_require),
                  !py_eval("'bridge_object' in globals()")
                )
                cat("helpers reinstalled with fresh state\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_requirements_get_add_set_and_live_activation(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["MCP_CONSOLE_LANGUAGES"] = "r"
    with McpClient(
        binary,
        execution.serve(
            "-c",
            "python.managed.resolution=implicit",
            "-c",
            "r.packages=[]",
            "-c",
            "python.managed.packages=[]",
        ),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        retained = client.send(requirements={"action": "get"})["structuredContent"]
        client.expect(
            "requirements declared without Python\n",
            # fmt: r
            r=code(r"""
                initial <- py_require()
                stopifnot(identical(initial, reticulate::py_require()))
                result <- withVisible(py_require(c("packaging", "six"), action = "set"))
                stopifnot(!result$visible, is.null(result$value))
                py_require("numpy")
                stopifnot(
                  identical(sort(py_require()$packages), c("numpy", "packaging", "six")),
                  identical(tail(py_require()$history, 1L)[[1L]]$requested_from, "R_GlobalEnv"),
                  !reticulate::py_available(initialize = FALSE)
                )
                cat("requirements declared without Python\n")
                """),
        )
        declared = client.send(requirements={"action": "get"})["structuredContent"]
        # MCP inspection reads accepted requirements; R declarations remain
        # pending until Python preparation accepts them.
        assert (
            declared["requirements"]["python"] == retained["requirements"]["python"]
        ), declared
        client.expect(
            "[done]",
            # fmt: r
            r=code("""
                py_require("packaging", action = "set")
                stopifnot(
                  identical(py_require()$packages, "packaging"),
                  !reticulate::py_available(initialize = FALSE)
                )
                """),
        )
        declared = client.send(requirements={"action": "get"})["structuredContent"]
        assert (
            declared["requirements"]["python"] == retained["requirements"]["python"]
        ), declared
        client.expect(
            r='py_run_string("bridge_object = object(); bridge_identity = id(bridge_object)")'
        )
        declared = client.send(requirements={"action": "get"})["structuredContent"]
        assert declared["requirements"]["python"] == ["packaging"], declared
        client.expect(
            # fmt: r
            r=code("""
                py_require("py-yaml12")
                stopifnot(
                  identical(py_import("yaml12")$`__name__`, "yaml12"),
                  py_eval("id(bridge_object) == bridge_identity"),
                  identical(sort(py_require()$packages), c("packaging", "py-yaml12"))
                )
                accepted <- py_require()
                """),
        )
        declared = client.send(requirements={"action": "get"})["structuredContent"]
        assert sorted(declared["requirements"]["python"]) == [
            "packaging",
            "py-yaml12",
        ], declared
        client.expect(
            "changed live set rejected without manifest changes\n",
            # fmt: r
            r=code(r"""
                failure <- tryCatch(py_require("six", action = "set"), error = conditionMessage)
                stopifnot(
                  identical(
                    failure,
                    "After Python has initialized, only `action = 'add'` is supported."
                  ),
                  identical(py_require(), accepted),
                  py_eval("id(bridge_object) == bridge_identity")
                )
                cat("changed live set rejected without manifest changes\n")
                """),
        )
        unchanged = client.send(requirements={"action": "get"})["structuredContent"]
        assert unchanged["requirements"] == declared["requirements"]
        client.send(control="restart")
        client.expect(
            "accepted requirements retained across restart\n",
            # fmt: r
            r=code(r"""
                stopifnot(
                  !reticulate::py_available(initialize = FALSE),
                  identical(sort(py_require()$packages), c("packaging", "py-yaml12")),
                  !exists("accepted", globalenv(), inherits = FALSE),
                  !py_eval("'bridge_object' in globals()")
                )
                cat("accepted requirements retained across restart\n")
                """),
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
