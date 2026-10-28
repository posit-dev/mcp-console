#!/usr/bin/env -S uv run --script

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, requires
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import isolated_r_home, r_test_environment
from support.records import Transcript
from support.snapshots import platform_snapshots
from support.suites import run_this_suite


def native_r_launchers(source: str) -> str:
    if os.name != "nt":
        return source
    for name in ("R", "Rscript"):
        source = source.replace(
            f'file.path(R.home("bin"), "{name}")',
            f'normalizePath(file.path(R.home(), "bin", "{name}.exe"), winslash = "/")',
        )
    for argument in ("commandArgs()[1L]", "arguments[[1L]]"):
        source = re.sub(
            rf"(?<![\w$]){re.escape(argument)}",
            lambda match: f'normalizePath({match[0]}, winslash = "/")',
            source,
        )
    source = source.replace(
        'stopifnot(is.null(attr(output, "status")), identical(output, character()))',
        'if (!is.null(attr(output, "status"))) stop(paste(output, collapse = "\\n"))\n'
        "  stopifnot(identical(output, character()))",
    )
    source = source.replace(
        'gsub(" ", "~+~", script, fixed = TRUE)',
        "script",
    )
    # The stock Windows launcher can shorten R_HOME to its 8.3 spelling.
    source = source.replace(
        "sub(R.home(),",
        'sub(normalizePath(R.home(), winslash = "/"),',
    )
    for paths in (
        "child$arguments[[1L]]",
        "reference$arguments[[1L]]",
        "child$environment[environment_names]",
        "Sys.getenv(environment_names)",
    ):
        source = source.replace(paths, f'normalizePath({paths}, winslash = "/")')
    # Windows R.exe delegates through cmd.exe's ANSI argument path. Keep this
    # native-launcher fixture ASCII; shared cell tests exercise UTF-8 R source.
    source = source.replace("λ", "ascii")
    return 'invisible(Sys.setlocale("LC_CTYPE", ".UTF-8"))\n' + source.replace(
        'basename(command) == "Rscript"', 'basename(command) == "Rscript.exe"'
    )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_uses_selected_r_resource_directories(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        selected = isolated_r_home(root, environment)
        launcher = selected / "bin/R"
        source = launcher.read_text()
        resources = subprocess.check_output(
            [
                launcher,
                "--vanilla",
                "--slave",
                "-e",
                'cat(vapply(c("share", "include", "doc"), R.home, ""), sep="\\n")',
            ],
            env=environment,
            text=True,
        ).splitlines()
        for (name, suffix), resource in zip(
            (
                ("R_SHARE_DIR", "share"),
                ("R_INCLUDE_DIR", "include"),
                ("R_DOC_DIR", "doc"),
            ),
            resources,
            strict=True,
        ):
            configured = root / f"configured {suffix} λ"
            configured.symlink_to(Path(resource).resolve())
            (selected / suffix).unlink(missing_ok=True)
            source, count = re.subn(
                rf"(?m)^{name}=.*$", f'{name}="{configured}"', source
            )
            assert count == 1, name
            # The selected launcher must supply its own paths, not inherited ones.
            environment[name] = str(root / f"stale-{suffix}")
        launcher.write_text(source)
        environment["RETICULATE_PYTHON"] = sys.executable
        # fmt: r
        r = code(r"""
            names <- c("R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR")
            directories <- Sys.getenv(names)
            stopifnot(
              identical(Sys.getenv("R_HOME"), R.home()),
              all(dir.exists(directories)),
              identical(
                unname(directories),
                vapply(
                  c("share", "include", "doc"),
                  R.home,
                  "",
                  USE.NAMES = FALSE
                )
              ),
              file.exists(file.path(R.home("include"), "R.h")),
              length(readLines(file.path(R.home("doc"), "AUTHORS"))) > 0L
            )
            child <- system2(
              commandArgs()[1L],
              c(
                "--vanilla",
                "--slave",
                "-e",
                shQuote(
                  'cat(Sys.getenv(c("R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR")), sep = "\n")'
                )
              ),
              stdout = TRUE,
              stderr = TRUE
            )
            stopifnot(is.null(attr(child, "status")), identical(child, unname(directories)))
            cat("R and its children use the selected resource directories\n")
            """)
        reference = subprocess.check_output(
            [launcher, "--vanilla", "--slave", "-e", r],
            env=environment,
            text=True,
        )
        expected = "R and its children use the selected resource directories\n"
        assert reference == expected, reference
        # Hidden R remains a bridge capability, but native startup waits until
        # Python uses that bridge. This exercises resource capture before R runs.
        environment["MCP_CONSOLE_LANGUAGES"] = "python,sql"
        with McpClient(
            binary, execution.serve("-c", "languages=[r,python,sql]"), environment
        ) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                "Python changed R paths before R initialization\n",
                "Python initialization before R resource validation",
                completion_timeout_seconds=600,
                # fmt: python
                python=code("""
                    import ctypes
                    import os

                    try:
                        initialized = bool(ctypes.c_void_p.in_dll(ctypes.CDLL(None), "R_GlobalEnv").value)
                    except ValueError:
                        initialized = False
                    assert not initialized, "R initialized before Python demand"
                    r_paths = {
                        name: os.environ[name]
                        for name in ("R_HOME", "R_SHARE_DIR", "R_INCLUDE_DIR", "R_DOC_DIR")
                    }
                    os.environ.pop("R_HOME")
                    os.environ["R_SHARE_DIR"] = "/missing-r-share"
                    os.environ.pop("R_INCLUDE_DIR")
                    os.environ["R_DOC_DIR"] = "/missing-r-doc"
                    print("Python changed R paths before R initialization")
                    """),
            )
            assert last_tool_text(client) == (
                "Python changed R paths before R initialization\n"
            ), last_tool_text(client)
            wait_for_evaluation_output(
                client,
                expected,
                "R resource-directory validation",
                completion_timeout_seconds=600,
                python=f"_ = r[{r!r}]",
            )
            assert last_tool_text(client) == expected, last_tool_text(client)
            wait_for_evaluation_output(
                client,
                "[done]",
                "restored native R environment",
                # fmt: python
                python=code("""
                    native = ctypes.CDLL(None)
                    native.getenv.argtypes = [ctypes.c_char_p]
                    native.getenv.restype = ctypes.c_char_p
                    assert all(
                        native.getenv(name.encode()) == os.fsencode(path) for name, path in r_paths.items()
                    )
                    """),
            )
            assert last_tool_text(client) == "[done]", last_tool_text(client)
            return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_uses_selected_r_launcher_default_architecture(
    binary: Path, execution: Execution
) -> Transcript:
    environment, rscript = r_test_environment()
    original_home = Path(environment["R_HOME"])
    underlying = subprocess.check_output(
        [rscript.parent / "R", "--vanilla", "--slave", "-e", "cat(commandArgs()[1L])"],
        env=environment,
        text=True,
    ).strip()
    with tempfile.TemporaryDirectory() as directory:
        selected = isolated_r_home(Path(directory), environment)
        launcher = selected / "bin/R"
        source, substitutions = re.subn(
            r"(?m)^: \$\{R_ARCH=.*\}$",
            ": ${R_ARCH=/identity-test}",
            launcher.read_text(),
        )
        assert substitutions == 1
        launcher.write_text(source)
        # Only the configured architecture has an executable in this installation.
        (selected / "bin/exec").unlink()
        architecture = selected / "bin/exec/identity-test"
        architecture.mkdir(parents=True)
        (architecture / "R").symlink_to(underlying)
        (selected / "etc/identity-test").symlink_to(".")
        (selected / "lib/identity-test").symlink_to(original_home / "lib")
        environment.pop("R_ARCH", None)
        # Prove that the selected stock launcher supplies the configured default.
        reference = subprocess.check_output(
            [launcher, "--vanilla", "--slave", "-e", 'cat(Sys.getenv("R_ARCH"))'],
            env=environment,
            text=True,
        )
        assert reference == "/identity-test", reference
        with McpClient(binary, execution.serve(), environment) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                "Selected R launcher supplies its default architecture\n",
                "R launcher architecture validation",
                completion_timeout_seconds=600,
                # fmt: r
                r=code(r"""
                    executable <- commandArgs()[1L]
                    stopifnot(
                      file.exists(executable),
                      identical(executable, file.path(R.home("bin"), "R")),
                      interactive(),
                      is.na(Sys.getenv("R_ARCH", unset = NA_character_))
                    )
                    child <- system2(
                      executable,
                      c("--vanilla", "--slave", "-e", shQuote('cat(Sys.getenv("R_ARCH"))')),
                      stdout = TRUE,
                      stderr = TRUE
                    )
                    stopifnot(
                      is.null(attr(child, "status")),
                      identical(child, "/identity-test")
                    )
                    cat("Selected R launcher supplies its default architecture\n")
                    """),
            )
            assert last_tool_text(client) == (
                "Selected R launcher supplies its default architecture\n"
            ), last_tool_text(client)
            return client.finish()


