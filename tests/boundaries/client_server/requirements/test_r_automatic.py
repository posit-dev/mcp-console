#!/usr/bin/env -S uv run --script

import os
import select
import shutil
import subprocess
import sys
import tempfile
from functools import cache
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.requirements import POSIX, PROCESS_EVENTS, R, SQL, command, requires
from support.assertions import (
    entry_result_text,
    last_result_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint, wait_for_worker_file
from support.client import McpClient, stop_client
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.processes import (
    capture_process_identity,
    child_process_identities,
    live_processes,
)
from support.records import Transcript
from support.r import r_test_environment, reference_r_error
from support.resolvers import (
    ir_requirements,
    ir_run_records,
    local_resolver_owner,
    recording_ir_environment,
)
from support.suites import run_this_suite


def fixture_r_libraries(
    environment: dict[str, str],
    directory: Path,
    packages: tuple[str, ...],
) -> tuple[Path, Path]:
    source_root = directory / "r-package-sources"
    libraries = (
        directory / "r-package-library-one",
        directory / "r-package-library-two",
    )
    source_root.mkdir()
    sources: tuple[list[Path], list[Path]] = ([], [])
    for library in libraries:
        library.mkdir()
    for index, package in enumerate(packages):
        source = source_root / package
        (source / "R").mkdir(parents=True)
        (source / "DESCRIPTION").write_text(
            f"""\
Package: {package}
Title: MCP Console Runtime Resolution Fixture
Version: 0.0.0.9000
Authors@R: person("MCP Console", role = c("aut", "cre"), email = "fixture@example.com")
Description: Provides a function for automatic R resolution transcript tests.
License: MIT
Encoding: UTF-8
""",
            encoding="utf-8",
        )
        (source / "NAMESPACE").write_text("export(fixture)\n", encoding="utf-8")
        (source / "R" / "fixture.R").write_text(
            "fixture <- function() TRUE\n", encoding="utf-8"
        )
        sources[index % len(sources)].append(source)

    r_home = Path(environment["R_HOME"])
    for library, library_sources in zip(libraries, sources, strict=True):
        if not library_sources:
            continue
        subprocess.run(
            [
                r_home / "bin/R",
                "CMD",
                "INSTALL",
                "--use-vanilla",
                f"--library={library}",
                "--no-docs",
                "--no-help",
                "--no-byte-compile",
                "--no-test-load",
                *library_sources,
            ],
            check=True,
            capture_output=True,
            env=environment,
        )
    return libraries


@cache
def installed_fixture_r_libraries(
    packages: tuple[str, ...],
) -> tuple[tempfile.TemporaryDirectory, tuple[Path, Path]]:
    # Each case runs its execution modes sequentially in one process. Retain
    # only immutable installed packages until that process exits; library
    # views, resolver records, and worker state remain execution-local.
    temporary = tempfile.TemporaryDirectory(prefix="mcp-console-r-packages-")
    environment, _ = r_test_environment()
    return temporary, fixture_r_libraries(environment, Path(temporary.name), packages)


def recording_fixture_r_environment(
    directory: Path,
    packages: tuple[str, ...],
) -> tuple[dict[str, str], Path]:
    environment, record = recording_ir_environment(directory)
    isolated_library = directory / "r-library"
    environment["R_LIBS_SITE"] = str(isolated_library)
    environment["R_LIBS_USER"] = str(isolated_library)
    _, source_libraries = installed_fixture_r_libraries(packages)
    environment["MCP_CONSOLE_TEST_IR_SOURCE_LIBRARIES"] = os.pathsep.join(
        map(str, source_libraries)
    )
    environment["MCP_CONSOLE_TEST_IR_LIBRARY_ROOT"] = str(
        directory / "resolved-r-libraries"
    )
    environment["MCP_CONSOLE_TEST_IR_FIXTURE_PACKAGES"] = ",".join(packages)
    environment["MCP_CONSOLE_TEST_IR_BASE_LIBRARY"] = str(directory / "base-r-library")
    return environment, record


def send_and_collect_runtime_r_resolution(
    client: McpClient,
    expected: str,
    **arguments: object,
) -> None:
    wait_for_evaluation_output(
        client,
        expected,
        "automatic R resolution",
        completion_timeout_seconds=client.response_timeout,
        **arguments,
    )


def send_and_compare_r_error(
    client: McpClient, environment: dict[str, str], source: str
) -> None:
    expected = reference_r_error(environment, source)
    send_and_collect_runtime_r_resolution(client, expected, r=source)
    # Like plot references, record the live comparison after exact equality.
    client.transcript[-1]["result"]["content"][0]["text"] = (
        "<error output identical to live Rscript>"
    )
    client.transcript[-1]["transcript_normalization"] = {
        "target": "result.content[0].text",
        "reference": "same source in live Rscript --vanilla",
        "comparison": "exact equality, including Calls; excluding Execution halted",
    }


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_resolves_missing_r_packages_during_evaluation(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        packages = ("mcpfirst", "mcpsecond")
        environment, _ = recording_fixture_r_environment(directory, packages)
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()

        client.send(python="python_sentinel = 40")
        output = last_result_text(client)
        assert output == "[done]", repr(output)
        client.send(sql="CREATE TABLE automatic_r_state AS SELECT 42 AS answer")

        # fmt: r
        setup = code(r"""
            sentinel <- 42L
            worker_pid <- Sys.getpid()
            """)
        client.expect(r=setup)

        # fmt: r
        r = code(r"""
            stopifnot(is.function(mcpfirst::fixture))
            suppressPackageStartupMessages(library(mcpsecond))
            stopifnot(
              identical(sentinel, 42L),
              identical(Sys.getpid(), worker_pid),
              suppressPackageStartupMessages(require(mcpfirst, quietly = TRUE)),
              requireNamespace("mcpsecond", quietly = TRUE)
            )
            connection <- suppressWarnings(file("/dev/stdin"))
            on.exit(close(connection))
            input <- readLines(connection, n = 1L)
            cat("answer: ", input, "\n", sep = "")
            """)
        client.send(r=r, stdin="42\n")
        output = last_result_text(client)
        assert output == "answer: 42\n", repr(output)

        client.expect("42\n", python="python_sentinel + 2")
        client.send(sql="SELECT answer FROM automatic_r_state")
        assert last_result_text(client).splitlines()[-1].split() == ["1", "42"]
        return client.finish()


@requires(R, POSIX, SQL)
@executions(RUNTIME)
@requires(command("ir"))
def test_does_not_resolve_missing_r_packages_from_sql_callbacks(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcpsqlcallback"
        environment, record = recording_fixture_r_environment(directory, (package,))
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()

        # fmt: r
        r = code(r"""
            sql_requires_package <- function() {
              as.integer(suppressWarnings(requireNamespace(
                "mcpsqlcallback",
                quietly = TRUE
              )))
            }
            invisible()
            """)
        client.expect(r=r)

        # fmt: python
        python = code("""
            import sqlite3

            connection = sqlite3.connect(":memory:")
            connection.create_function("sql_requires_package", 0, r.sql_requires_package)
            _console.sql_connection(connection)
            """)
        client.expect(python=python)
        baseline = len(ir_run_records(record))

        client.send(sql="SELECT sql_requires_package() AS resolved")
        preview = last_result_text(client)
        assert preview.splitlines()[-1].split() == ["0"], preview
        assert len(ir_run_records(record)) == baseline

        # Automatic resolution resumes outside the SQL evaluation.
        # fmt: r
        r = code(r"""
            stopifnot(
              requireNamespace("mcpsqlcallback", quietly = TRUE),
              is.function(mcpsqlcallback::fixture)
            )
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=r)
        runs = ir_run_records(record)[baseline:]
        assert len(runs) == 1, runs
        assert package in ir_requirements(runs[0]), runs
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_resolves_reached_r_packages_at_runtime(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        packages = (
            "mcplibrary",
            "mcpcharacter",
            "mcprequire",
            "mcpnamespace",
            "mcpload",
            "mcpinternal",
            "mcpdynamic",
            "mcpdynamicns",
        )
        environment, record = recording_fixture_r_environment(directory, packages)
        environment["PKG_SUBPROCESS_TIMEOUT"] = "0"
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        # fmt: r
        static = code(r"""
            invisible(suppressWarnings(suppressPackageStartupMessages({
              base::library(package = mcplibrary, quietly = TRUE)
              library(
                "mcpcharacter",
                help = stats,
                character.only = TRUE,
                quietly = TRUE
              )
              stopifnot(
                base::require(mcprequire, quietly = TRUE),
                base::requireNamespace("mcpnamespace", quietly = TRUE),
                is.environment(base::loadNamespace("mcpload")),
                is.environment(mcpinternal:::.__NAMESPACE__.),
                is.function(mcplibrary::fixture),
                is.function("mcplibrary"::fixture),
                is.function(mcplibrary::fixture)
              )
            })))
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=static)
        static_runs = ir_run_records(record)[baseline:]
        static_packages = packages[:6]
        assert len(static_runs) == len(static_packages), static_runs
        for index, (run, package) in enumerate(
            zip(static_runs, static_packages, strict=True)
        ):
            requirements = ir_requirements(run)
            assert requirements.count(package) == 1, requirements
            for retained in static_packages[: index + 1]:
                assert requirements.count(retained) == 1, requirements
            assert all(
                later not in requirements for later in static_packages[index + 1 :]
            )
            assert run["no_local_sources"] == "1", run
            assert run["subprocess_timeout"] == "60000", run

        dynamic_baseline = len(ir_run_records(record))
        # fmt: r
        dynamic = code(r"""
            invisible(suppressWarnings(suppressPackageStartupMessages({
              attached <- "mcpdynamic"
              stopifnot(do.call(
                base::library,
                list(
                  package = attached,
                  help = NULL,
                  character.only = TRUE,
                  logical.return = TRUE,
                  quietly = TRUE
                )
              ))
              package <- "mcpdynamicns"
              stopifnot(do.call(
                base::requireNamespace,
                list(package = package, quietly = TRUE)
              ))
            })))
            42L
            """)
        send_and_collect_runtime_r_resolution(client, "[1] 42\n", r=dynamic)
        dynamic_runs = ir_run_records(record)[dynamic_baseline:]
        assert len(dynamic_runs) == 2, dynamic_runs
        assert "mcpdynamic" in ir_requirements(dynamic_runs[0]), dynamic_runs
        assert "mcpdynamicns" in ir_requirements(dynamic_runs[1]), dynamic_runs
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_retains_automatic_r_package_after_error_and_restart(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            stopifnot(is.function(fortunes::fortune))
            stop("after activation")
            """)
        wait_for_evaluation_output(
            client,
            "Error: after activation\n",
            "automatic R package error",
            completion_timeout_seconds=client.response_timeout,
            r=r,
            timeout_ms=0,
        )
        assert len(ir_run_records(record)) == baseline + 1

        client.expect(
            "[1] 42\n",
            r='stopifnot(requireNamespace("fortunes", quietly = TRUE)); 42L',
        )
        assert len(ir_run_records(record)) == baseline + 1

        client.expect(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
            control="restart",
        )
        client.expect(
            "[1] 42\n",
            r='stopifnot(requireNamespace("fortunes", quietly = TRUE)); 42L',
        )
        assert len(ir_run_records(record)) == baseline + 1
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_preserves_missing_package_conditions_after_resolution_failure(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(
            Path(temporary), fail_requirement="notloaded.pkg"
        )
        # fmt: r
        caught = code(r"""
            tryCatch(
              loadNamespace("notloaded.pkg"),
              packageNotFoundError = function(e) "expected missing-package error"
            )
            """)
        # Compare the original condition, including its call and package fields,
        # with this host's R rather than recreating R's condition in the test.
        # fmt: r
        details = code(r"""
            describe_missing <- function(expr) {
              tryCatch(
                withCallingHandlers(
                  expr,
                  packageNotFoundError = function(e) {
                    cat("calling handler: ")
                    print(class(e))
                  }
                ),
                packageNotFoundError = function(e) {
                  print(e)
                  print(list(
                    message = conditionMessage(e),
                    call = deparse(conditionCall(e)),
                    package = e$package,
                    lib.loc = if (is.null(e$lib.loc)) {
                      NULL
                    } else {
                      identical(e$lib.loc, .libPaths())
                    }
                  ))
                  invisible(NULL)
                }
              )
            }
            describe_missing(loadNamespace("notloaded.pkg"))
            describe_missing(notloaded.pkg::missing)
            describe_missing(notloaded.pkg:::missing)
            describe_missing(library(notloaded.pkg))
            print(requireNamespace("notloaded.pkg"))
            print(requireNamespace("notloaded.pkg", quietly = TRUE))
            suppressWarnings(require(notloaded.pkg, quietly = TRUE))
            suppressWarnings(library(notloaded.pkg, logical.return = TRUE))
            """)
        reference = subprocess.run(
            [Path(environment["R_HOME"]) / "bin/Rscript", "--vanilla", "-"],
            input=details,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=True,
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment
        ) as client:
            client.initialize_and_list_tools()
            for restart in (False, True):
                if restart:
                    client.send(control="restart")
                send_and_collect_runtime_r_resolution(
                    client, '[1] "expected missing-package error"\n', r=caught
                )
                baseline = len(ir_run_records(record))
                send_and_collect_runtime_r_resolution(
                    client, reference.stdout, r=details
                )
                client.transcript[-1]["result"]["content"][0]["text"] = (
                    "<condition classes, messages, calls, and fields identical to live Rscript>"
                )
                client.transcript[-1]["transcript_normalization"] = {
                    "target": "result.content[0].text",
                    "reference": "same source in live Rscript --vanilla",
                    "comparison": "exact equality of printed condition details",
                }
                runs = ir_run_records(record)[baseline:]
                assert len(runs) == 8, runs
                assert all("notloaded.pkg" in ir_requirements(run) for run in runs)

            # Explicit preparation still reports the resolver's failure.
            client.send(requirements={"r": ["notloaded.pkg"]})
            assert "synthetic `ir` failure for notloaded.pkg" in last_result_text(
                client
            )
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_matches_base_r_missing_package_error_display(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, _ = recording_ir_environment(
            Path(temporary), fail_requirement="notloaded.pkg"
        )
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment
        ) as client:
            client.initialize_and_list_tools()
            client.send(r="options(showErrorCalls = TRUE)")
            for source in (
                "library(notloaded.pkg)",
                "base::library(notloaded.pkg)",
                'loadNamespace("notloaded.pkg")',
                'base::loadNamespace("notloaded.pkg")',
                "notloaded.pkg::missing",
                "notloaded.pkg:::missing",
                'package <- "notloaded.pkg"; library(package, character.only = TRUE)',
                "loader <- library; loader(notloaded.pkg)",
                "lookup <- function() library(notloaded.pkg); lookup()",
                'lookup <- function() loadNamespace("notloaded.pkg"); lookup()',
            ):
                send_and_compare_r_error(client, environment, source)
            return client.finish()


@requires(POSIX)
@executions(RUNTIME)
@requires(command("ir"))
def test_does_not_resolve_unreached_package_loads(
    binary: Path, execution: Execution
) -> Transcript:
    missing = "mcpconsolenotarealpackage"
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(
            Path(temporary),
            fail_requirement=missing,
        )
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        client.send(r="options(showErrorCalls = TRUE)")
        client.expect("[1] 42\n", r=f"if (FALSE) library({missing}); 42L")
        assert len(ir_run_records(record)) == baseline

        source = f"library({missing})"
        send_and_compare_r_error(client, environment, source)
        failed = len(ir_run_records(record))
        assert failed == baseline + 1

        client.expect("[1] 42\n", r="42L")
        send_and_compare_r_error(client, environment, source)
        assert len(ir_run_records(record)) == failed + 1
        return client.finish()


@requires(POSIX)
@executions(RUNTIME)
@requires(command("ir"))
def test_rejects_non_package_runtime_names_before_ir(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            invalid <- c(
              paste0("package", intToUtf8(10L)),
              "github::owner/repo",
              "https://example.com/package",
              "../local/package",
              "package@version",
              "package name"
            )
            available <- vapply(
              invalid,
              requireNamespace,
              logical(1L),
              quietly = TRUE
            )
            host_response <- .Call("mcp_console_resolve_r", invalid)
            stopifnot(
              !any(available),
              identical(host_response[[1L]], "failed"),
              identical(host_response[[2L]], "host")
            )
            42L
            """)
        client.send(r=r)
        output = last_result_text(client)
        assert output == "[1] 42\n", repr(output)
        assert len(ir_run_records(record)) == baseline
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_preserves_base_r_loading_semantics_without_resolution(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            ambient_package <- find.package("codetools")
            hidden_library <- file.path(tempdir(), "libpath-library")
            dir.create(hidden_library)
            stopifnot(file.copy(
              ambient_package,
              hidden_library,
              recursive = TRUE
            ))
            attributed_package <- structure(
              "codetools",
              LibPath = hidden_library
            )
            namespace <- loadNamespace(attributed_package)
            if (getRversion() < "4.6.0") {
              stopifnot(identical(
                normalizePath(getNamespaceInfo(namespace, "path")),
                normalizePath(file.path(hidden_library, "codetools"))
              ))
            }
            unloadNamespace("codetools")

            listing_warnings <- character()
            listing <- withCallingHandlers(
              library(),
              warning = function(w) {
                listing_warnings <<- c(listing_warnings, conditionMessage(w))
                invokeRestart("muffleWarning")
              }
            )
            expected_warnings <- if (Sys.getenv("MCP_CONSOLE_SANDBOX") == "1") {
              sprintf(
                ngettext(
                  1L,
                  "library %s contains no packages",
                  "libraries %s contain no packages"
                ),
                sQuote(.libPaths()[[1L]])
              )
            } else {
              character()
            }
            help_info <- library(help = base)
            restricted <- suppressWarnings(library(
              "fortunes",
              lib.loc = .Library,
              character.only = TRUE,
              logical.return = TRUE,
              quietly = TRUE
            ))
            partial_failed <- inherits(
              try(
                loadNamespace("fortunes", lib.loc = .Library, partial = TRUE),
                silent = TRUE
              ),
              "try-error"
            )
            package <- "methods"
            library(package, character.only = TRUE, quietly = TRUE)
            invalid <- require(
              "mcpconsole-invalid",
              character.only = TRUE,
              quietly = TRUE
            )
            stopifnot(
              inherits(listing, "libraryIQR"),
              identical(listing_warnings, expected_warnings),
              inherits(help_info, "packageInfo"),
              identical(restricted, FALSE),
              partial_failed,
              identical(invalid, FALSE),
              identical(jsonlite::fromJSON("42"), 42L)
            )
            42L
            """)
        client.send(r=r)
        output = last_result_text(client)
        assert output == "[1] 42\n", repr(output)

        # fmt: r
        r = code(r"""
            stopifnot(!"package:splines" %in% search())
            forwarded <- function(x) {
              base::library(
                splines,
                attach.required = x,
                quietly = TRUE
              )
            }
            error <- try(forwarded(), silent = TRUE)
            stopifnot(
              inherits(error, "try-error"),
              identical(
                conditionMessage(attr(error, "condition")),
                'argument "x" is missing, with no default'
              )
            )
            42L
            """)
        client.send(r=r)
        output = last_result_text(client)
        assert output == "[1] 42\n", repr(output)

        # fmt: r
        r = code(r"""
            stopifnot(!isNamespaceLoaded("stats4"))
            forwarded <- function(x) {
              base::loadNamespace("stats4", keep.source = x)
            }
            messages <- capture.output(
              error <- try(forwarded(), silent = TRUE),
              type = "message"
            )
            stopifnot(
              inherits(error, "try-error"),
              any(grepl(
                'argument "x" is missing, with no default',
                messages,
                fixed = TRUE
              ))
            )
            42L
            """)
        client.send(r=r)
        output = last_result_text(client)
        assert output == "[1] 42\n", repr(output)
        assert len(ir_run_records(record)) == baseline
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_loads_package_with_devtools(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        fixture = Path(__file__).resolve().parents[3] / "fixtures" / "load_all"
        package = directory / "package"
        shutil.copytree(fixture, package)
        environment, record = recording_ir_environment(directory)
        client = McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=package,
        )
        client.initialize_and_list_tools()
        client.expect("[prepared]", requirements={"r": ["DBI"]})
        baseline = len(ir_run_records(record))

        # fmt: r
        r = code(r"""
            stopifnot(
              !isNamespaceLoaded("devtools"),
              !isNamespaceLoaded("pkgload"),
              !isNamespaceLoaded("mcpconsoleloadall")
            )
            devtools::load_all(
              reset = TRUE,
              recompile = FALSE,
              export_all = FALSE,
              helpers = FALSE,
              quiet = TRUE
            )
            stopifnot(
              !"internal_value" %in%
                getNamespaceExports("mcpconsoleloadall")
            )
            list(
              exported = mcpconsoleloadall::exported_value(),
              internal = mcpconsoleloadall:::internal_value()
            )
            """)
        client.send(r=r)
        output = last_result_text(client)
        assert (
            output
            == """$exported
[1] 42

$internal
[1] 41

"""
        ), repr(output)
        assert len(ir_run_records(record)) == baseline
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_r_activation_failure_requires_restart_without_stopping_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        environment, record = recording_ir_environment(Path(temporary))
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        client.initialize_and_list_tools()
        client.expect(r="activation_state <- 41L; activation_pid <- Sys.getpid()")
        baseline = len(ir_run_records(record))

        # The private bridge deliberately uses the live base::.libPaths binding
        # shared with explicit preparation.
        # fmt: r
        r = code(r"""
            local({
              invisible(suppressMessages(base::trace(
                ".libPaths",
                tracer = quote(if (!missing(new)) {
                  stop("synthetic managed R activation failure")
                }),
                print = FALSE,
                where = base::baseenv()
              )))
              on.exit(invisible(suppressMessages(base::untrace(
                ".libPaths",
                where = base::baseenv()
              ))))
              package <- "fortunes"
              do.call(
                base::loadNamespace,
                list(package = package)
              )
            })
            """)
        client.send(r=r)
        assert "synthetic managed R activation failure" in last_result_text(client)
        assert len(ir_run_records(record)) == baseline + 1

        client.expect(
            "[1] 42\n",
            r=(
                "activation_state + as.integer(identical(Sys.getpid(), activation_pid))"
            ),
        )
        client.expect("[restart required]", requirements={"r": ["english"]})

        client.expect(
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
            control="restart",
        )
        client.expect(
            "[1] 42\n",
            r=(
                "package <- 'fortunes'; "
                "stopifnot(do.call(base::requireNamespace, "
                "list(package = package, quietly = TRUE))); 42L"
            ),
        )
        assert len(ir_run_records(record)) == baseline + 2
        return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_restart_discards_unactivated_r_candidate(
    binary: Path, execution: Execution
) -> Transcript:
    checkpoint_name = "automatic-r-activation-before-report"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        package = "mcprestart"
        environment, record = recording_fixture_r_environment(directory, (package,))
        environment["TMPDIR"] = temporary
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        passed = False
        try:
            client.initialize_and_list_tools()
            client.expect(r="invisible(NULL)")
            baseline = len(ir_run_records(record))

            # The live .libPaths() binding is reached after RResolved and
            # immediately before the bridge applies and reports the candidate.
            # Restart closes fd 0 after rotating the lifecycle generation, so
            # EOF releases the old worker only after its candidate is stale.
            # fmt: r
            r = code(f"""
                activation_checkpoint <- base::file.path(
                  base::tempdir(),
                  "{checkpoint_name}"
                )
                activation_gate_used <- FALSE
                invisible(suppressMessages(base::trace(
                  ".libPaths",
                  tracer = quote({{
                    if (
                      !missing(new) &&
                        base::length(new) > 0L &&
                        !activation_gate_used
                    ) {{
                      activation_gate_used <<- TRUE
                      base::stopifnot(base::file.create(activation_checkpoint))
                      gate <- base::suppressWarnings(base::file(
                        "/dev/stdin",
                        open = "rb"
                      ))
                      base::on.exit(base::close(gate), add = TRUE)
                      base::stopifnot(base::length(base::readBin(
                        gate,
                        what = "raw",
                        n = 1L
                      )) == 0L)
                    }}
                  }}),
                  print = FALSE,
                  where = base::baseenv()
                )))
                package <- "{package}"
                invisible(base::do.call(
                  base::loadNamespace,
                  list(package = package)
                ))
                """)
            evaluation = client.start_send(r=r, timeout_ms=0)
            wait_for_worker_file(directory, checkpoint_name, client)
            assert len(ir_run_records(record)) == baseline + 1

            restart = client.start_send(control="restart")
            client.receive_many([evaluation, restart])
            assert (
                without_elapsed(entry_result_text(evaluation))
                == "\n[running; poll with an empty send]"
            )
            assert entry_result_text(restart) == (
                "[active evaluation stopped by session restart request]\n"
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "[idle]"
            )
            assert len(ir_run_records(record)) == baseline + 1

            client.send(
                r=(
                    f"package <- '{package}'; "
                    "invisible(base::do.call(base::loadNamespace, "
                    "list(package = package))); 42L"
                )
            )
            output = last_result_text(client)
            assert output == "[1] 42\n", repr(output)
            assert len(ir_run_records(record)) == baseline + 2
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            if not passed:
                stop_client(client)


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_rejects_preparation_while_automatic_r_resolver_is_running(
    binary: Path,
    execution: Execution,
) -> Transcript:
    package = "mcppreparation"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_fixture_r_environment(directory, (package,))
        started = FifoCheckpoint.create(directory / "ir-started")
        release = FifoCheckpoint.create(directory / "ir-release")
        environment["MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT"] = package
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(started.path)
        environment["MCP_CONSOLE_TEST_IR_RELEASE"] = str(release.path)
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        resolver_released = False
        finished = False
        try:
            client.initialize_and_list_tools()
            client.expect("[prepared]", requirements={"r": ["DBI"]})
            baseline = len(ir_run_records(record))

            evaluation = client.start_send(
                r=(f'invisible(base::loadNamespace("{package}")); 42L'),
                timeout_ms=0,
            )
            started.wait("automatic R resolver")
            client.receive(evaluation)
            assert (
                without_elapsed(entry_result_text(evaluation))
                == "\n[running; poll with an empty send]"
            )
            preparation = client.start_send(
                requirements={"r": ["english"]},
            )
            readable, _, _ = select.select([client.stdout], [], [], 10)
            assert readable, "preparation waited for the active R resolver"
            client.receive(preparation)
            assert preparation["result"] == {
                "content": [
                    {
                        "type": "text",
                        "text": (
                            "worker is already evaluating a cell; poll it before "
                            "preparing requirements"
                        ),
                    }
                ],
                "isError": True,
            }, preparation

            poll = client.start_send()
            release.release()
            resolver_released = True
            client.receive(poll)
            assert entry_result_text(poll) == "[1] 42\n"
            evaluation["result"] = poll["result"]
            assert client.transcript.pop() is poll
            assert len(ir_run_records(record)) == baseline + 1
            transcript = client.finish()
            finished = True
            return transcript
        finally:
            if not resolver_released:
                release.release()
            started.close()
            release.close()
            if not finished:
                stop_client(client)


@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS, command("ir"))
def test_interrupts_automatic_r_resolver_and_preserves_worker(
    binary: Path,
    execution: Execution,
) -> Transcript:
    package = "RcppRoll"
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        environment, record = recording_ir_environment(directory)
        cell_release = FifoCheckpoint.create(directory / "cell-release")
        started = FifoCheckpoint.create(directory / "ir-started")
        interrupted = FifoCheckpoint.create(directory / "ir-interrupted")
        environment["MCP_CONSOLE_TEST_R_RESOLVER_RELEASE"] = str(cell_release.path)
        environment["MCP_CONSOLE_TEST_IR_BLOCK_REQUIREMENT"] = package
        environment["MCP_CONSOLE_TEST_IR_STARTED"] = str(started.path)
        environment["MCP_CONSOLE_TEST_IR_INTERRUPTED"] = str(interrupted.path)
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        passed = False
        try:
            client.initialize_and_list_tools()
            client.expect(
                r="resolver_interrupt_state <- 41L; resolver_pid <- Sys.getpid()",
            )
            baseline = len(ir_run_records(record))
            server = capture_process_identity(client.process.pid)
            owner = local_resolver_owner(server, binary)
            existing_children = child_process_identities(owner)

            # fmt: r
            r = code(r"""
                local({
                  gate <- fifo(
                    Sys.getenv("MCP_CONSOLE_TEST_R_RESOLVER_RELEASE"),
                    "rb",
                    blocking = TRUE
                  )
                  stopifnot(identical(readBin(gate, "raw", 1L), charToRaw("1")))
                  close(gate)
                })
                package <- "RcppRoll"
                do.call(base::loadNamespace, list(package = package))
                resolver_interrupt_cell_ran <- TRUE
                """)
            # Release the cell's response claim before the interrupt becomes
            # the sole reader of the resolver error and evaluation completion.
            # The resolver cannot start before this response is projected.
            client.expect("\n[running; poll with an empty send]", r=r, timeout_ms=0)
            assert "phase:" not in last_result_text(client), last_result_text(client)
            cell_release.release()
            started.wait("automatic R resolver")
            resolver = [
                child
                for child in child_process_identities(owner)
                if child not in existing_children
            ]
            assert len(resolver) == 1, resolver
            wait_for_evaluation_output(
                client,
                "Error: R package resolution interrupted\n",
                "automatic R resolver interruption",
                completion_timeout_seconds=client.response_timeout,
                control="interrupt",
            )
            interrupted.wait("automatic R resolver received SIGINT")
            assert live_processes(resolver) == [], (
                "interrupt did not reap the R resolver"
            )
            assert len(ir_run_records(record)) == baseline + 1

            client.expect(
                "[1] 42\n",
                r=(
                    "resolver_interrupt_state + "
                    "as.integer(!exists('resolver_interrupt_cell_ran')) + "
                    "as.integer(identical(Sys.getpid(), resolver_pid)) - 1L"
                ),
            )
            transcript = client.finish()
            passed = True
            return transcript
        finally:
            cell_release.release()
            cell_release.close()
            started.close()
            interrupted.close()
            if not passed:
                stop_client(client)


if __name__ == "__main__":
    run_this_suite(__file__)
