"""Installed R documentation uses ordinary, capturable console output."""

import os
import re
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
from support.requirements import NON_UTF8_FILENAMES, R, requires
from support.resolvers import bare_runtime_environment


@contextmanager
def documentation_client(
    binary: Path,
    execution: Execution,
    *,
    packages: tuple[Path, ...] = (),
    locale: str | None = None,
) -> Iterator[McpClient]:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        environment, rscript = r_test_environment()
        if locale is not None:
            environment["LC_ALL"] = locale
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
                *packages,
            ],
            env=environment,
            check=True,
            capture_output=True,
        )
        subprocess.run(
            [rscript, FIXTURES / "r_documentation_metadata.R", library],
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
                  pdfviewer = function(...) stop("PDF viewer invoked"),
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


def normalize_documentation_preview(client: McpClient) -> None:
    output = normalize_trailing_spaces(client)
    path = re.search(
        r"raw log: (\.agents[/\\]console[/\\]sessions[/\\][^/\\]+[/\\]outputs[/\\]call-\d+\.log)",
        output,
    )
    assert path is not None, output
    parts = path[1].replace("\\", "/").split("/")
    parts[3] = "<run ID>"
    client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
        path[1], "/".join(parts)
    )


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_help_workflow(binary: Path, execution: Execution) -> Transcript:
    # Match CI's locale for native help typography and omission byte counts.
    with documentation_client(binary, execution, locale="C") as client:
        client.send(r="?stats::lm")
        output = last_tool_text(client)
        assert "Fitting Linear Models" in output, output
        assert "lm(formula" in output, output
        normalize_documentation_preview(client)
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_vignette_workflow(binary: Path, execution: Execution) -> Transcript:
    with documentation_client(
        binary, execution, packages=(FIXTURES / "cpp11_0.5.2.tar.gz",), locale="C"
    ) as client:
        client.send(r='vignette(package = "cpp11")')
        output = last_tool_text(client)
        assert "Converting from Rcpp" in output, output
        normalize_trailing_spaces(client)
        client.send(r='vignette("converting", package = "cpp11")')
        output = last_tool_text(client)
        assert "# Converting from Rcpp" in output, output
        assert "## Getting started" in output, output
        assert "Vignette source:" not in output, output
        assert "NumericVector" in output, output
        assert "writable::doubles" in output, output
        assert (
            "https://cpp11.r-lib.org/articles/motivations.html#growing-vectors"
            in output
        )
        assert "https://youtu.be/KKeucpfAuuA" in output, output
        assert "[1]:" in output, output
        normalize_documentation_preview(client)
        return client.finish()


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
@executions(DIRECT, SANDBOXED)
def test_vignette_rendered_html(binary: Path, execution: Execution) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.send(r='vignette("rendered", package = "consolehelp")')
        output = last_tool_text(client)
        assert "42 — evaluated result" in output, output
        assert "Source-only prose" not in output, output
        assert "SENTINEL" not in output, output
        normalize_trailing_spaces(client)
        client.expect(
            "[done]", r='saved <- utils::vignette("rendered", package = "consolehelp")'
        )
        client.expect(
            "rendered structure, capture, metadata, and return value retained\n",
            # fmt: r
            r=code(r"""
                stopifnot(saved$PDF == "evaluated.HTML", saved$File == "rendered.Rnw")
                text <- capture.output(print(saved))
                stopifnot(
                  any(grepl("# Rendered results", text, fixed = TRUE)),
                  any(grepl("## Steps", text, fixed = TRUE)),
                  any(grepl("café, 日本語, and €", text, fixed = TRUE)),
                  any(text == ""),
                  any(grepl("* Read installed HTML", text, fixed = TRUE)),
                  any(grepl("1. First step", text, fixed = TRUE)),
                  any(grepl("  sum(c(20, 22))", text, fixed = TRUE)),
                  any(grepl("42 — evaluated result", text, fixed = TRUE)),
                  any(grepl("Evaluated totals", text, fixed = TRUE)),
                  any(grepl("alpha", text, fixed = TRUE) & grepl("42", text, fixed = TRUE)),
                  any(grepl("https://example.org/vignette?x=1&y=2", text, fixed = TRUE)),
                  any(grepl("other.html#details", text, fixed = TRUE)),
                  any(grepl("A rising trend", text, fixed = TRUE)),
                  any(grepl("Figure 1: evaluated trend.", text, fixed = TRUE)),
                  any(grepl("Remote figure description", text, fixed = TRUE)),
                  !any(grepl("SENTINEL", text, fixed = TRUE)),
                  identical(
                    text,
                    capture.output(vignette("rendered", package = "consolehelp"))
                  ),
                  identical(text, capture.output(utils::vignette(consolehelp::rendered))),
                  identical(text, capture.output(vignette(consolehelp::rendered)))
                )
                printed <- capture.output(returned <- withVisible(print(saved)))
                stopifnot(identical(returned$value, saved), !returned$visible)
                stopifnot(identical(capture.output(writeLines(text[1:5])), text[1:5]))
                listing <- capture.output(vignette(package = "consolehelp"))
                stopifnot(any(grepl("Console rendered vignette", listing, fixed = TRUE)))
                local({
                  previous <- Sys.getlocale("LC_CTYPE")
                  on.exit(Sys.setlocale("LC_CTYPE", previous))
                  stopifnot(identical(Sys.setlocale("LC_CTYPE", "C"), "C"))
                  captured <- capture.output(print(saved))
                  stopifnot(identical(
                    charToRaw(paste(captured, collapse = "\n")),
                    charToRaw(paste(text, collapse = "\n"))
                  ))
                  library <- enc2utf8("library-café")
                  stopifnot(identical(Encoding(library), "UTF-8"))
                  # Under C, Unix filenames use native bytes; Windows APIs use Unicode.
                  if (.Platform$OS.type == "unix") {
                    Encoding(library) <- "unknown"
                  }
                  stopifnot(dir.create(library))
                  stopifnot(file.copy(saved$Dir, library, recursive = TRUE))
                  native <- utils::vignette(
                    "rendered",
                    package = "consolehelp",
                    lib.loc = library
                  )
                  native_text <- capture.output(print(native))
                  stopifnot(identical(
                    charToRaw(paste(native_text, collapse = "\n")),
                    charToRaw(paste(text, collapse = "\n"))
                  ))
                })
                cat("rendered structure, capture, metadata, and return value retained\n")
                """),
        )
        client.expect(
            "HTML conversion errors do not switch to source\n",
            # fmt: r
            r=code(r"""
                html <- file.path(saved$Dir, "doc", saved$PDF)
                unlink(file.path(saved$Dir, "doc", saved$File))
                stopifnot(identical(capture.output(print(saved)), text))
                writeBin(as.raw(c(255, 254)), html)
                failed <- capture.output(error <- tryCatch(print(saved), error = identity))
                stopifnot(
                  inherits(error, "error"),
                  grepl(
                    "Unable to convert installed HTML documentation",
                    conditionMessage(error),
                    fixed = TRUE
                  ),
                  length(failed) == 0L
                )
                unlink(html)
                error <- tryCatch(print(saved), error = identity)
                stopifnot(inherits(error, "error"))
                cat("HTML conversion errors do not switch to source\n")
                """),
        )
        return client.finish()