@platform_snapshots(
    "win32",
    reason="Native R executable identity and launcher arguments differ on Windows",
)
@executions(DIRECT, SANDBOXED)
def test_shows_interactive_interpreter_identity(
    binary: Path, execution: Execution
) -> Transcript:
    with (
        tempfile.TemporaryDirectory() as directory,
        McpClient(
            binary, execution.serve(), current_directory=Path(directory)
        ) as client,
    ):
        (Path(directory) / "identity.py").write_text(
            # fmt: python
            code(r"""
                import sys

                print("script sys.argv:", sys.argv)
                print("script sys.argv[0] == sys.executable:", sys.argv[0] == sys.executable)
                """)
        )
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
            sub(R.home(), "<R_HOME>", commandArgs()[1L], fixed = TRUE)
            identical(commandArgs()[1L], file.path(R.home("bin"), "R"))
            interactive()
            """)
        client.send(r=native_r_launchers(r))
        launcher = "R.exe" if os.name == "nt" else "R"
        assert last_tool_text(client) == (
            f'[1] "<R_HOME>/bin/{launcher}"\n[1] TRUE\n[1] TRUE\n'
        ), last_tool_text(client)
        client.send(
            # fmt: python
            python=code(r"""
                import subprocess
                import sys
                from pathlib import Path

                print("sys.executable is a file:", Path(sys.executable).is_file())
                print("sys.argv:", sys.argv)
                print("sys.orig_argv == [sys.executable]:", sys.orig_argv == [sys.executable])
                print("sys.argv[0] == sys.executable:", sys.argv[0] == sys.executable)
                child = subprocess.run(
                    [sys.executable, "identity.py", "two words"],
                    stdout=subprocess.PIPE,
                    text=True,
                )
                sys.stdout.write(child.stdout)
                child.check_returncode()
                """),
        )
        assert last_tool_text(client).replace("\r\n", "\n") == (
            "sys.executable is a file: True\n"
            "sys.argv: ['']\n"
            "sys.orig_argv == [sys.executable]: True\n"
            "sys.argv[0] == sys.executable: False\n"
            "script sys.argv: ['identity.py', 'two words']\n"
            "script sys.argv[0] == sys.executable: False\n"
        ), last_tool_text(client)
        return client.finish()


@platform_snapshots(
    "win32", reason="Native R child launchers and argument escaping differ on Windows"
)
@executions(DIRECT, SANDBOXED)
def test_launches_r_children_from_interpreter_identity(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        # fmt: r
        r = code(r"""
                arguments <- commandArgs()
                stopifnot(
                  file.exists(arguments[[1L]]),
                  identical(arguments[[1L]], file.path(R.home("bin"), "R")),
                  interactive(),
                  identical(arguments[-1L], c("--quiet", "--interactive", "--no-save")),
                  identical(commandArgs(TRUE), character())
                )
                environment_names <- c(
                  "R_HOME",
                  "R_SHARE_DIR",
                  "R_INCLUDE_DIR",
                  "R_DOC_DIR"
                )
                script <- tempfile("identity λ ", fileext = ".R")
                result <- tempfile("identity result λ ", fileext = ".rds")
                writeLines(
                  c(
                    "arguments <- commandArgs()",
                    "saveRDS(list(arguments = arguments, user = commandArgs(TRUE),",
                    "  interactive = interactive(),",
                    "  environment = Sys.getenv(c('R_HOME', 'R_ARCH', 'R_SHARE_DIR',",
                    "    'R_INCLUDE_DIR', 'R_DOC_DIR'))), commandArgs(TRUE)[[1L]])"
                  ),
                  script,
                  useBytes = TRUE
                )
                user <- c(result, "two words", "λ", "--literal")
                reference <- NULL
                for (command in c(
                  file.path(R.home("bin"), "R"),
                  file.path(R.home("bin"), "Rscript"),
                  arguments[[1L]]
                )) {
                  options <- if (basename(command) == "Rscript") {
                    c("--vanilla", script)
                  } else {
                    c("--slave", "--vanilla", paste0("--file=", script), "--args")
                  }
                  output <- system2(
                    command,
                    shQuote(c(options, user)),
                    stdout = TRUE,
                    stderr = TRUE
                  )
                  stopifnot(is.null(attr(output, "status")), identical(output, character()))
                  child <- readRDS(result)
                  if (is.null(reference)) {
                    reference <- child
                  }
                  # The selected launcher supplies the native executable and architecture.
                  file_argument <- paste0("--file=", gsub(" ", "~+~", script, fixed = TRUE))
                  expected <- if (basename(command) == "Rscript") {
                    c("--no-echo", "--no-restore", "--vanilla", file_argument, "--args", user)
                  } else {
                    c("--slave", "--vanilla", file_argument, "--args", user)
                  }
                  stopifnot(
                    file.exists(child$arguments[[1L]]),
                    identical(child$arguments[[1L]], reference$arguments[[1L]]),
                    identical(child$arguments[-1L], expected),
                    identical(child$user, user),
                    identical(child$interactive, FALSE),
                    identical(
                      child$environment[environment_names],
                      Sys.getenv(environment_names)
                    ),
                    identical(child$environment[["R_ARCH"]], reference$environment[["R_ARCH"]])
                  )
                }
                unlink(c(script, result))
                cat(
                  "R interpreter and children retain executable, arguments, and environment\n"
                )
                """)
        client.expect(
            "R interpreter and children retain executable, arguments, and environment\n",
            r=native_r_launchers(r),
        )
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_detects_cpu_cores(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        cores <- parallel::detectCores()
        stopifnot(
          length(cores) == 1L,
          !is.na(cores),
          cores >= 1L
        )
        writeLines("R core detection available")
        """)
    client.send(r=r)
    return client.finish()


