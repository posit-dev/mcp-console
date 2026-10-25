"""Dependency activation preserves native R project library ownership."""

import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import POSIX, R, command, requires
from support.resolvers import bare_runtime_environment, ir_run_records, ir_requirements
from support.suites import run_this_suite


@requires(R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_replaces_console_layer_at_its_native_profile_position(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        project_library = root / "project-library"
        project_library.mkdir()
        environment, _ = r_test_environment()
        profile = root / ".Rprofile"
        profile.write_text('.libPaths(c("project-library", .libPaths()))\n')
        environment.update(R_PROFILE_USER=str(profile), RETICULATE_PYTHON="")
        with McpClient(
            binary, execution.serve(), environment, root, use_r_startup_files=True
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[done]",
                # fmt: r
                r=code(r"""
                    native_paths <- .libPaths()
                    managed_index <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") 3L else 2L
                    initial_library <- native_paths[[managed_index]]
                    stopifnot(identical(
                      native_paths[[managed_index - 1L]],
                      normalizePath("project-library", winslash = "/")
                    ))
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["praise"]})
            client.expect(
                "project order retained\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    stopifnot(
                      !initial_library %in% paths,
                      identical(paths[-managed_index], native_paths[-managed_index]),
                      identical(dirname(find.package("praise")), paths[[managed_index]])
                    )
                    cat("project order retained\n")
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_preserves_project_copy_of_candidate_across_requirement_changes(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consoleproject", "consolemissing")
        )
        project_library = root / "resolved-r-libraries/consoleproject"
        project_library.mkdir(parents=True)
        (root / "project-extra").mkdir()
        profile = root / ".Rprofile"
        profile.write_text(
            '.libPaths(c(.libPaths(), "resolved-r-libraries/consoleproject", "project-extra"))\n'
        )
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "[done]",
                # fmt: r
                r=code(r"""
                    native_paths <- .libPaths()
                    initial_library <- dirname(find.package("jsonlite"))
                    project_paths <- native_paths[!native_paths %in% initial_library]
                    project_library <- normalizePath(
                      "resolved-r-libraries/consoleproject",
                      winslash = "/"
                    )
                    stopifnot(project_library %in% project_paths)
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consoleproject"]})
            client.expect(
                "[prepared]",
                requirements={"r": ["consoleproject", "consolemissing"]},
            )
            client.expect(
                "project library retained across changes\n",
                # fmt: r
                r=code(r"""
                    paths <- .libPaths()
                    stopifnot(
                      !initial_library %in% paths,
                      all(project_paths %in% paths),
                      identical(paths[paths %in% project_paths], project_paths),
                      consoleproject::fixture(),
                      consolemissing::fixture()
                    )
                    cat("project library retained across changes\n")
                    """),
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_replaces_library_prepared_before_r_initialization(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = recording_fixture_r_environment(
            root, ("consolefirst", "consolesecond")
        )
        profile = root / ".Rprofile"
        profile.write_text(
            'writeLines("started", file.path(Sys.getenv("TMPDIR"), "r-started"))\n'
        )
        environment.update(
            RETICULATE_PYTHON=sys.executable,
            MCP_CONSOLE_LANGUAGES="python,sql",
            R_PROFILE_USER=str(profile),
            TMPDIR=str(root),
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host", "-c", "languages=[r,python,sql]"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            # Hiding R defers its native profile until the Python bridge is
            # used, while public requirements can still prepare R libraries.
            client.expect(
                "[done]",
                # fmt: python
                python=code("""
                    import os
                    from pathlib import Path

                    r_started = Path(os.environ["TMPDIR"]) / "r-started"
                    assert not r_started.exists()
                    """),
            )
            client.expect("[prepared]", requirements={"r": ["consolefirst"]})
            client.expect("[done]", python="assert not r_started.exists()")
            # fmt: r
            source = code(r"""
                native_paths <- .libPaths()
                prepared_library <- dirname(find.package("consolefirst"))
                stopifnot(prepared_library %in% native_paths)
                """)
            client.expect("[done]", python=f"_ = r[{source!r}]")
            client.expect(
                "[done]", python='assert r_started.read_text() == "started\\n"'
            )
            client.expect(
                "[prepared]",
                requirements={"r": ["consolefirst", "consolesecond"]},
            )
            # fmt: r
            source = code(r"""
                paths <- .libPaths()
                replacement <- dirname(find.package("consolesecond"))
                stopifnot(
                  !prepared_library %in% paths,
                  identical(
                    paths[!paths %in% replacement],
                    native_paths[!native_paths %in% prepared_library]
                  ),
                  consolefirst::fixture(),
                  consolesecond::fixture()
                )
                cat("pre-initialization library replaced\n")
                """)
            client.expect(
                "pre-initialization library replaced\n", python=f"_ = r[{source!r}]"
            )
            return client.finish()


@requires(POSIX, R, command("ir"))
@executions(DIRECT, SANDBOXED)
def test_renv_project_keeps_available_packages_and_library_order(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, record = recording_fixture_r_environment(
            root, ("consoleambient", "consolemissing")
        )
        base = subprocess.run(
            [
                environment["MCP_CONSOLE_TEST_REAL_IR"],
                "run",
                "--with",
                "renv",
                "--with",
                "reticulate",
                "--with",
                "jsonlite",
                "--isolated",
                "--vanilla",
                "-e",
                "cat(.libPaths()[[1L]])",
            ],
            env=environment,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        assert Path(base).is_dir(), base
        # Prepare a real renv project before starting sandboxed execution.
        subprocess.run(
            [Path(environment["R_HOME"]) / "bin/Rscript", "--vanilla", "-"],
            # fmt: r
            input=code(r"""
                renv::init(project = getwd(), bare = TRUE, restart = FALSE)
                project <- renv::paths$library(project = getwd())
                for (package in list.files(
                  Sys.getenv("CONSOLE_PROJECT_BASE"),
                  full.names = TRUE
                )) {
                  target <- file.path(project, basename(package))
                  if (!file.exists(target)) stopifnot(file.symlink(package, target))
                }
                ambient <- file.path(
                  strsplit(
                    Sys.getenv("MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES"),
                    .Platform$path.sep
                  )[[1L]][[1L]],
                  "consoleambient"
                )
                stopifnot(file.symlink(ambient, file.path(project, "consoleambient")))
                """),
            env={
                **environment,
                "R_LIBS": base,
                "CONSOLE_PROJECT_BASE": base,
                "RENV_CONFIG_AUTO_SNAPSHOT": "FALSE",
                "RENV_CONFIG_SANDBOX_ENABLED": "FALSE",
            },
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
        )
        (root / "project-extra").mkdir()
        profile = root / ".Rprofile"
        profile.write_text(
            'source("renv/activate.R")\n.libPaths(c(.libPaths(), "project-extra"))\nlibrary(consoleambient)\nproject_paths <- .libPaths()\n'
        )
        environment.update(
            RENV_CONFIG_AUTO_SNAPSHOT="FALSE",
            RENV_CONFIG_STARTUP_QUIET="TRUE",
            RENV_CONFIG_SYNCHRONIZED_CHECK="FALSE",
            RENV_CONFIG_SANDBOX_ENABLED="FALSE",
            RENV_CONFIG_USER_PROFILE="FALSE",
            R_PROFILE_USER=str(profile),
            RETICULATE_PYTHON=sys.executable,
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "ambient available\n",
                r='stopifnot(consoleambient::fixture(), identical(as.character(packageVersion("consoleambient")), "0.0.0.9000")); cat("ambient available\\n")',
            )
            baseline = len(ir_run_records(record))
            client.expect(
                "ambient retained\n",
                r='stopifnot(requireNamespace("consoleambient"), consoleambient::fixture()); cat("ambient retained\\n")',
            )
            assert len(ir_run_records(record)) == baseline
            client.expect(
                "project paths retained\n",
                r='stopifnot(requireNamespace("consolemissing", quietly = TRUE), consolemissing::fixture(), identical(.libPaths()[.libPaths() %in% project_paths], project_paths)); cat("project paths retained\\n")',
            )
            runs = ir_run_records(record)[baseline:]
            assert len(runs) == 1 and "consolemissing" in ir_requirements(runs[0]), runs
            return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_bare_worker_clears_inherited_console_library(
    binary: Path, execution: Execution
) -> Transcript:
    with TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, _ = r_test_environment()
        environment = bare_runtime_environment(environment, root / "r-library")
        environment["MCP_CONSOLE_R_LIBRARY"] = str(root / "retired-library")
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            source = 'stopifnot(Sys.getenv("MCP_CONSOLE_R_LIBRARY") == ""); cat("bare library ready\\n")'
            client.expect("bare library ready\n", r=source)
            client.send(control="restart")
            client.expect("bare library ready\n", r=source)
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
