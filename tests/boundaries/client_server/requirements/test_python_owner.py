#!/usr/bin/env -S uv run --script

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import R, SQL, requires
from support.assertions import last_result_text
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.suites import run_this_suite


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_prepares_without_reticulate_environment_mutators(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary, execution.serve("-c", "python.managed.resolution=implicit")
    ) as client:
        client.initialize_and_list_tools()
        client.send(r="owner_r_state <- 42L")
        client.send(sql="CREATE TABLE owner_state AS SELECT 42 AS answer")
        client.send(
            # fmt: python
            python=code("""
                import os

                owner_object = object()
                owner_identity = id(owner_object)
                owner_pid = os.getpid()
                """)
        )
        client.send(
            # fmt: r
            r=code("""
                namespace <- asNamespace("reticulate")
                mutators <- c(
                  "py_require",
                  "py_reqs_activate",
                  "py_activate_virtualenv",
                  "uv_get_or_create_env"
                )
                originals <- mget(mutators, envir = namespace)
                for (name in mutators) {
                  unlockBinding(name, namespace)
                  assign(
                    name,
                    function(...) stop("reticulate environment mutator reached"),
                    namespace
                  )
                  lockBinding(name, namespace)
                }
                """)
        )
        client.send(
            requirements={"python": ["py-yaml12"]},
            stdin="queued input\n",
            # fmt: python
            python=code("""
                import yaml12

                assert os.getpid() == owner_pid and id(owner_object) == owner_identity
                input()
                """),
        )
        assert last_result_text(client) == "[input requested: \"\"]\n'queued input'\n"
        client.send(python="import humanize; humanize.intcomma(1234)")
        assert last_result_text(client) == "'1,234'\n", last_result_text(client)
        client.send(
            # fmt: r
            r=code("""
                for (name in mutators) {
                  unlockBinding(name, namespace)
                  assign(name, originals[[name]], namespace)
                  lockBinding(name, namespace)
                }
                stopifnot(
                  all(c("py-yaml12", "humanize") %in% reticulate::py_require()$packages),
                  is.null(reticulate::py_require()$python_version),
                  identical(
                    reticulate::py_config()$python,
                    reticulate::import("sys")$executable
                  ),
                  identical(owner_r_state, 42L)
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(sql="SELECT answer FROM owner_state")
        assert last_result_text(client).splitlines()[-1].split() == ["1", "42"]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_initializes_with_lazy_exclusion_date(
    binary: Path, execution: Execution
) -> Transcript:
    environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="r")
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                reticulate::py_require(exclude_newer = "2026-09-01")
                stopifnot(!reticulate::py_available(initialize = FALSE))
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        client.send(r='reticulate::py_eval("1 + 1")')
        assert last_result_text(client) == "[1] 2\n", last_result_text(client)
        client.send(control="restart")
        client.send(
            # fmt: r
            r=code("""
                stopifnot(
                  identical(reticulate::py_require()$exclude_newer, "2026-09-01"),
                  !reticulate::py_available(initialize = FALSE)
                )
                invisible(reticulate::py_config())
                stopifnot(
                  reticulate::py_available(initialize = FALSE),
                  identical(reticulate::py_require()$exclude_newer, "2026-09-01")
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_preserves_live_reticulate_requirement_rules(
    binary: Path, execution: Execution
) -> list:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                config_reader <- reticulate::py_config
                initial <- config_reader()
                current <- reticulate::py_require()
                replacement <- tryCatch(
                  reticulate::py_require(c(current$packages, "py-yaml12"), action = "set"),
                  error = conditionMessage
                )
                stopifnot(
                  identical(
                    replacement,
                    "After Python has initialized, only `action = 'add'` is supported."
                  ),
                  identical(reticulate::py_require(), current)
                )
                reticulate::py_require(python_version = ">=3.10")
                stopifnot(identical(
                  reticulate::py_require()$python_version,
                  current$python_version
                ))
                reticulate::py_require("py-yaml12")
                config <- reticulate::py_config()
                sys <- reticulate::import("sys")
                stopifnot(
                  "py-yaml12" %in% reticulate::py_require()$packages,
                  identical(config_reader, reticulate::py_config),
                  identical(config_reader(), config),
                  identical(reticulate::py_exe(), sys$executable),
                  identical(config$python, sys$executable),
                  identical(config$executable, sys$executable),
                  identical(config$prefix, sys$prefix),
                  identical(config$exec_prefix, sys$exec_prefix),
                  identical(config$virtualenv, sys$prefix),
                  identical(
                    config$pythonpath,
                    paste(
                      ifelse(nzchar(sys$path), sys$path, "."),
                      collapse = .Platform$path.sep
                    )
                  ),
                  !grepl(initial$prefix, config$pythonpath, fixed = TRUE),
                  !nzchar(config$virtualenv_activate) ||
                    identical(dirname(config$virtualenv_activate), dirname(sys$executable)),
                  identical(
                    config$pythonhome,
                    if (identical(sys$base_prefix, sys$base_exec_prefix)) {
                      sys$base_prefix
                    } else {
                      paste(sys$base_prefix, sys$base_exec_prefix, sep = ":")
                    }
                  )
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_refreshes_numpy_configuration_after_live_preparation(
    binary: Path, execution: Execution
) -> Transcript:
    environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="r")
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                reticulate::py_require(character(), action = "set")
                stopifnot(is.null(reticulate::py_config()$numpy))
                reticulate::py_require("numpy")
                numpy <- reticulate::import("numpy", convert = FALSE)
                config <- reticulate::py_config()
                stopifnot(
                  !is.null(config$numpy),
                  identical(
                    config$numpy$version,
                    numeric_version(reticulate::py_to_r(numpy$`__version__`))
                  ),
                  identical(
                    normalizePath(config$numpy$path),
                    normalizePath(reticulate::py_to_r(numpy$`__path__`)[[1L]])
                  )
                )
                reticulate::py_require("py-yaml12")
                active <- reticulate::py_config()
                stopifnot(
                  !identical(active$numpy$path, config$numpy$path),
                  startsWith(active$numpy$path, active$prefix),
                  identical(active$numpy$version, config$numpy$version)
                )
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()[3:]


@executions(RUNTIME)
def test_matches_live_python_version_constraints_like_the_resolver(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            # fmt: r
            r=code("""
                invisible(reticulate::py_config())
                version <- as.character(reticulate::py_version(patch = TRUE))
                minor <- as.character(reticulate::py_version())
                current <- reticulate::py_require()
                for (constraint in c(
                  paste0(version, "rc1"),
                  paste0("1!", version),
                  paste0(version, "+console"),
                  paste0(version, ".post1"),
                  paste0(version, ".dev1"),
                  paste0(">", minor)
                )) {
                  error <- tryCatch(
                    reticulate::py_require(
                      "humanize; python_version < '0'",
                      python_version = constraint
                    ),
                    error = conditionMessage
                  )
                  stopifnot(
                    is.character(error),
                    grepl("Python version requirements cannot be changed", error, fixed = TRUE),
                    identical(reticulate::py_require(), current)
                  )
                }
                for (constraint in c(
                  version,
                  minor,
                  paste0("==", minor),
                  paste0("<=", minor)
                )) {
                  reticulate::py_require(python_version = constraint)
                }
                stopifnot(identical(
                  reticulate::py_require()$python_version,
                  current$python_version
                ))
                """)
        )
        assert last_result_text(client) == "[done]", last_result_text(client)
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