@executions(RUNTIME)
def test_rejects_incomplete_and_invalid_source(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="answer <- 41")
    # fmt: r
    r = code(r"""
        answer <- 42
        created <- TRUE
        answer + (
        """)
    client.send(r=r)
    assert "unexpected end of input" in last_tool_text(client), last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    # fmt: r
    unchanged = code(r"""
        stopifnot(
          identical(answer, 41),
          identical(base::.Last.value, 41),
          !exists("created", envir = globalenv(), inherits = FALSE)
        )
        answer
        """)
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    client.send(r=")")
    assert "unexpected ')'" in last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    # fmt: r
    r = code(r"""
        answer <- 43
        created <- TRUE
        )
        """)
    client.send(r=r)
    assert "unexpected ')'" in last_tool_text(client)
    assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    # This parser failure raises an R condition instead of returning PARSE_ERROR.
    # fmt: r
    r = code(r"""
        answer <- 44
        created <- TRUE
        function(x, x) x
        """)
    client.send(r=r)
    assert "repeated formal argument 'x'" in last_tool_text(client)
    assert client.transcript[-1]["result"]["isError"] is False
    client.send(r=unchanged)
    assert last_tool_text(client) == "[1] 41\n"
    # A runtime error still preserves expressions evaluated before it.
    # fmt: r
    r = code(r"""
        answer <- 45
        stop("runtime failure")
        answer <- 46
        """)
    client.send(r=r)
    assert last_tool_text(client) == "Error: runtime failure\n"
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 45\n"
    return client.finish()