@requires(NON_UTF8_FILENAMES, R)
@executions(DIRECT, SANDBOXED)
def test_vignette_non_utf8_library_path(
    binary: Path, execution: Execution
) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.expect(
            "rendered vignettes preserve native filename bytes\n",
            # fmt: r
            r=code(r"""
                local({
                  previous <- Sys.getlocale("LC_CTYPE")
                  on.exit(Sys.setlocale("LC_CTYPE", previous))
                  stopifnot(identical(Sys.setlocale("LC_CTYPE", "C"), "C"))
                  original <- utils::vignette("rendered", package = "consolehelp")
                  text <- capture.output(print(original))
                  library <- rawToChar(c(charToRaw("library-"), as.raw(255)))
                  stopifnot(dir.create(library))
                  stopifnot(file.copy(original$Dir, library, recursive = TRUE))
                  saved <- utils::vignette(
                    "rendered",
                    package = "consolehelp",
                    lib.loc = library
                  )
                  stopifnot(as.raw(255) %in% charToRaw(saved$Dir))
                  stopifnot(identical(capture.output(print(saved)), text))
                  cat("rendered vignettes preserve native filename bytes\n")
                })
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
                source <- c(
                  paste("Vignette source:", saved$File),
                  readLines(file.path(saved$Dir, "doc", saved$File), encoding = "UTF-8")
                )
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
                  c(
                    paste("Vignette source:", grid_source$File),
                    readLines(
                      file.path(grid_source$Dir, "doc", grid_source$File),
                      encoding = "UTF-8"
                    )
                  )
                ))
                for (topic in c("pdf", "markdown")) {
                  other <- utils::vignette(topic, package = "consolehelp")
                  stopifnot(nzchar(other$PDF), !grepl("html$", other$PDF))
                  stopifnot(identical(
                    capture.output(print(other)),
                    c(
                      paste("Vignette source:", other$File),
                      readLines(file.path(other$Dir, "doc", other$File), encoding = "UTF-8")
                    )
                  ))
                }
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
def test_vignette_deferred_pager(binary: Path, execution: Execution) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.expect(
            "HTML remains available for a deferred pager\n",
            # fmt: r
            r=code(r"""
                options(pager = function(files, header, title, delete.file) {
                  pending <<- list(files = files, delete.file = delete.file)
                })
                vignette("rendered", package = "consolehelp")
                stopifnot(file.exists(pending$files), pending$delete.file)
                cat("HTML remains available for a deferred pager\n")
                """),
        )
        client.expect(
            "deferred pager reads evaluated results and owns cleanup\n",
            # fmt: r
            r=code(r"""
                text <- readLines(pending$files, encoding = "UTF-8")
                stopifnot(any(grepl("42 — evaluated result", text, fixed = TRUE)))
                if (pending$delete.file) {
                  unlink(pending$files)
                }
                stopifnot(!file.exists(pending$files))
                options(pager = function(files, header, title, delete.file) {
                  failed <<- files
                  stop("pager failed")
                })
                error <- tryCatch(
                  print(vignette("rendered", package = "consolehelp")),
                  error = identity
                )
                stopifnot(
                  identical(conditionMessage(error), "pager failed"),
                  !file.exists(failed)
                )
                cat("deferred pager reads evaluated results and owns cleanup\n")
                """),
        )
        return client.finish()


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_vignette_custom_pager_encoding(
    binary: Path, execution: Execution
) -> Transcript:
    with documentation_client(binary, execution) as client:
        client.expect(
            "custom pagers receive the standard native encoding\n",
            # fmt: r
            r=code(r"""
                local({
                  saved <- utils::vignette("rendered", package = "consolehelp")
                  text <- capture.output(print(saved))
                  previous <- Sys.getlocale("LC_CTYPE")
                  on.exit(Sys.setlocale("LC_CTYPE", previous))
                  stopifnot(identical(Sys.setlocale("LC_CTYPE", "C"), "C"))
                  reference <- tempfile()
                  writeBin(charToRaw(paste(text, collapse = "\n")), reference)
                  selected <- function(files, header, title, delete.file) {
                    bytes <<- readBin(files, "raw", n = file.info(files)$size)
                    if (delete.file) unlink(files)
                  }
                  file.show(reference, pager = selected, encoding = "UTF-8", delete.file = TRUE)
                  expected <- bytes
                  options(pager = selected)
                  print(saved)
                  stopifnot(identical(bytes, expected))
                  options(pager = function(files, header, title, delete.file) {
                    failed <<- files
                    stop("pager failed")
                  })
                  error <- tryCatch(print(saved), error = identity)
                  stopifnot(
                    identical(conditionMessage(error), "pager failed"),
                    !file.exists(failed)
                  )
                  cat("custom pagers receive the standard native encoding\n")
                })
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
            browser_error <- tryCatch(getOption("browser")(), error = identity)
            stopifnot(identical(conditionMessage(browser_error), "browser invoked"))
            rm(browser_error)
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
        client.expect("user pager\n", r='vignette("rendered", package = "consolehelp")')
        client.expect(
            '[1] "user pager"\n',
            r="capture.output(utils::vignette(consolehelp::source))",
        )
        client.expect(
            '[1] "user pager"\n',
            r="capture.output(utils::vignette(consolehelp::rendered))",
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
            "user vignette\n", r='vignette("rendered", package = "consolehelp")'
        )
        client.expect(
            '[1] "user vignette"\n',
            r="capture.output(utils::vignette(consolehelp::source))",
        )
        client.expect(
            '[1] "user vignette"\n',
            r="capture.output(utils::vignette(consolehelp::rendered))",
        )
        client.expect("user pager\n", r='help("topic", package = "consolehelp")')
        client.send(control="restart")
        client.expect(
            "documentation defaults installed after native profiles\n", r=check
        )
        return client.finish()
