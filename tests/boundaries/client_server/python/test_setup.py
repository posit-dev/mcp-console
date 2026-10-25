#!/usr/bin/env -S uv run --script

import json
import os
import subprocess
import shutil
import sys
import tempfile
from pathlib import Path
from contextlib import contextmanager

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import NATIVE_FIXTURES, POSIX, R, requires
from support.assertions import (
    assert_exact_interleaving,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint
from support.processes import host_process_id, process_exists, stop_process_id
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_resolution_error
from support.native import build_interposer
from support.python import runtime_source_line, virtualenv_python
from support.records import Transcript
from support.snapshots import execution_snapshots
from support.resolvers import (
    bare_runtime_environment,
    send_and_collect_runtime_python_resolution,
)
from support.r import install_r_startup, r_test_environment
from support.suites import run_this_suite


@contextmanager
def startup_client(
    binary: Path,
    execution: Execution,
    source: str,
    *arguments: str,
    selected_python: str | None = None,
):
    with tempfile.TemporaryDirectory() as temporary:
        modules = Path(temporary)
        (modules / "sitecustomize.py").write_text(
            "import __main__\n"
            f"exec(compile({json.dumps(source)}, '<setup checkpoint>', 'exec'), __main__.__dict__)\n"
        )
        environment = dict(os.environ, RETICULATE_PYTHONPATH=str(modules))
        if selected_python is not None:
            environment["RETICULATE_PYTHON"] = selected_python
        with McpClient(binary, execution.serve(*arguments), environment) as client:
            client.initialize_and_list_tools()
            yield client


@contextmanager
def deferred_selection_client(
    binary: Path, serve: tuple[str, ...], *, r_requirements: tuple[str, ...] = ()
):
    """Arrange the public retryable, uninitialized state for selection tests."""
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, rscript = r_test_environment()
        if r_requirements:
            # Resolve fixture dependencies before deliberately disabling resolution.
            libraries = subprocess.check_output(
                [
                    "ir",
                    "run",
                    "--rscript",
                    str(rscript),
                    *(
                        argument
                        for package in r_requirements
                        for argument in ("--with", package)
                    ),
                    "--isolated",
                    "--vanilla",
                    "-e",
                    "writeLines(.libPaths())",
                ],
                env=environment,
                stderr=subprocess.PIPE,
                text=True,
            )
            environment["R_LIBS"] = os.pathsep.join(libraries.splitlines())
        library = install_r_startup(
            directory,
            environment,
            # fmt: r
            code(f"""
                if (!file.exists({json.dumps(str(directory / "interrupted"))})) {{
                  options(reticulate.python.beforeInitialized = function() {{
                    options(reticulate.python.beforeInitialized = NULL)
                    readline("defer selection> ")
                    stop(structure(
                      list(message = "fixture bootstrap interrupt", call = NULL),
                      class = c("interrupt", "condition")
                    ))
                  }})
                }}
                """),
        )
        environment = bare_runtime_environment(environment, library)
        if os.name == "nt":
            # Windows eagerly inspects a PATH Python in bare R sessions. This
            # fixture specifically arranges unresolved R-side selection.
            environment["PATH"] = os.pathsep.join(
                entry
                for entry in environment["PATH"].split(os.pathsep)
                if not (Path(entry) / "python.exe").exists()
            )
        with McpClient(binary, serve, environment, directory) as client:
            client.initialize_and_list_tools()
            initialized = client.transcript.copy()
            wait_for_evaluation_output(
                client,
                '[input requested: "defer selection> "]\n[waiting for stdin]',
                "deferred selection fixture",
                completion_timeout_seconds=client.response_timeout,
                python="raise AssertionError('interrupted bootstrap ran setup cell')",
                timeout_ms=0,
            )
            wait_for_evaluation_output(
                client,
                "Error: fixture bootstrap interrupt\n",
                "deferred selection bootstrap interruption",
                completion_timeout_seconds=client.response_timeout,
                stdin="\n",
            )
            assert client.transcript[-1]["result"].get("isError") is not True
            assert "AssertionError" not in last_result_text(client)
            client.send(r="stopifnot(!reticulate::py_available(initialize = FALSE))")
            assert last_result_text(client) == "[done]", client.transcript[-1]
            # Mark from the controller so the sentinel survives private sandbox storage.
            (directory / "interrupted").touch()
            # Bootstrap arrangement is shared fixture setup, not this case's transcript.
            client.transcript[:] = initialized
            yield client


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_preserves_queued_inspection_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        probe = build_interposer(
            Path(temporary_directory), "queued_inspection_interrupt"
        )
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with deferred_selection_client(binary, serve) as client:
            client.send(
                # fmt: r
                r=code(f"""
                    dyn.load({json.dumps(str(probe))})
                    retained_pid <- Sys.getpid()
                    retained_value <- 41L
                    options(reticulate.python.beforeInitialized = function() {{
                      options(reticulate.python.beforeInitialized = NULL)
                      invisible(.C("queue_inspection_interrupt"))
                    }})
                    """)
            )
            # Keep R from consuming the queued SIGINT before inspection enters
            # its native callback. The inspection owner must still cancel it.
            client.send(
                # fmt: r
                r=code("""
                    suspendInterrupts(invisible(reticulate::py_config()))
                    """)
            )
            client.send(
                # fmt: r
                r=code("""
                    stopifnot(Sys.getpid() == retained_pid)
                    stopifnot(!reticulate::py_available(initialize = FALSE))
                    retained_value + 1L
                    """)
            )
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.send(
                # fmt: python
                python=code("""
                    retried_value = 43
                    retried_value
                    """)
            )
            assert last_result_text(client) == "43\n", client.transcript[-1]
            records = client.finish()
            for record in records:
                if "send" in record and "r" in record["send"]:
                    record["send"]["r"] = record["send"]["r"].replace(
                        str(probe), "<interrupt fixture>"
                    )
            return records


@execution_snapshots
@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_cancels_native_inspection_and_retries(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        subprocess.run(
            [sys.executable, "-m", "venv", "--without-pip", temporary / "venv"],
            check=True,
            capture_output=True,
        )
        selected = virtualenv_python(temporary / "venv")
        subprocess.run(
            ["uv", "pip", "install", "--python", selected, "numpy", "pandas"],
            check=True,
            capture_output=True,
        )
        fixture = Path(__file__).resolve().parents[3] / "fixtures"
        site = Path(
            subprocess.check_output(
                [selected, fixture / "native_python_paths.py", "site-packages"],
                text=True,
            ).strip()
        )
        shutil.copyfile(
            fixture / "native_python_sitecustomize.py", site / "sitecustomize.py"
        )
        (site / "inspection-mode").write_text("inspection-checkpoint")
        ready = FifoCheckpoint.create(site / "inspection-ready")
        release = FifoCheckpoint.create(site / "inspection-release")
        pid = None
        try:
            serve = (
                execution.serve("--writable-root", temporary_directory)
                if execution == SANDBOXED
                else execution.serve()
            )
            with deferred_selection_client(binary, serve) as client:
                client.expect(
                    # fmt: r
                    r=code(f"""
                        retained_value <- 41L
                        retained_pid <- Sys.getpid()
                        Sys.setenv(RETICULATE_PYTHON = {
                          json.dumps(str(selected))
                        })
                        """)
                )
                operation = client.start_send(
                    # fmt: python
                    python=code("""
                        unexecuted_value = 1
                        """),
                    timeout_ms=600_000,
                )
                # Inspection follows cold Python setup; contention must not
                # turn its checkpoint into a ten-second startup deadline.
                ready.wait("native Python inspection", timeout=600)
                pid = host_process_id(
                    int((site / "inspection-pid").read_text()), client.process.pid
                )
                result_file = Path((site / "inspection-result").read_text())
                assert result_file.exists()
                interrupt = client.start_send(control="interrupt", timeout_ms=0)
                client.receive_many([operation, interrupt])
                assert not process_exists(pid), "inspection child was not reaped"
                pid = None
                assert not result_file.exists(), "inspection output was not removed"
                (site / "inspection-mode").write_text("inspection-output")
                client.send(
                    # fmt: r
                    r=code("""
                        stopifnot(Sys.getpid() == retained_pid)
                        stopifnot(!reticulate::py_available(initialize = FALSE))
                        retained_value + 1L
                        """)
                )
                assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        retried_value = 43
                        retried_value
                        """)
                )
                assert last_result_text(client) == "43\n", client.transcript[-1]
                assert (site / "inspection-count").read_text() == "1\n1\n"
                client.send(
                    # fmt: python
                    python=code("""
                        retried_value + 1
                        """)
                )
                assert last_result_text(client) == "44\n", client.transcript[-1]
                assert (site / "inspection-count").read_text() == "1\n1\n"
                records = client.finish()
                for record in records:
                    if "send" in record and "r" in record["send"]:
                        record["send"]["r"] = record["send"]["r"].replace(
                            json.dumps(str(selected))[1:-1], "<selected python>"
                        )
                return records
        finally:
            stop_process_id(pid)
            ready.close()
            release.close()


@executions(DIRECT, SANDBOXED)
def test_retries_failed_native_inspection(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        client.send(
            # fmt: r
            r=code("""
                retained_pid <- Sys.getpid()
                retained_value <- 41L
                invisible(asNamespace("reticulate"))
                return_missing <- TRUE
                assignInNamespace(
                  "py_discover_config",
                  local({
                    original <- get("py_discover_config", asNamespace("reticulate"))
                    function(...) {
                      config <- original(...)
                      if (return_missing) {
                        config$python <- "/missing-selected-python"
                      }
                      config
                    }
                  }),
                  "reticulate"
                )
                startup_environment <- Sys.getenv(
                  c(
                    "VIRTUAL_ENV",
                    "R_SESSION_INITIALIZED",
                    "PYTHONIOENCODING",
                    "PATH",
                    "LD_LIBRARY_PATH",
                    "PYTHONPATH"
                  ),
                  unset = NA_character_
                )
                """)
        )
        client.send(
            # fmt: python
            python=code("""
                unexecuted_value = 1
                """)
        )
        assert last_result_text(client) == (
            "Error: selected Python executable is not an absolute file: /missing-selected-python\n"
        ), client.transcript[-1]
        client.send(
            # fmt: r
            r=code("""
                stopifnot(Sys.getpid() == retained_pid)
                stopifnot(!reticulate::py_available(initialize = FALSE))
                stopifnot(identical(
                  Sys.getenv(names(startup_environment), unset = NA_character_),
                  startup_environment
                ))
                failure <- tryCatch(reticulate::py_config(), error = identity)
                stopifnot(inherits(failure, "console_python_inspection_error"))
                stopifnot(!reticulate::py_available(initialize = FALSE))
                return_missing <- FALSE
                retained_value + 1L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                retried_value = 43
                import sys

                assert sys.orig_argv == [sys.executable], (sys.orig_argv, sys.executable)
                retried_value
                """)
        )
        assert last_result_text(client) == "43\n", client.transcript[-1]
        return client.finish()


@execution_snapshots
@executions(DIRECT, SANDBOXED)
def test_console_configures_selected_python(
    binary: Path, execution: Execution
) -> Transcript:
    transcript = []
    for first in ("python", "r"):
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary = Path(temporary_directory)
            subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "venv",
                    "--without-pip",
                    "--copies",
                    temporary / "venv",
                ],
                check=True,
                capture_output=True,
            )
            selected = virtualenv_python(temporary / "venv")
            subprocess.run(
                ["uv", "pip", "install", "--python", selected, "numpy", "pandas"],
                check=True,
                capture_output=True,
            )
            serve = (
                execution.serve("--writable-root", temporary_directory)
                if execution == SANDBOXED
                else execution.serve()
            )
            with deferred_selection_client(binary, serve) as client:
                client.send(
                    # fmt: r
                    r=code(f"""
                        selected_python <- {json.dumps(str(selected))}
                        Sys.unsetenv("RETICULATE_PYTHON")
                        reticulate::use_python(selected_python, required = TRUE)
                        selection_calls <- 0L
                        assignInNamespace("py_discover_config", local({{
                          original <- get("py_discover_config", asNamespace("reticulate"))
                          function(...) {{
                            selection_calls <<- selection_calls + 1L
                            config <- original(...)
                            stopifnot(identical(normalizePath(config$python), normalizePath(selected_python)))
                            selected_python <<- config$python
                            config$libpython <- "/missing-reticulate-libpython"
                            config$pythonhome <- "/missing-reticulate-home"
                            config
                          }}
                        }}), "reticulate")
                        """)
                )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                if first == "python":
                    client.send(
                        # fmt: python
                        python=code("""
                            owned_value = 41
                            """)
                    )
                else:
                    client.send(
                        # fmt: r
                        r=code("""
                            reticulate::py_run_string("owned_value = 41")
                            """)
                    )
                assert last_result_text(client) == "[done]", client.transcript[-1]
                client.send(
                    # fmt: r
                    r=code("""
                        config <- reticulate::py_config()
                        stopifnot(selection_calls == 1L)
                        stopifnot(identical(config$python, selected_python))
                        stopifnot(file.exists(config$libpython))
                        stopifnot(dir.exists(config$pythonhome))
                        stopifnot(!identical(config$pythonhome, dirname(dirname(selected_python))))
                        reticulate::py_to_r(reticulate::py$owned_value) + 1L
                        """)
                )
                assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
                client.send(
                    # fmt: python
                    python=code("""
                        owned_value + 2
                        """)
                )
                assert last_result_text(client) == "43\n", client.transcript[-1]
                records = client.finish()
                for record in records:
                    if "send" in record and "r" in record["send"]:
                        record["send"]["r"] = record["send"]["r"].replace(
                            json.dumps(str(selected))[1:-1], "<selected python>"
                        )
                transcript.extend(records)
    return transcript


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_python_first_initializes_before_reticulate_attaches(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with deferred_selection_client(binary, serve) as client:
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                startup_calls <- 0L
                callback_calls <- 0L
                Sys.unsetenv("RETICULATE_PYTHON")
                expected_python <- normalizePath(if (.Platform$OS.type == "windows") Sys.getenv("MCP_CONSOLE_TEST_PYTHON") else Sys.which("python3"))
                options(reticulate.python.beforeInitialized = function() {{
                  callback_calls <<- callback_calls + 1L
                  initialized <- .C(
                    getNativeSymbolInfo(
                      "mcp_console_probe_python_initialized",
                      PACKAGE = .GlobalEnv$startup_probe
                    ),
                    value = 0L
                  )$value
                  if (initialized == 1L) {{
                    stop(sprintf("callback %d ran after CPython initialization", callback_calls))
                  }}
                  reticulate::use_python(expected_python, required = TRUE)
                }})
                invisible(suppressMessages(base::trace(
                  "py_initialize",
                  tracer = quote({{
                    assign(
                      "startup_calls",
                      get("startup_calls", envir = .GlobalEnv) + 1L,
                      envir = .GlobalEnv
                    )
                    stopifnot(identical(
                      .C(
                        getNativeSymbolInfo(
                          "mcp_console_probe_python_initialized",
                          PACKAGE = .GlobalEnv$startup_probe
                        ),
                        value = 0L
                      )$value,
                      1L
                    ))
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                """)
            client.send(r=r)
            assert last_result_text(client) == "[done]", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(
                # fmt: python
                python=code("""
                    startup_value = 41
                    startup_value + 1
                    """)
            )
            assert last_result_text(client) == "42\n", client.transcript[-1]
            client.send(
                # fmt: r
                r=code("""
                    stopifnot(startup_calls == 0L)
                    stopifnot(callback_calls == 1L)
                    stopifnot(identical(
                      normalizePath(reticulate::py_config()$python),
                      expected_python
                    ))
                    stopifnot(startup_calls == 1L)
                    reticulate::py_to_r(reticulate::py$startup_value) + 1L
                    """)
            )
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            return client.finish()


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_r_first_initializes_before_reticulate_attaches(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with deferred_selection_client(binary, serve) as client:
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                startup_calls <- 0L
                invisible(suppressMessages(base::trace(
                  "py_initialize",
                  tracer = quote({{
                    assign(
                      "startup_calls",
                      get("startup_calls", envir = .GlobalEnv) + 1L,
                      envir = .GlobalEnv
                    )
                    stopifnot(identical(
                      .C(
                        getNativeSymbolInfo(
                          "mcp_console_probe_python_initialized",
                          PACKAGE = .GlobalEnv$startup_probe
                        ),
                        value = 0L
                      )$value,
                      1L
                    ))
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                invisible(reticulate::py_config())
                reticulate::py_run_string("startup_value = 41")
                stopifnot(startup_calls == 1L)
                reticulate::py_to_r(reticulate::py$startup_value) + 1L
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(python="startup_value + 1")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_r_first_runs_selection_callback_once(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code("""
            Sys.unsetenv("RETICULATE_PYTHON")
            expected_python <- normalizePath(
              if (.Platform$OS.type == "windows") {
                Sys.getenv("MCP_CONSOLE_TEST_PYTHON")
              } else {
                Sys.which("python3")
              }
            )
            callback_calls <- 0L
            options(reticulate.python.beforeInitialized = function() {
              callback_calls <<- callback_calls + 1L
              reticulate::use_python(expected_python, required = TRUE)
            })
            config <- reticulate::py_config()
            stopifnot(callback_calls == 1L)
            stopifnot(identical(normalizePath(config$python), expected_python))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(NATIVE_FIXTURES)
def test_retries_attachment_without_reinitializing_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        probe = build_interposer(temporary, "python_initialized")
        serve = (
            execution.serve("--writable-root", temporary_directory)
            if execution == SANDBOXED
            else execution.serve()
        )
        with deferred_selection_client(binary, serve) as client:
            # fmt: r
            r = code(f"""
                startup_probe <- dyn.load({json.dumps(str(probe))})
                invisible(suppressMessages(base::trace(
                  "py_initialize",
                  exit = quote({{
                      startup_environment <<- Sys.getenv(c("VIRTUAL_ENV", "PATH", "R_SESSION_INITIALIZED"))
                      stop(structure(
                        list(message = "synthetic reticulate attach failure", call = NULL),
                        class = c(attachment_failure, "condition")
                      ))
                  }}),
                  print = FALSE,
                  where = asNamespace("reticulate")
                )))
                for (attachment_failure in c("error", "interrupt")) {{
                  failure <- tryCatch(
                    reticulate::py_config(),
                    error = conditionMessage,
                    interrupt = conditionMessage
                  )
                  stopifnot(!reticulate::py_available(initialize = FALSE))
                  stopifnot(grepl("synthetic reticulate attach failure", failure, fixed = TRUE))
                  stopifnot(identical(
                    Sys.getenv(names(startup_environment)), startup_environment
                  ))
                  stopifnot(identical(
                    .C(
                      getNativeSymbolInfo(
                        "mcp_console_probe_python_initialized",
                        PACKAGE = startup_probe
                      ),
                      value = 0L
                    )$value,
                    1L
                  ))
                }}
                invisible(suppressMessages(base::untrace(
                  "py_initialize", where = asNamespace("reticulate")
                )))
                config <- reticulate::py_config()
                sys <- reticulate::import("sys", convert = FALSE)
                stopifnot(identical(config$executable, reticulate::py_to_r(sys$executable)))
                reticulate::py_run_string("startup_value = 41")
                reticulate::py_to_r(reticulate::py$startup_value) + 1L
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
            client.transcript[-1]["send"]["r"] = r.replace(
                str(probe), "<test python probe>"
            )
            client.send(python="startup_value + 1")
            assert last_result_text(client) == "42\n", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_partial_attachment_requires_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        client.send(
            python="attachment_object = object(); attachment_identity = id(attachment_object)"
        )
        assert last_result_text(client) == "[done]"
        client.send(
            r=code("""
            options(reticulate.python.afterInitialized = function() stop("partial attachment"))
            first <- tryCatch(reticulate::py_config(), error = conditionMessage)
            stopifnot(identical(first, "partial attachment"))
            options(reticulate.python.afterInitialized = NULL)
            second <- tryCatch(reticulate::py_config(), error = conditionMessage)
            stopifnot(identical(second, "R/Python attachment is incomplete; restart required"))
            cat("partial attachment requires restart\\n")
            """)
        )
        assert last_result_text(client) == "partial attachment requires restart\n", (
            client.transcript[-1]
        )
        client.send(python="assert id(attachment_object) == attachment_identity; 42")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        client.send(control="restart")
        client.send(
            python="assert 'attachment_object' not in globals(); int(r['40L + 2L'])"
        )
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_selection_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code(r"""
            r_marker <- 41L
            invisible(suppressMessages(base::trace(
              "py_discover_config",
              tracer = quote({
                if (
                  !exists(
                    "selection_interrupted",
                    envir = .GlobalEnv,
                    inherits = FALSE
                  )
                ) {
                  assign("selection_interrupted", TRUE, envir = .GlobalEnv)
                  stop(base::structure(
                    base::list(message = "synthetic selection interrupt", call = NULL),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted selection ran the cell")
                """)
        )
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        client.send(python="r.r_marker + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restores_selection_environment_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code(r"""
            Sys.setenv(PYTHONPATH = "selection-original")
            original_environment <- Sys.getenv(
              c(
                "VIRTUAL_ENV",
                "R_SESSION_INITIALIZED",
                "PYTHONIOENCODING",
                "PATH",
                "LD_LIBRARY_PATH",
                "PYTHONPATH"
              ),
              unset = NA_character_
            )
            selection_env_interrupted <- FALSE
            invisible(suppressMessages(base::trace(
              "py_discover_config",
              exit = quote({
                if (!selection_env_interrupted) {
                  selection_env_interrupted <<- TRUE
                  stop(base::structure(
                    base::list(
                      message = "synthetic selection environment interrupt",
                      call = NULL
                    ),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted selection ran the cell")
                """)
        )
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        # fmt: r
        r = code("""
            stopifnot(selection_env_interrupted)
            stopifnot(identical(
              Sys.getenv(names(original_environment), unset = NA_character_),
              original_environment
            ))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        client.send(
            # fmt: r
            r=code("""
                stopifnot(identical(
                  Sys.getenv("PYTHONPATH"),
                  original_environment[["PYTHONPATH"]]
                ))
                42L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_restores_virtualenv_after_selection_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code("""
            reticulate::use_python(
              normalizePath(
                if (.Platform$OS.type == "windows") {
                  Sys.getenv("MCP_CONSOLE_TEST_PYTHON")
                } else {
                  Sys.which("python3")
                }
              ),
              required = TRUE
            )
            interrupted <- TRUE
            invisible(suppressMessages(base::trace(
              "py_discover_config",
              exit = quote({
                if (!interrupted) {
                  interrupted <<- TRUE
                  stop(structure(
                    list(message = "selection interrupted", call = NULL),
                    class = c("interrupt", "condition")
                  ))
                }
              }),
              print = FALSE,
              where = asNamespace("reticulate")
            )))
            for (previous in c(NA_character_, "before-selection")) {
              if (is.na(previous)) {
                Sys.unsetenv("VIRTUAL_ENV")
              } else {
                Sys.setenv(VIRTUAL_ENV = previous)
              }
              interrupted <- FALSE
              failure <- tryCatch(reticulate::py_config(), interrupt = conditionMessage)
              stopifnot(
                identical(failure, "selection interrupted"),
                identical(Sys.getenv("VIRTUAL_ENV", unset = NA_character_), previous),
                !reticulate::py_available(initialize = FALSE)
              )
            }
            invisible(suppressMessages(base::untrace(
              "py_discover_config",
              where = asNamespace("reticulate")
            )))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(python="41 + 1")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_recovers_from_conflicting_requirements_after_python_startup(
    binary: Path, execution: Execution
) -> Transcript:
    # Fix uv's logging level so the complete error is deterministic.
    environment = dict(os.environ, RUST_LOG="error")
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.send(r="startup_marker <- 41L")
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(
            python="import sys; retained_object = object(); retained_object_id = id(retained_object); print(sys.executable)"
        )
        running_python = last_result_text(client).strip()
        assert Path(running_python).is_file(), client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = "<running Python>\n"
        result = client.send(
            python="raise AssertionError('failed preparation ran the cell')",
            requirements={"python": ["py-yaml12<0"]},
        )
        assert result["isError"] is True, result
        output = last_result_text(client)
        assert "No solution found" in output, client.transcript[-1]
        client.transcript[-1]["result"]["content"][0]["text"] = (
            normalize_python_resolution_error(output, executable=running_python)
        )
        client.send(
            # fmt: r
            r=code("""
                stopifnot(isTRUE(reticulate::py_eval(
                  "id(retained_object) == retained_object_id"
                )))
                startup_marker + 1L
                """)
        )
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        client.send(
            python="assert id(retained_object) == retained_object_id; r.startup_marker + 1",
            requirements={"python": ["py-yaml12"]},
        )
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_serializes_selected_python_once_inside_interrupt_boundary(
    binary: Path, execution: Execution
) -> Transcript:
    with deferred_selection_client(binary, execution.serve()) as client:
        # fmt: r
        r = code(r"""
            original_environment <- Sys.getenv(
              c(
                "VIRTUAL_ENV",
                "R_SESSION_INITIALIZED",
                "PYTHONIOENCODING",
                "PATH",
                "LD_LIBRARY_PATH",
                "PYTHONPATH"
              ),
              unset = NA_character_
            )
            selection_serializations <- 0L
            invisible(suppressMessages(base::trace(
              "fromJSON",
              tracer = quote({
                if (
                  is.character(txt) && length(txt) == 1L && startsWith(txt, '{"embedding":')
                ) {
                  selection_serializations <<- selection_serializations + 1L
                  if (selection_serializations == 1L) {
                    base::stop(base::structure(
                      base::list(
                        message = "synthetic serialization interrupt",
                        call = NULL
                      ),
                      class = c("interrupt", "condition")
                    ))
                  }
                }
              }),
              print = FALSE,
              where = asNamespace("jsonlite")
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        client.send(python="raise AssertionError('interrupted selection ran the cell')")
        assert client.transcript[-1]["result"]["isError"] is False, client.transcript[
            -1
        ]
        # fmt: r
        r = code("""
            stopifnot(!reticulate::py_available(initialize = FALSE))
            stopifnot(identical(
              Sys.getenv(names(original_environment), unset = NA_character_),
              original_environment
            ))
            42L
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        client.send(r="stopifnot(selection_serializations == 2L); 42L")
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_activates_without_reticulate_virtualenv_helper(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(
        binary,
        execution.serve("-c", "python.managed.resolution=implicit"),
    ) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            invisible(reticulate::py_config())
            namespace <- asNamespace("reticulate")
            invisible(suppressMessages(base::trace(
              "py_activate_virtualenv",
              tracer = quote(stop("reticulate activation helper was called")),
              print = FALSE,
              where = namespace
            )))
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            initial_prefix = sys.prefix
            live_object = object()
            live_object_id = id(live_object)
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require("py-yaml12")
            config <- reticulate::py_config()
            sys <- reticulate::import("sys", convert = FALSE)
            stopifnot(
              identical(config$executable, reticulate::py_to_r(sys$executable)),
              identical(config$prefix, reticulate::py_to_r(sys$prefix)),
              isTRUE(config$available),
              isTRUE(config$ephemeral)
            )
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess
            import yaml12

            assert id(live_object) == live_object_id
            assert sys.executable != initial_executable
            assert sys.prefix != initial_prefix
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_preserves_setup_after_r_initialization(
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
                """)
        )
        assert last_result_text(client) == "[done]", client.transcript[-1]
        # Load multiprocessing before activation so a spawned child must use
        # the updated interpreter after the new requirements are available.
        # fmt: python
        python = code("""
            import multiprocessing.spawn
            import sys

            initial_executable = sys.executable
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        # fmt: r
        r = code(r"""
            reticulate::py_require(c("matplotlib", "py-yaml12"))
            plt <- reticulate::import("matplotlib.pyplot")
            invisible(plt$show())
            42L
            """)
        output = send_and_collect_runtime_python_resolution(client, r=r, timeout_ms=0)
        assert output == "[1] 42\n", client.transcript[-1]
        # fmt: python
        python = code("""
            import subprocess

            import matplotlib.pyplot as plt

            assert sys.executable != initial_executable
            child_executable = subprocess.check_output(
                [sys.executable, "-c", "import sys, yaml12; print(sys.executable)"],
                text=True,
            ).strip()
            assert child_executable == sys.executable
            with multiprocessing.get_context("spawn").Pool(1) as pool:
                child_executable = pool.apply(eval, ("__import__('sys').executable",))
            assert child_executable == sys.executable
            plt.show()
            42
            """)
        client.send(python=python)
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_managed_import_setup_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    thread_line = runtime_source_line("self._thread = self._threading.get_ident()")
    # The import finder captures its configuring thread after module defaults.
    # Interrupt that public threading call once, then retry without bootstrap.
    source = code("""
        import numpy as np
        import threading
        original_get_printoptions = np.get_printoptions
        original_get_ident = threading.get_ident
        runtime_identity = object()
        runtime_identity_id = id(runtime_identity)
        def configuring_thread():
            threading.get_ident = original_get_ident
            input('Managed import setup> ')
            return original_get_ident()
        def configure_thread_checkpoint():
            np.get_printoptions = original_get_printoptions
            threading.get_ident = configuring_thread
            return original_get_printoptions()
        np.get_printoptions = configure_thread_checkpoint
        """)
    with startup_client(
        binary, execution, source, "-c", "python.managed.resolution=implicit"
    ) as client:
        client.send(python="raise AssertionError('interrupted setup ran the cell')")
        assert last_result_text(client) == (
            '[input requested: "Managed import setup> "]\n[waiting for stdin]'
        ), client.transcript[-1]
        wait_for_evaluation_output(
            client,
            "Traceback (most recent call last):\n"
            f'  File "<string>", line {thread_line}, in configure\n'
            '  File "<setup checkpoint>", line 9, in configuring_thread\n'
            '  File "<string>", line 50, in _console_input\n'
            "KeyboardInterrupt\n",
            "managed import setup interruption",
            control="interrupt",
        )
        client.send(
            python="import yaml12; assert id(runtime_identity) == runtime_identity_id; print('managed import setup retried')"
        )
        assert last_result_text(client) == (
            "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
            "managed import setup retried\n"
        ), client.transcript[-1]
        accepted = client.send(requirements={"action": "get"})["structuredContent"][
            "requirements"
        ]
        assert "py-yaml12" in accepted["python"], accepted
        # Inspect the complete public response before recording the Python fields
        # owned by this case; provider inventories have separate coverage.
        assert (
            json.loads(last_result_text(client))
            == client.transcript[-1]["result"]["structuredContent"]
        )
        client.transcript[-1]["result"] = {
            "accepted_python_requirements": accepted["python"]
        }
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_retries_matplotlib_setup_after_interrupt(
    binary: Path, execution: Execution
) -> Transcript:
    configuration_line = runtime_source_line("_defaults.apply(name)")
    apply_line = runtime_source_line("self._install_show()")
    show_line = runtime_source_line('_setattr(pyplot, "show", show)')
    # A module attribute setter blocks first-cell setup on managed input.
    # Its public input request is the checkpoint for a real interrupt.
    source = code("""
        import sys
        import types

        class InterruptingPyplot(types.ModuleType):
            interrupted = False

            def show(self, *args):
                raise AssertionError("default show was not replaced")

            def __setattr__(self, name, value):
                if name == "show" and not self.interrupted:
                    self.interrupted = True
                    input("Matplotlib setup> ")
                super().__setattr__(name, value)

            def get_fignums(self):
                return []

            def close(self, *args):
                pass

        InterruptingPyplot.show.__module__ = "matplotlib.pyplot"
        sys.modules["matplotlib.pyplot"] = InterruptingPyplot("matplotlib.pyplot")
        runtime_identity = object()
        runtime_identity_id = id(runtime_identity)
        """)
    with startup_client(binary, execution, source) as client:
        client.send(
            # fmt: python
            python=code("""
                raise AssertionError("interrupted setup ran the cell")
                """)
        )
        assert last_result_text(client) == (
            '[input requested: "Matplotlib setup> "]\n[waiting for stdin]'
        )
        wait_for_evaluation_output(
            client,
            "Traceback (most recent call last):\n"
            f'  File "<string>", line {configuration_line}, in _mcp_console_configure_module_defaults\n'
            f'  File "<string>", line {apply_line}, in apply\n'
            f'  File "<string>", line {show_line}, in _mcp_console_install_matplotlib_show\n'
            '  File "<setup checkpoint>", line 13, in __setattr__\n'
            '  File "<string>", line 50, in _console_input\n'
            "KeyboardInterrupt\n",
            "Matplotlib setup interruption",
            control="interrupt",
        )
        client.send(
            # fmt: python
            python=code("""
                assert id(runtime_identity) == runtime_identity_id
                sys.modules["matplotlib.pyplot"].show()
                42
                """)
        )
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_reports_r_first_setup_error_without_replaying_hooks(
    binary: Path, execution: Execution
) -> Transcript:
    # fmt: python
    source = code("""
        import sys

        startup_count = globals().get("startup_count", 0) + 1
        runtime_identity = object()
        runtime_identity_id = id(runtime_identity)


        class FailingPyplot:
            def show(self, *args):
                pass

            def __setattr__(self, name: str, value: object) -> None:
                sys.modules.pop("matplotlib.pyplot")
                raise ValueError("R-first plotting setup failed")


        FailingPyplot.show.__module__ = "matplotlib.pyplot"
        sys.modules["matplotlib.pyplot"] = FailingPyplot()
        """)
    with startup_client(binary, execution, source) as client:
        client.expect(
            "R-first setup error retained\n",
            # fmt: r
            r=code("""
                failure <- tryCatch(reticulate::py_config(), error = identity)
                stopifnot(
                  inherits(failure, "python.builtin.ValueError"),
                  grepl(
                    "R-first plotting setup failed",
                    conditionMessage(failure),
                    fixed = TRUE
                  )
                )
                second <- tryCatch(reticulate::py_config(), error = conditionMessage)
                stopifnot(identical(
                  second,
                  "R/Python attachment is incomplete; restart required"
                ))
                cat("R-first setup error retained\\n")
                """),
        )
        client.expect(
            "Python setup retried without replaying hooks\n",
            # fmt: python
            python=code("""
                assert startup_count == 1
                assert id(runtime_identity) == runtime_identity_id
                print("Python setup retried without replaying hooks")
                """),
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_reports_matplotlib_setup_error_once(
    binary: Path, execution: Execution
) -> Transcript:
    source = code("""
        import sys

        class FailingPyplot:
            def show(self, *args):
                pass

            def __setattr__(self, name: str, value: object) -> None:
                sys.modules.pop("matplotlib.pyplot")
                raise ValueError("matplotlib setup failed")

        FailingPyplot.show.__module__ = "matplotlib.pyplot"
        sys.modules["matplotlib.pyplot"] = FailingPyplot()
        runtime_identity = object()
        runtime_identity_id = id(runtime_identity)
        """)
    with startup_client(binary, execution, source) as client:
        output = wait_for_evaluation_output(
            client,
            None,
            "first Python use reports plotting setup failure",
            completion_timeout_seconds=client.response_timeout,
            python="pass",
        )
        assert output.startswith("Traceback (most recent call last):\n"), output
        assert output.count("ValueError: matplotlib setup failed\n") == 1, output
        assert output.endswith("ValueError: matplotlib setup failed\n"), output
        client.send(python="assert id(runtime_identity) == runtime_identity_id; 42")
        assert last_result_text(client) == "42\n", client.transcript[-1]
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_startup_environment_mutation(
    binary: Path, execution: Execution
) -> Transcript:
    error_line = runtime_source_line("raise RuntimeError(")
    source = "import sys; sys.prefix = 'changed-by-startup-hook'"
    with startup_client(
        binary, execution, source, selected_python=sys.executable
    ) as client:
        client.send(python="raise AssertionError('invalid environment ran code')")
        result = client.transcript[-1]["result"]
        output = last_result_text(client)
        assert result["isError"] is True, result
        traceback = (
            "Traceback (most recent call last):\n"
            f'  File "<string>", line {error_line}, in _mcp_console_configure_environment\n'
            "RuntimeError: embedded Python prefix differs from the selected environment: "
            f"'changed-by-startup-hook' != {sys.prefix!r}\n"
        )
        failure = "Python environment setup failed; restart required\n"
        lifecycle = (
            "[worker sideband read failed: worker sideband closed]\n"
            "[worker exited with status 1]\n"
            "[worker stopped: in-memory state lost]\n"
            "[starting new worker]\n[idle]"
        )
        assert output.endswith(lifecycle), output
        # The Python diagnostic and fatal stderr have independent transports.
        assert_exact_interleaving(output[: -len(lifecycle)], traceback, failure)
        result["content"][0]["text"] = (traceback + failure + lifecycle).replace(
            repr(sys.prefix), "'<selected Python prefix>'"
        )
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
