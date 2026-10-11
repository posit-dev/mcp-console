#!/usr/bin/env -S uv run --script

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, R_EVENT_LOOP, command, requires
from support.assertions import (
    last_result_text,
    release_worker_callback_gate,
    wait_for_evaluation_output,
    wait_for_idle_output,
)
from support.client import McpClient, stop_client
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_process_diagnostic
from support.r import r_test_environment
from support.records import Transcript
from support.snapshots import platform_snapshots
from support.resolvers import (
    checkpoint_uv_environment,
    ir_requirements,
    ir_run_records,
    recording_ir_environment,
)
from support.suites import run_this_suite
from support.python import isolated_python


def named_requirement_error(requirement: str) -> str:
    return (
        f"Python requirement `{requirement}` is not accepted: host-side managed "
        "resolution accepts named package requirements only"
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_rejects_unsupported_ir_version(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        fake_bin = workspace / "bin"
        fake_bin.mkdir()
        fake_ir = fake_bin / "ir"
        fake_ir.write_text(
            code(r"""
                #!/bin/sh

                set -eu
                if [ "$#" -eq 1 ] && [ "$1" = "--version" ]; then
                  printf 'ir 0.3.0\n'
                  exit 0
                fi
                printf 'started\n' > "$MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"
                exit 97
                """),
            encoding="utf-8",
        )
        fake_ir.chmod(0o755)
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        run_marker = workspace / "unsupported-ir-ran"
        environment["MCP_CONSOLE_UNSUPPORTED_IR_RUN_MARKER"] = str(run_marker)
        zod = Path(__file__).resolve().parents[3] / "fixtures" / "zod"

        client = McpClient(
            binary,
            execution.serve("--worker", str(zod)),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        client.send(
            requirements={"r": ["local::package"]},
        )
        result = client.transcript[-1]["result"]
        assert not run_marker.exists(), "unsupported `ir` reached package resolution"
        assert result["isError"] is True, result
        assert result["content"][0]["text"] == (
            "R package resolution requires `ir` 0.4.0 or later; found `ir` 0.3.0"
        ), result
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_rejects_local_r_installation(binary: Path, execution: Execution) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    environment.pop("IR_NO_LOCAL_SOURCES", None)

    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary).resolve()
        fixture = Path(__file__).resolve().parents[3] / "fixtures" / "r_install_escape"
        package = workspace / "package"
        shutil.copytree(fixture, package)
        (package / "inst").mkdir()
        (package / "inst" / "nonce").write_text(str(workspace), encoding="utf-8")

        install_marker = workspace / "package-configure-ran"
        environment["MCP_CONSOLE_R_INSTALL_MARKER"] = str(install_marker)

        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        reference = f"local::{package}?reinstall&nocache"
        client.send(requirements={"r": [reference]})
        result = client.transcript[-1]["result"]
        assert not install_marker.exists(), (
            "local package configure ran with server permissions"
        )
        assert result["isError"] is True, result
        error = result["content"][0]["text"]
        assert "IR_NO_LOCAL_SOURCES is set" in error, error
        assert "mcpconsolerinstallescape" in error, error
        assert "Use a remote package source" in error, error
        progress, diagnostic_start, diagnostic = error.partition(
            "Error: IR_NO_LOCAL_SOURCES is set"
        )
        status = "exit code" if os.name == "nt" else "exit status"
        failure_prefix = f"R package resolution failed with {status}: 1: "
        assert progress.startswith(failure_prefix), error
        assert diagnostic_start and "Resolving" in progress, error
        # `ir` may load cached metadata or refresh it before the same rejection.
        error = (
            f"{failure_prefix}<cache-dependent `ir` progress>\n"
            f"{diagnostic_start}{diagnostic}"
        )
        client.transcript[-1]["send"]["requirements"]["r"] = [
            reference.replace(str(package), "<absolute package path>")
        ]
        result["content"][0]["text"] = normalize_process_diagnostic(error).replace(
            str(package), "<absolute package path>"
        )
        client.transcript[-1]["transcript_normalization"] = {
            "target": "result.content[0].text",
            "replacements": {
                "cache_dependent_ir_progress": "<cache-dependent `ir` progress>",
            },
        }
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_prepares_and_uses_cran_packages(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(
        requirements={"r": ["praise, zeallot"]},
    )
    assert last_result_text(client) == "[prepared]", client.transcript[-1]

    # fmt: r
    r = code(r"""
        managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
        stopifnot(
          identical(dirname(find.package("praise")), .libPaths()[[managed_index]]),
          identical(dirname(find.package("zeallot")), .libPaths()[[managed_index]])
        )
        result <- dplyr::summarise(
          data.frame(value = c(40L, 2L)),
          answer = sum(.data$value)
        )
        praise::praise(sprintf("answer: %d", result$answer))
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "answer: 42"\n'
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_prepares_with_empty_stdin_then_restarts(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"r": ["praise"]})
        assert last_result_text(client) == "[prepared]"
        client.send(stdin="", requirements={"r": ["praise"]})
        assert last_result_text(client) == "[prepared]"
        client.send(control="restart", requirements={"r": ["praise"]})
        assert last_result_text(client) == "[starting new worker]\n[idle]", (
            client.transcript[-1]
        )

        # fmt: r
        r = code(r"""
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            stopifnot(
              identical(dirname(find.package("praise")), .libPaths()[[managed_index]])
            )
            sentinel <- 42L
            worker_pid <- Sys.getpid()
            praise::praise("ready")
            """)
        client.send(r=r)
        assert last_result_text(client) == '[1] "ready"\n'
        client.send(stdin="", requirements={"r": ["praise"]})
        assert last_result_text(client) == "[prepared]"
        client.send(r="stopifnot(identical(Sys.getpid(), worker_pid)); sentinel")
        assert last_result_text(client) == "[1] 42\n"

        # Prepare a new package while replacing the live worker.
        client.send(control="restart", requirements={"r": ["zeallot"]})
        assert last_result_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        # fmt: r
        r = code(r"""
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            stopifnot(
              !exists("sentinel"),
              !exists("worker_pid"),
              identical(dirname(find.package("praise")), .libPaths()[[managed_index]]),
              identical(dirname(find.package("zeallot")), .libPaths()[[managed_index]])
            )
            praise::praise("restarted")
            """)
        client.send(r=r)
        assert last_result_text(client) == '[1] "restarted"\n'
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_sends_r_cell_with_initial_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()

    # fmt: r
    r = code(r"""
        managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
        stopifnot(
          identical(dirname(find.package("praise")), .libPaths()[[managed_index]])
        )
        praise::praise("ready")
        """)
    wait_for_evaluation_output(
        client,
        None,
        "R cell with initial requirements",
        completion_timeout_seconds=client.response_timeout,
        r=r,
        requirements={"r": ["praise"]},
    )
    output = last_result_text(client)
    assert output.startswith('[1] "') and "ready" in output, output
    assert "[prepared]" not in output, output
    return client.finish()


@executions(DIRECT, SANDBOXED)
def test_prepares_r_requirements_after_worker_startup(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["praise"]})
    assert last_result_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
        sandbox_library <- if (managed_index == 2L) .libPaths()[[1L]] else NULL
        initial_library <- .libPaths()[[managed_index]]
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]"

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          is.null(sandbox_library) || identical(.libPaths()[[1L]], sandbox_library),
          !initial_library %in% .libPaths(),
          identical(dirname(find.package("zeallot")), .libPaths()[[managed_index]])
        )
        42L
        """)
    client.send(r=r, requirements={"r": ["zeallot"]})
    assert last_result_text(client) == "[1] 42\n"
    return client.finish()


@requires(R_EVENT_LOOP)
@executions(DIRECT, SANDBOXED)
@platform_snapshots(
    "win32",
    reason="Forced worker retirement reports a Windows exit status or Unix signal",
)
def test_stops_live_preparation_for_idle_callback_input(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["later"]})

    # fmt: r
    r = code(r"""
        reticulate::py_require("py-yaml12")
        invisible(reticulate::py_config())
        callback_gate <- tempfile("mcp-console-callback-gate-")
        callback_checkpoint <- tempfile("mcp-console-callback-checkpoint-")
        run_callback <- function() {
          if (!file.exists(callback_gate)) {
            later::later(run_callback, delay = 0.01)
            return(invisible(NULL))
          }
          # later caps one top-level handler turn at 20 callback passes.
          # Leave the input callback ready for the next handler turn.
          request_input <- function(turns) {
            if (turns == 0L) {
              stopifnot(file.create(callback_checkpoint))
              readline("later> ")
              # Keep the submitted callback alive if relay retirement closes
              # fd 0 before the force-stop signal reaches the worker.
              repeat {
                Sys.sleep(1)
              }
            } else {
              later::later(function() request_input(turns - 1L), delay = 0)
            }
          }
          request_input(25L)
        }
        later::later(run_callback, delay = 0.01)
        cat(callback_gate, callback_checkpoint, sep = "\n")
        """)
    client.send(r=r)
    release_worker_callback_gate(client, "idle input callback")
    wait_for_idle_output(
        client,
        '[input requested: "later> "]\n[waiting for stdin]',
        "idle callback input request",
    )
    # Keep this distinct from the callback's retained requirement so activation
    # cannot turn the preparation into an idempotent server-side no-op.
    result = client.send(
        requirements={"python": ["py-yaml12>=0"]},
    )
    assert result["isError"] is True, result
    retirement = (
        "[worker exited with status 1]"
        if sys.platform == "win32"
        else "[worker terminated by signal 9]"
    )
    assert result["content"][0]["text"] == (
        '[idle R callback requested input "later> " during requirement '
        "preparation; collect callback input with send before preparing "
        f"requirements]\n{retirement}\n"
        "[worker stopped: in-memory state lost]"
    ), result
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"), command("uv"))
def test_failed_mixed_preparation_retains_live_python_activation(
    binary: Path,
    execution: Execution,
) -> Transcript:
    requirement = "mcpconsolepreparationfixture"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment, uv_started, uv_release = checkpoint_uv_environment(
            temporary, "py-yaml12"
        )
        r_environment, _ = r_test_environment()
        environment["R_HOME"] = r_environment["R_HOME"]

        real_ir = shutil.which("ir")
        assert real_ir is not None, "real `ir` is required"
        fake_bin = temporary / "bin"
        fake_bin.mkdir()
        (fake_bin / "ir").symlink_to(
            Path(__file__).resolve().parents[3] / "fixtures" / "selective_ir"
        )
        path = environment.get("PATH")
        assert path is not None, "PATH is required"
        environment["PATH"] = os.pathsep.join((str(fake_bin), path))
        candidate = temporary / "candidate-r-library"
        candidate.mkdir()
        environment["MCP_CONSOLE_TEST_REAL_IR"] = real_ir
        environment["MCP_CONSOLE_TEST_IR_REQUIREMENT"] = requirement
        environment["MCP_CONSOLE_TEST_IR_LIBRARY"] = str(candidate)

        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        passed = False
        try:
            client.initialize_and_list_tools()
            # fmt: r
            setup = code(r"""
                invisible(reticulate::py_config())
                managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
                cat(.libPaths()[[managed_index]])
                """)
            client.send(r=setup)
            initial_library = Path(last_result_text(client))
            assert initial_library.is_dir(), initial_library
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<initial managed R library>"
            )
            for package in initial_library.iterdir():
                (candidate / package.name).symlink_to(
                    package,
                    target_is_directory=package.is_dir(),
                )

            preparation = client.start_send(
                requirements={"r": [requirement], "python": ["py-yaml12"]},
            )
            uv_started.wait("mixed live requirement preparation")
            for package in candidate.iterdir():
                package.unlink()
            candidate.rmdir()
            candidate.write_text("not an R library", encoding="utf-8")
            uv_release.release()
            client.receive(preparation)
            result = preparation["result"]
            assert result["isError"] is True, result
            assert result["content"][0]["text"] == (
                "resolved R library was not added to .libPaths(); further "
                "requirement changes are unavailable until session restart"
            )

            client.send(control="restart")
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            # fmt: python
            python = code("""
                import yaml12

                yaml12.format_yaml({"answer": 42})
                """)
            client.send(python=python)
            assert last_result_text(client) == "'answer: 42'\n", client.transcript[-1]
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            uv_release.release()
            uv_started.close()
            uv_release.close()
            if not passed:
                stop_client(client)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_preparation_preserves_input_before_native_r_startup(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        python, site = isolated_python(root)
        (site / "sitecustomize.py").write_text(
            # fmt: python
            code("""
                import builtins
                import sys

                if sys.argv[0] != "-c":
                    builtins.startup_answer = input("startup> ")
                """)
        )
        environment, _ = r_test_environment()
        environment.update(
            RETICULATE_PYTHON=str(python),
            PYTHONPATH=str(site),
            RETICULATE_PYTHONPATH=str(site),
        )
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            wait_for_idle_output(
                client,
                '[input requested: "startup> "]\n[waiting for stdin]',
                "Python input before native R startup",
                completion_timeout_seconds=client.response_timeout,
            )
            for requirements in ({"r": ["dplyr"]}, {"action": "reset"}):
                result = client.send(requirements=requirements)
                assert result["isError"] is False, result
                assert result["content"][0]["text"] == "[prepared]", result
            result = client.send(requirements={"r": ["praise"]})
            assert result["isError"] is True, result
            assert result["content"][0]["text"] == (
                "runtime startup requested input; supply stdin before preparing requirements"
            ), result
            client.expect(
                "'ready'\n",
                python="import builtins; builtins.startup_answer",
                stdin="ready\n",
            )
            client.expect("[1] 42\n", r="42L")
            client.send(requirements={"r": ["praise"]})
            assert last_result_text(client) == "[prepared]", client.transcript[-1]
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_failed_late_mixed_preparation_preserves_worker(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    client.send(requirements={"r": ["praise"]})
    assert last_result_text(client) == "[prepared]"

    # fmt: r
    r = code(r"""
        sentinel <- 42L
        worker_pid <- Sys.getpid()
        initial_lib_paths <- .libPaths()
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]", client.transcript[-1]

    invalid_python = "example @ https://example.invalid/example.whl"
    client.send(
        requirements={
            "r": ["zeallot"],
            "python": [invalid_python],
            "duckdb": ["not_a_real_duckdb_extension"],
        },
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert result["content"][0]["text"] == named_requirement_error(invalid_python)

    # fmt: r
    r = code(r"""
        stopifnot(
          identical(sentinel, 42L),
          identical(Sys.getpid(), worker_pid),
          identical(.libPaths(), initial_lib_paths)
        )
        42L
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 42\n"
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_evaluates_with_default_managed_r(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        environment, record = recording_ir_environment(workspace)
        environment["MCP_CONSOLE_LANGUAGES"] = "r"
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(ambient_library), environment.get("R_LIBS")))
        )

        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        )
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
            managed_packages <- c(
              "tidyverse",
              "dplyr",
              "dbplyr",
              "reticulate",
              "DBI",
              "duckdb",
              "arrow",
              "nanoarrow",
              "yyjsonr",
              "ggplot2"
            )
            # Loaded namespaces report canonical cache paths; ir libraries can
            # contain symlinks to those same package directories.
            stopifnot(
              !reticulate::py_available(initialize = FALSE),
              vapply(
                managed_packages,
                function(package) {
                  identical(
                    normalizePath(find.package(package)),
                    normalizePath(file.path(.libPaths()[[managed_index]], package))
                  )
                },
                logical(1L)
              ),
              vapply(
                c("ggplot2", "dplyr", "readr", "jsonlite", "yyjsonr"),
                requireNamespace,
                logical(1L),
                quietly = TRUE
              )
            )
            csv <- readr::read_csv(I("value\n40\n2\n"), show_col_types = FALSE)
            json <- jsonlite::fromJSON('[{"value": 40}, {"value": 2}]')
            stopifnot(identical(as.integer(csv$value), json$value))
            dplyr::summarise(csv, answer = sum(.data$value))$answer
            """)
        client.send(r=r)
        assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
        # Repository labels vary by mirror. Check the default package references
        # passed to ir, including the registry release of reticulate.
        runs = ir_run_records(record)
        assert len(runs) == 1, runs
        assert set(ir_requirements(runs[0])) == {
            "tidyverse",
            "dplyr",
            "dbplyr",
            "reticulate",
            "DBI",
            "duckdb",
            "arrow",
            "nanoarrow",
            "yyjsonr",
            "ggplot2",
            "jsonlite",
            "pillar",
            "tibble",
            "utf8",
        }, runs
        client.send(
            requirements={"r": ["DBI", "duckdb", "arrow", "nanoarrow"]},
        )
        assert last_result_text(client) == "[prepared]", client.transcript[-1]
        assert ir_run_records(record) == runs
        return client.finish()[3:]


@executions(DIRECT, SANDBOXED)
def test_prepares_initial_r_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    initial_r = "praise"
    candidate_r = "zeallot"
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        ambient_library = workspace / "ambient-library"
        ambient_library.mkdir()
        environment["R_LIBS"] = os.pathsep.join(
            filter(None, (str(ambient_library), environment.get("R_LIBS")))
        )
        environment["MCP_CONSOLE_AMBIENT_R_LIBRARY"] = str(ambient_library)

        with McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(
                requirements={"r": [initial_r]},
            )
            assert last_result_text(client) == "[prepared]"

            invalid_r = "not a valid requirement !!!"
            reference = subprocess.run(
                [
                    "ir",
                    "run",
                    "--rscript",
                    str(rscript),
                    "--with",
                    invalid_r,
                    "--isolated",
                    "--vanilla",
                    "-e",
                    "42",
                ],
                env=environment
                | {"IR_NO_LOCAL_SOURCES": "1", "PKG_SUBPROCESS_TIMEOUT": "60000"},
                cwd=workspace,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
            )
            assert reference.returncode == 1, reference
            client.send(
                requirements={"r": [invalid_r]},
            )
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            error = result["content"][0]["text"]
            status = "exit code" if os.name == "nt" else "exit status"
            failure_prefix = f"R package resolution failed with {status}: 1: "
            assert error.startswith(failure_prefix + "Error:"), error
            assert f"Cannot parse package: {invalid_r}." in error, error
            assert error.replace("\r\n", "\n").endswith(
                "Execution halted\nir: dependency resolution failed"
            ), error
            assert error.replace("\r\n", "\n") == (
                f"{failure_prefix}{reference.stderr.strip()}"
            ), error
            # Keep the complete diagnostic; `ir` releases change pkg_deps arguments.
            pak_call = next(
                line
                for line in error.splitlines()
                if line.startswith("3. pak::pkg_deps(")
            )
            assert error.count(pak_call) == 1, error
            result["content"][0]["text"] = (
                normalize_process_diagnostic(error)
                .replace(pak_call, "3. pak::pkg_deps(<ir-version-dependent arguments>)")
                .replace("ℹ See", "i See")
                .replace("…", "...")
            )
            client.transcript[-1]["transcript_normalization"] = {
                "target": "result.content[0].text",
                "replacements": {
                    "ir_pkg_deps_arguments": "<ir-version-dependent arguments>",
                },
            }

            invalid_python = "example @ https://example.invalid/example.whl"
            client.send(
                requirements={
                    "r": [candidate_r],
                    "python": [invalid_python],
                },
            )
            result = client.transcript[-1]["result"]
            assert result["isError"] is True, result
            assert result["content"][0]["text"] == named_requirement_error(
                invalid_python
            )

            # fmt: r
            r = code(r"""
                managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
                stopifnot(
                  identical(
                    dirname(find.package("praise")),
                    .libPaths()[[managed_index]]
                  ),
                  normalizePath(.libPaths()[[managed_index + 1L]]) ==
                    normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
                )
                42L
                """)
            client.send(r=r)
            assert last_result_text(client) == "[1] 42\n", client.transcript[-1]

            client.send(
                requirements={"r": [initial_r]},
            )
            assert last_result_text(client) == "[prepared]"
            client.send(
                requirements={
                    "r": [candidate_r],
                    "python": ["py-yaml12"],
                },
            )
            assert last_result_text(client) == "[prepared]"

            # fmt: r
            prepared_r = code(r"""
                managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 2L else 1L
                stopifnot(
                  "py-yaml12" %in% reticulate::py_require()$packages,
                  identical(
                    dirname(find.package("praise")),
                    .libPaths()[[managed_index]]
                  ),
                  identical(
                    dirname(find.package("zeallot")),
                    .libPaths()[[managed_index]]
                  ),
                  normalizePath(.libPaths()[[managed_index + 1L]]) ==
                    normalizePath(Sys.getenv("MCP_CONSOLE_AMBIENT_R_LIBRARY"))
                )
                42L
                """)
            client.send(r=prepared_r)
            assert last_result_text(client) == "[1] 42\n"

            client.send(control="restart")
            assert last_result_text(client) == (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            client.send(r=prepared_r)
            assert last_result_text(client) == "[1] 42\n"
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
