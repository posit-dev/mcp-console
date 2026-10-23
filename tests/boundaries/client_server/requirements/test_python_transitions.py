#!/usr/bin/env -S uv run --script

import json
import os
import sys
import tempfile
from contextlib import ExitStack
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.snapshots import execution_snapshots
from support.requirements import POSIX, requires
from support.assertions import (
    last_tool_text,
    release_worker_callback_gate,
    wait_for_idle_output,
)
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from boundaries.client_server.requirements.test_actions import inspect
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.resolvers import (
    checkpoint_uv_environment,
    recording_uv_environment,
    uv_tool_run_requirements,
)
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_owns_managed_python_transitions(
    binary: Path, execution: Execution
) -> Transcript:
    environment = dict(os.environ, MCP_CONSOLE_LANGUAGES="r")
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
        environment,
    ) as client:
        client.initialize_and_list_tools()
        # Instrument the old planner, then exercise only public declarations,
        # explicit preparation, and automatic imports through the console.
        # fmt: r
        r = code(r"""
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "py_reqs_plan",
              tracer = quote(stop("reticulate calculated the transition")),
              print = FALSE,
              where = namespace
            )))
            reticulate::py_require(c("numpy", "unused", "numpy"), action = "set")
            reticulate::py_require("unused", action = "remove")
            reticulate::py_require(python_version = ">=3.10, <4")
            reticulate::py_require(exclude_newer = "2026-01-01")
            reticulate::py_require(exclude_newer = NA_character_, action = "remove")
            declared <- reticulate::py_require()
            stopifnot(
              identical(declared$packages, "numpy"),
              identical(declared$python_version, c(">=3.10", "<4")),
              identical(declared["exclude_newer"], list(exclude_newer = NULL)),
              !reticulate::py_available(initialize = FALSE)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(requirements={"python": ["numpy"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(r="stopifnot(!reticulate::py_available(initialize = FALSE))")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(
            r='reticulate::py_run_string("import yaml12; print(yaml12.__name__)")'
        )
        assert last_tool_text(client) == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "yaml12\n"
        ), last_tool_text(client)
        # fmt: r
        r = code(r"""
            current <- reticulate::py_require()
            stopifnot(
              identical(current$packages, c("py-yaml12", "numpy")),
              identical(current$python_version, declared$python_version)
            )
            reticulate::py_require("py-yaml12")
            reticulate::py_require("absent", action = "remove")
            reticulate::py_require(rev(current$packages), action = "set")
            reticulate::py_require(python_version = ">=3.10")
            unchanged <- reticulate::py_require()
            stopifnot(
              identical(unchanged$packages, current$packages),
              identical(unchanged$python_version, declared$python_version),
              length(unchanged$history) == length(current$history) + 4L
            )
            for (action in c("remove", "set")) {
              message <- tryCatch(
                reticulate::py_require("numpy", action = action),
                error = conditionMessage
              )
              stopifnot(identical(
                message,
                "After Python has initialized, only `action = 'add'` is supported."
              ))
            }
            stopifnot(identical(reticulate::py_require(), unchanged))
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(requirements={"python": ["packaging"]})
        assert last_tool_text(client) == "[prepared]"
        client.send(
            r='reticulate::py_run_string("import packaging; print(packaging.__name__)")'
        )
        assert last_tool_text(client) == "packaging\n"
        return client.finish()[3:]


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_native_activation_agrees_with_reticulate_and_publishes_after_commit(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        environment, record = recording_uv_environment(root)
        serve = (
            execution.serve(
                "-c",
                "python.managed.resolution=implicit",
                "-c",
                "cache=host",
                "--writable-root",
                str(root),
            )
            if execution == SANDBOXED
            else execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            )
        )
        with McpClient(binary, serve, environment, root) as client:
            client.initialize_and_list_tools()
            client.send(
                python="import sys; identity = object(); identity_id = id(identity); initial_executable = sys.executable; from pathlib import Path; _ = Path('initial-python').write_text(sys.executable)"
            )
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            record.write_text("")
            # fmt: r
            r = code(r"""
                before <- reticulate::py_config()
                invisible(reticulate::py_require("py-yaml12"))
                after <- reticulate::py_config()
                stopifnot(
                  identical(after$libpython, before$libpython),
                  identical(
                    after$executable,
                    reticulate::py_eval("__import__('sys').executable")
                  ),
                  "py-yaml12" %in% reticulate::py_require()$packages
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            calls = [json.loads(line) for line in record.read_text().splitlines()]
            resolution = next(call for call in calls if call[:2] == ["tool", "run"])
            assert (
                resolution[resolution.index("--python") + 1]
                == (root / "initial-python").read_text()
            ), resolution
            assert not any(call[:2] == ["python", "list"] for call in calls), calls
            declared = client.send(requirements={"action": "get"})["structuredContent"]
            assert "py-yaml12" in declared["requirements"]["python"]
            client.send(
                python="import yaml12; (id(identity) == identity_id, sys.executable != initial_executable, yaml12.__name__)"
            )
            assert last_tool_text(client) == "(True, True, 'yaml12')\n"
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_preserves_preparation_restoration_and_live_noops(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_uv_environment(
            directory, fail_requirement="py-yaml12"
        )
        environment["MCP_CONSOLE_LANGUAGES"] = "r"
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            # fmt: r
            r = code(r"""
                reticulate::py_require(
                  "numpy",
                  python_version = ">=3.10, <4",
                  action = "set"
                )
                before <- reticulate::py_require()
                stopifnot(!reticulate::py_available(initialize = FALSE))
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            failed = client.send(requirements={"python": ["py-yaml12"]})
            assert failed["isError"] is True, failed
            error = failed["content"][0]["text"]
            assert "synthetic uv failure" in error, error
            # fmt: r
            r = code(r"""
                stopifnot(
                  identical(reticulate::py_require(), before),
                  !reticulate::py_available(initialize = FALSE)
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            Path(environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).unlink()
            client.send(r="invisible(reticulate::py_config())")
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            resolutions = uv_tool_run_requirements(record)
            # fmt: r
            r = code(r"""
                before <- reticulate::py_require()
                reticulate::py_require("numpy")
                reticulate::py_require("absent", action = "remove")
                reticulate::py_require(before$packages, action = "set")
                reticulate::py_require(python_version = ">=3.10")
                unchanged <- reticulate::py_require()
                stopifnot(
                  identical(unchanged$packages, before$packages),
                  identical(unchanged$python_version, c(">=3.10", "<4")),
                  length(unchanged$history) == length(before$history) + 4L
                )
                message <- tryCatch(
                  reticulate::py_require("numpy==0"),
                  error = conditionMessage
                )
                stopifnot(identical(
                  message,
                  paste(
                    "After Python has initialized, only `action = 'add'` with new packages is supported.",
                    "You tried to add `numpy==0` but requirements contain `numpy` already."
                  )
                ))
                message <- tryCatch(
                  reticulate::py_require(exclude_newer = "2026-01-01"),
                  error = conditionMessage
                )
                stopifnot(identical(
                  message,
                  "`exclude_newer` cannot be changed after Python has initialized."
                ))
                version <- reticulate::py_eval(
                  "'.'.join(str(x) for x in __import__('sys').version_info[:3])"
                )
                message <- tryCatch(
                  reticulate::py_require(python_version = "<3"),
                  error = conditionMessage
                )
                stopifnot(
                  identical(
                    message,
                    paste0(
                      "Python version requirements cannot be changed after Python has been initialized.\n",
                      "* Python version request: '<3'\n",
                      "* Python version initialized: '",
                      version,
                      "'"
                    )
                  ),
                  identical(reticulate::py_require(), unchanged)
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            assert uv_tool_run_requirements(record) == resolutions
            accepted = inspect(client)
            Path(environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).touch()
            client.expect(
                # fmt: r
                r=code("""
                    marker <- reticulate::py_eval("object()", convert = FALSE)
                    marker_id <- reticulate::py_id(marker)
                    initial_config <- reticulate::py_config()
                    failure <- tryCatch(
                      reticulate::py_require("py-yaml12"),
                      error = conditionMessage
                    )
                    stopifnot(
                      grepl("synthetic uv failure", failure, fixed = TRUE),
                      identical(reticulate::py_require(), unchanged),
                      identical(reticulate::py_config(), initial_config),
                      identical(reticulate::py_id(marker), marker_id)
                    )
                    """),
            )
            assert inspect(client) == accepted
            assert uv_tool_run_requirements(record)[len(resolutions) :] == [
                ["numpy", "py-yaml12"]
            ]
            Path(environment["MCP_CONSOLE_TEST_UV_FAILURE_MARKER"]).unlink()
            client.send(requirements={"python": ["py-yaml12"]})
            assert last_tool_text(client) == "[prepared]", last_tool_text(client)
            # fmt: r
            r = code(r"""
                stopifnot(
                  identical(reticulate::py_require()$python_version, c(">=3.10", "<4")),
                  identical(reticulate::py_require()$packages, c("py-yaml12", "numpy"))
                )
                """)
            client.send(r=r)
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_uses_inspected_candidate_without_reticulate_rediscovery(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        client.send(
            python="import sys; identity = object(); identity_id = id(identity)"
        )
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # Poison generic discovery only after initialization. Live declaration
        # and import activation must use the execution host's inspected identity.
        # fmt: r
        r = code(r"""
            before <- reticulate::py_config()
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "python_config",
              tracer = quote(stop("candidate rediscovery is forbidden")),
              print = FALSE,
              where = namespace
            )))
            reticulate::py_require("py-yaml12")
            after <- reticulate::py_config()
            stopifnot(
              identical(after$libpython, before$libpython),
              identical(after$python, reticulate::py_eval("__import__('sys').executable"))
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(
            python="import yaml12, more_itertools; assert id(identity) == identity_id; print('inspected candidate retained')"
        )
        assert last_tool_text(client) == "inspected candidate retained\n", (
            last_tool_text(client)
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_activation_python_failure_once_and_restores_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        client.send(python="import runpy; original_run_path = runpy.run_path")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(r="before <- reticulate::py_require()")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # fmt: python
        python = code("""
            def fail_activation_hook(_path):
                raise ValueError("activation hook failed")


            runpy.run_path = fail_activation_hook
            """)
        client.send(python=python)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(r='reticulate::py_require("py-yaml12")')
        output = last_tool_text(client)
        assert output.count("ValueError: activation hook failed") == 1, output
        client.send(r="stopifnot(identical(reticulate::py_require(), before))")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(python="runpy.run_path = original_run_path")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        client.send(requirements={"python": ["py-yaml12"]})
        assert last_tool_text(client) == "[restart required]", last_tool_text(client)
        client.send(control="restart")
        client.send(python="import yaml12; yaml12.__name__")
        assert last_tool_text(client) == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "'yaml12'\n"
        ), last_tool_text(client)
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_activation_interrupt_conditions(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        client.send(python="None")
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        # An R interrupt during candidate configuration must still reach the
        # caller's handler rather than becoming an ordinary preparation error.
        # fmt: r
        r = code(r"""
            before <- reticulate::py_require()
            namespace <- asNamespace("jsonlite")
            invisible(suppressMessages(base::trace(
              "fromJSON",
              tracer = quote({
                if (is.character(txt) && grepl('"selection":', txt, fixed = TRUE)) {
                  base::stop(structure(
                    list(message = "activation interrupted", call = NULL),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = namespace
            )))
            outcome <- tryCatch(
              reticulate::py_require("py-yaml12"),
              interrupt = function(condition) conditionMessage(condition),
              error = function(condition) paste("error:", conditionMessage(condition))
            )
            stopifnot(
              identical(outcome, "activation interrupted"),
              identical(reticulate::py_require(), before)
            )
            """)
        client.send(r=r)
        assert last_tool_text(client) == "[done]", last_tool_text(client)
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_incompatible_live_libpython_before_activation(
    binary: Path, execution: Execution
) -> Transcript:
    from boundaries.client_server.python.test_without_r import (
        live_python_rejects_incompatible_library_before_activation,
    )

    return live_python_rejects_incompatible_library_before_activation(
        binary, execution, with_r=True
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_idle_activation_failure_retains_worker_until_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary, ExitStack() as cleanup:
        root = Path(temporary)
        environment, resolver_started, resolver_release = checkpoint_uv_environment(
            root, "py-yaml12", reuse_resolved_python_for=("py-yaml12",)
        )
        environment.pop("RETICULATE_PYTHON", None)
        cleanup.callback(resolver_started.close)
        cleanup.callback(resolver_release.close)
        client = cleanup.enter_context(
            McpClient(
                binary,
                execution.serve(
                    "-c", "python.managed.resolution=implicit", "-c", "cache=host"
                ),
                environment,
                root,
            )
        )
        client.initialize_and_list_tools()
        client.send(requirements={"r": ["later"]})
        # fmt: python
        python = code("""
            import runpy
            import sys

            identity = object()
            identity_id = id(identity)


            def fail_activation(_path):
                raise ValueError("idle activation failure")


            runpy.run_path = fail_activation
            print(sys.executable)
            """)
        client.send(python=python)
        executable = Path(last_tool_text(client).strip())
        assert executable.is_absolute() and executable.is_file(), executable
        Path(environment["MCP_CONSOLE_TEST_UV_REUSE_PYTHON"]).write_text(
            str(executable), encoding="utf-8"
        )
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
        before = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        # fmt: r
        r = code(r"""
            callback_gate <- tempfile("python-activation-gate-")
            callback_checkpoint <- tempfile("python-activation-checkpoint-")
            callback_complete <- tempfile("python-activation-complete-")
            run_callback <- function() {
              if (!file.exists(callback_gate)) {
                later::later(run_callback, delay = 0.01)
                return(invisible(NULL))
              }
              stopifnot(file.create(callback_checkpoint))
              condition <- tryCatch(
                reticulate::py_require("py-yaml12"),
                error = conditionMessage
              )
              stopifnot(grepl(
                "ValueError: idle activation failure",
                condition,
                fixed = TRUE
              ))
              cat("idle activation rejected\n")
              complete <- fifo(callback_complete, open = "wb", blocking = TRUE)
              writeBin(charToRaw("1"), complete)
              close(complete)
            }
            later::later(run_callback, delay = 0.01)
            cat(callback_gate, callback_checkpoint, callback_complete, sep = "\n")
            """)
        client.send(r=r)
        callback_complete = FifoCheckpoint.create(
            Path(last_tool_text(client).splitlines()[2])
        )
        cleanup.callback(callback_complete.close)
        # Confirm idle callback entry while resolution is still held. Candidate
        # preparation must not be included in the callback-entry deadline.
        release_worker_callback_gate(
            client, "idle Python activation failure", ("complete",)
        )
        resolver_started.wait("idle Python activation resolver")
        resolver_release.release()
        callback_complete.wait("idle Python activation failure", timeout=5)
        assert Path(environment["MCP_CONSOLE_TEST_UV_REUSE_RECORD"]).read_text() == (
            "py-yaml12\n"
        )
        # The callback's FIFO receipt does not order its output transport.
        # Receive the idle output before admitting the follow-up evaluation.
        wait_for_idle_output(
            client,
            "idle activation rejected\n\n[idle]",
            "idle Python activation failure output",
        )
        client.send(python="assert id(identity) == identity_id; print('same object')")
        assert last_tool_text(client) == "same object\n", last_tool_text(client)
        assert (
            client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            == before
        )
        client.send(requirements={"python": ["six"]})
        assert last_tool_text(client) == "[restart required]"
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
