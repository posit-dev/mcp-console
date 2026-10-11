"""Installed R documentation uses ordinary, capturable console output."""

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code, normalize_trailing_spaces
from support.r import FIXTURES, r_test_environment
from support.records import Transcript
from support.requirements import R, requires
from support.resolvers import bare_runtime_environment


@contextmanager
def documentation_client(binary: Path, execution: Execution) -> Iterator[McpClient]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        library = root / "library"
        library.mkdir()
        environment = bare_runtime_environment(environment, library)
        environment["MCP_CONSOLE_LANGUAGES"] = "r"
        subprocess.run(
            [
                rscript.with_name("R.exe" if os.name == "nt" else "R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                FIXTURES / "r_documentation",
            ],
            env=environment,
            check=True,
            capture_output=True,
        )
        profile = root / ".Rprofile"
        profile.write_text(
            # fmt: r
            code(r"""
                options(
                  help_type = "html",
                  pager = function(...) stop("native pager invoked"),
                  browser = function(...) stop("browser invoked"),
                  pdfviewer = "false",
                  menu.graphics = FALSE
                )
                """),
            encoding="utf-8",
        )
        environment["R_PROFILE_USER"] = str(profile)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
            ),
            environment,
            current_directory=root,
            use_r_startup_files=True,
        ) as client:
            client.initialize_and_list_tools()
            yield client