@executions(RUNTIME)
def test_rejects_source_without_error_side_effects(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # Seed a real traceback and verify the error hook still handles runtime errors.
    # fmt: r
    r = code(r"""
        error_hook_calls <- 0L
        options(error = quote({
          error_hook_calls <<- error_hook_calls + 1L
          traceback()
        }))
        f <- function() stop("seed traceback")
        f()
        """)
    client.send(r=r)
    client.send(r="traceback()")
    previous_traceback = last_tool_text(client)
    assert 'stop("seed traceback")' in previous_traceback
    # fmt: r
    incomplete = code(r"""
        created <- TRUE
        (
        """)
    # fmt: r
    invalid = code(r"""
        created <- TRUE
        )
        """)
    # fmt: r
    duplicate_formal = code(r"""
        created <- TRUE
        function(x, x) x
        """)
    for source in (incomplete, invalid, duplicate_formal):
        client.send(r=source)
        assert last_tool_text(client).startswith("Error: "), last_tool_text(client)
        assert 'stop("seed traceback")' not in last_tool_text(client), last_tool_text(
            client
        )
        client.send(r="traceback()")
        assert last_tool_text(client) == previous_traceback, last_tool_text(client)
    # fmt: r
    r = code(r"""
        stopifnot(
          error_hook_calls == 1L,
          !exists("created", envir = globalenv(), inherits = FALSE)
        )
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]", last_tool_text(client)
    return client.finish()


@executions(RUNTIME)
def test_preserves_parser_warning_behavior(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    client.send(r="invisible(1.0L)")
    assert last_tool_text(client).count("unnecessary decimal point") == 1
    # fmt: r
    r = code(r"""
        options(warning.expression = quote(cat("parser warning\n")))
        """)
    client.send(r=r)
    client.send(r="invisible(1.0L)")
    assert last_tool_text(client) == "parser warning\n", last_tool_text(client)
    # fmt: r
    r = code(r"""
        options(
          warning.expression = NULL,
          warn = 2,
          error = quote(cat("error handler warn: ", getOption("warn"), "\n", sep = ""))
        )
        """)
    client.send(r=r)
    # fmt: r
    r = code(r"""
        function(x, x) x
        """)
    client.send(r=r)
    assert "repeated formal argument 'x'" in last_tool_text(client)
    assert "error handler warn:" not in last_tool_text(client)
    # The warning becomes an error only when the native REPL reaches the literal.
    # fmt: r
    r = code(r"""
        answer <- 42
        1.0L
        """)
    client.send(r=r)
    assert "(converted from warning)" in last_tool_text(client)
    assert last_tool_text(client).endswith("error handler warn: 2\n")
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


@executions(RUNTIME)
def test_preserves_parser_warning_handlers(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # The native REPL's R_ToplevelExec isolates condition handlers between cells.
    # Register the handler in the cell where the native parser will warn.
    # fmt: r
    r = code(r"""
        parser_warnings <- 0L
        globalCallingHandlers(warning = function(w) {
          parser_warnings <<- parser_warnings + 1L
          cat("global warning handler\n")
          invokeRestart("muffleWarning")
        })
        invisible(1.0L)
        """)
    client.send(r=r)
    assert last_tool_text(client) == "global warning handler\n", last_tool_text(client)
    client.send(r="parser_warnings")
    assert last_tool_text(client) == "[1] 1\n"
    # An error from the handler must occur after the preceding assignment.
    # fmt: r
    r = code(r"""
        globalCallingHandlers(NULL)
        globalCallingHandlers(warning = function(w) {
          stop("global warning handler", call. = FALSE)
        })
        answer <- 42
        1.0L
        """)
    client.send(r=r)
    assert last_tool_text(client) == "Error: global warning handler\n"
    client.send(r="answer")
    assert last_tool_text(client) == "[1] 42\n"
    return client.finish()


@executions(RUNTIME)
def test_runs_native_top_level_bookkeeping(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        invisible(addTaskCallback(
          local({
            first <- TRUE
            function(expr, ...) {
              if (first) {
                first <<- FALSE
                return(TRUE)
              }
              cat(deparse1(expr), "\n", sep = "")
              FALSE
            }
          }),
          name = "mcp-console-test"
        ))
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    client.send(r="# No expression to evaluate.")
    assert last_tool_text(client) == "[done]"
    # Neither preflight nor a rejected cell may consume the registered callback.
    # fmt: r
    r = code(r"""
        mcp_console_callback_probe <- 99
        (
        """)
    client.send(r=r)
    assert "unexpected end of input" in last_tool_text(client), last_tool_text(client)
    client.send(r="mcp_console_callback_probe <- 42")
    assert last_tool_text(client) == "mcp_console_callback_probe <- 42\n"
    # fmt: r
    r = code(r"""
        warning("careful", call. = FALSE)
        invisible(42)
        cat("last value: ", identical(base::.Last.value, 42), "\n", sep = "")
        """)
    client.send(r=r)
    return client.finish()


@executions(RUNTIME)
def test_preserves_native_stack_and_last_value_binding(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # The private parser must not resolve helpers from the user's workspace.
    # fmt: r
    r = code(r"""
        str2expression <- suppressWarnings <- function(...) stop("masked parser")
        """)
    client.send(r=r)
    assert last_tool_text(client) == "[done]"
    # fmt: r
    r = code(r"""
        user_calls <- function() {
          vapply(sys.calls(), deparse1, character(1))
        }
        calls <- user_calls()
        cat("contains user call: ", "user_calls()" %in% calls, "\n", sep = "")
        cat(
          "contains internal call: ",
          any(grepl("mcp_console|base::get", calls)),
          "\n",
          sep = ""
        )
        cat(
          "global binding: ",
          exists(".Last.value", envir = globalenv(), inherits = FALSE),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