@requires(R)
@executions(RUNTIME)
def test_help_lookup_and_capture(binary: Path, execution: Execution) -> Transcript:
    with documentation_client(binary, execution) as client:
        output = wait_for_evaluation_output(
            client,
            None,
            "installed help output",
            completion_timeout_seconds=client.response_timeout,
            r='print(help("topic", package = "consolehelp", help_type = "text"))',
        )
        assert "Console fixture help" in output, output
        assert "https://example.org/console-help" in output, output
        assert "café" in output, output
        assert "\b" not in output, output
        normalize_trailing_spaces(client)
        client.expect(output, r="?consolehelp::topic")
        normalize_trailing_spaces(client)
        client.expect(
            "help capture, aliases, search, and indexes retained\n",
            # fmt: r
            r=code(r"""
                stopifnot(
                  identical(getOption("help_type"), "text"),
                  identical(ls(all.names = TRUE), character())
                )
                page <- capture.output(print(help("topic", package = "consolehelp")))
                stopifnot(
                  length(page) > 10L,
                  identical(page, capture.output(?consolehelp::topic)),
                  identical(
                    page,
                    capture.output(print(help("topic-alias", package = "consolehelp")))
                  ),
                  any(grepl("Console fixture help", page, fixed = TRUE)),
                  any(grepl("https://example.org/console-help", page, fixed = TRUE)),
                  !any(grepl("\b", page, fixed = TRUE))
                )
                search <- capture.output(help.search(
                  "Console fixture help",
                  package = "consolehelp"
                ))
                operator <- capture.output(??consolehelp::"Console fixture help")
                index <- capture.output(help(package = "consolehelp"))
                stopifnot(
                  any(grepl("consolehelp::topic", search, fixed = TRUE)),
                  identical(search, operator),
                  any(grepl("Console fixture help", index, fixed = TRUE))
                )
                long <- capture.output(print(?stats::lm))
                stopifnot(length(long) > 120L)
                part <- capture.output(writeLines(long[80:120]))
                stopifnot(identical(part, long[80:120]))
                cat("help capture, aliases, search, and indexes retained\n")
                """),
        )
        output = wait_for_evaluation_output(
            client,
            None,
            "native ambiguous help selection",
            completion_timeout_seconds=client.response_timeout,
            # fmt: r
            r=code(r"""
                selection <- capture.output(help("mean", package = c("consolehelp", "base")))
                stopifnot(
                  any(grepl("Choose one", selection, fixed = TRUE)),
                  any(grepl("Console fixture help", selection, fixed = TRUE)),
                  any(grepl("Arithmetic Mean", selection, fixed = TRUE)),
                  any(grepl("https://example.org/console-help", selection, fixed = TRUE))
                )
                cat("native help selection retained\n")
                """),
            stdin="1\n",
        )
        assert (
            output
            == '[input requested: "Selection: "]\nnative help selection retained\n'
        ), output
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_file_show_headers_and_cleanup(
    binary: Path, execution: Execution
) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.expect(
            "headers, multiple files, and cleanup retained\n",
            # fmt: r
            r=code(r"""
                first <- tempfile()
                second <- tempfile()
                writeLines("first page", first)
                writeLines("second page", second)
                text <- capture.output(file.show(
                  first,
                  second,
                  header = c("First header", "Second header"),
                  delete.file = TRUE
                ))
                stopifnot(
                  identical(
                    text,
                    c("First header", "first page", "Second header", "second page")
                  ),
                  !file.exists(first),
                  !file.exists(second)
                )
                missing <- tempfile()
                writeLines("first page", first)
                discarded <- capture.output(
                  error <- tryCatch(
                    suppressWarnings(file.show(first, missing, delete.file = TRUE)),
                    error = identity
                  )
                )
                stopifnot(inherits(error, "error"), !file.exists(first))
                cat("headers, multiple files, and cleanup retained\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(RUNTIME)
def test_vignette_source_and_capture(binary: Path, execution: Execution) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.send(r='vignette("source", package = "consolehelp")')
        output = last_tool_text(client)
        assert "Installed source: 日本語 and €." in output, output
        assert 'stop("vignette source must not execute")' in output, output
        client.expect(
            "[done]", r='saved <- utils::vignette("source", package = "consolehelp")'
        )
        client.expect(
            "qualified vignette lookup, capture, and return value retained\n",
            # fmt: r
            r=code(r"""
                stopifnot(inherits(saved, "vignette"))
                source <- readLines(file.path(saved$Dir, "doc", saved$File), encoding = "UTF-8")
                stopifnot(
                  identical(capture.output(print(saved)), source),
                  identical(
                    capture.output(vignette("source", package = "consolehelp")),
                    source
                  ),
                  identical(capture.output(utils::vignette(consolehelp::source)), source),
                  identical(capture.output(vignette(consolehelp::source)), source)
                )
                printed <- capture.output(returned <- withVisible(print(saved)))
                stopifnot(identical(returned$value, saved), !returned$visible)
                listing <- capture.output(vignette(package = "consolehelp"))
                stopifnot(any(grepl("Console fixture vignette", listing, fixed = TRUE)))
                grid_source <- utils::vignette("grid", package = "grid")
                stopifnot(identical(
                  capture.output(print(grid_source)),
                  readLines(
                    file.path(grid_source$Dir, "doc", grid_source$File),
                    encoding = "UTF-8"
                  )
                ))
                local({
                  previous <- Sys.getlocale("LC_CTYPE")
                  on.exit(Sys.setlocale("LC_CTYPE", previous))
                  stopifnot(identical(Sys.setlocale("LC_CTYPE", "C"), "C"))
                  text <- capture.output(print(saved))
                  stopifnot(identical(
                    charToRaw(paste(text, collapse = "\n")),
                    charToRaw(paste(source, collapse = "\n"))
                  ))
                })
                cat("qualified vignette lookup, capture, and return value retained\n")
                """),
        )
        client.expect(
            "missing installed source fails clearly\n",
            # fmt: r
            r=code(r"""
                unlink(file.path(saved$Dir, "doc", saved$File))
                error <- tryCatch(print(saved), error = identity)
                stopifnot(
                  inherits(error, "error"),
                  grepl(
                    "Installed vignette source is unavailable",
                    conditionMessage(error),
                    fixed = TRUE
                  )
                )
                cat("missing installed source fails clearly\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_documentation_startup_restart_and_overrides(
    binary: Path, execution: Execution
) -> Transcript:
    with documentation_client(binary, execution) as client:
        # fmt: r
        check = code(r"""
            stopifnot(identical(ls(all.names = TRUE), character()))
            stopifnot(length(capture.output(?consolehelp::topic)) > 10L)
            cat("documentation defaults installed after native profiles\n")
            """)
        client.expect(
            "documentation defaults installed after native profiles\n", r=check
        )
        client.expect(
            "[done]",
            # fmt: r
            r=code(r"""
                options(pager = function(...) cat("user pager\n"), help_type = "text")
                """),
        )
        client.expect("user pager\n", r='help("topic", package = "consolehelp")')
        client.expect("user pager\n", r='vignette("source", package = "consolehelp")')
        client.expect(
            '[1] "user pager"\n',
            r="capture.output(utils::vignette(consolehelp::source))",
        )
        client.expect(
            "[done]",
            # fmt: r
            r=code(r"""
                registerS3method(
                  "print",
                  "vignette",
                  function(x, ...) cat("user vignette\n"),
                  envir = baseenv()
                )
                """),
        )
        client.expect(
            "user vignette\n", r='vignette("source", package = "consolehelp")'
        )
        client.expect(
            '[1] "user vignette"\n',
            r="capture.output(utils::vignette(consolehelp::source))",
        )
        client.expect("user pager\n", r='help("topic", package = "consolehelp")')
        client.send(control="restart")
        client.expect(
            "documentation defaults installed after native profiles\n", r=check
        )
        return client.finish()
