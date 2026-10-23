#!/usr/bin/env -S uv run --script

import sys
import tarfile
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import SANDBOX, requires
from support.suites import run_this_suite


@requires(SANDBOX)
def test_installs_package_in_temporary_r_library(binary: Path) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""

    with TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        package = workspace / "mcpconsolelocalpkg"
        (package / "R").mkdir(parents=True)
        (package / "DESCRIPTION").write_text(
            """\
Package: mcpconsolelocalpkg
Title: Local R Installation Fixture
Version: 0.0.1
Authors@R: person("Test", "Fixture", email = "fixture@example.com", role = c("aut", "cre"))
Description: Verifies R package installation inside the sandbox.
License: MIT
Encoding: UTF-8
""",
            encoding="utf-8",
        )
        (package / "NAMESPACE").write_text("export(answer)\n", encoding="utf-8")
        (package / "R" / "answer.R").write_text(
            # fmt: r
            code("""
                answer <- function() 42L
                """),
            encoding="utf-8",
        )
        (package / "man").mkdir()
        (package / "man" / "answer.Rd").write_text(
            r"""\name{answer}
\alias{answer}
\title{Return the fixture answer}
\usage{answer()}
\description{Returns 42.}
""",
            encoding="utf-8",
        )
        archive = workspace / "mcpconsolelocalpkg_0.0.1.tar.gz"
        with tarfile.open(archive, "w:gz", format=tarfile.USTAR_FORMAT) as tar:
            tar.add(package, arcname=package.name)

        with McpClient(binary, ("serve",), environment, workspace) as client:
            client.initialize_and_list_tools()
            wait_for_evaluation_output(
                client,
                None,
                "sandbox R package installation",
                completion_timeout_seconds=client.response_timeout,
                # fmt: r
                r=code(r"""
                    library <- .libPaths()[[1L]]
                    stopifnot(
                      identical(Sys.getenv("MCP_CONSOLE_SANDBOX"), "1"),
                      startsWith(
                        library,
                        paste0(normalizePath(tempdir()), .Platform$file.sep)
                      ),
                      dir.exists(library),
                      file.access(library, 2L) == 0L
                    )
                    installer_stdout <- file.path(tempdir(), "mcpconsolelocalpkg.out")
                    installer_stderr <- file.path(tempdir(), "mcpconsolelocalpkg.err")
                    # Local installs ignore keep_outputs. Record complete streams
                    # separately because their interleaving is unspecified.
                    installer_status <- system2(
                      file.path(R.home("bin"), "R"),
                      c(
                        "CMD",
                        "INSTALL",
                        # Request the same help output across R installations.
                        "--html",
                        "-l",
                        shQuote(library),
                        "mcpconsolelocalpkg_0.0.1.tar.gz"
                      ),
                      stdout = installer_stdout,
                      stderr = installer_stderr
                    )
                    stopifnot(
                      file.exists(installer_stdout),
                      file.exists(installer_stderr)
                    )
                    writeLines(c(
                      "installer stdout:",
                      readLines(installer_stdout),
                      "installer stderr:",
                      readLines(installer_stderr)
                    ))
                    stopifnot(
                      installer_status == 0L,
                      identical(dirname(find.package("mcpconsolelocalpkg")), library),
                      identical(mcpconsolelocalpkg::answer(), 42L)
                    )
                    """),
            )
            install_output = last_tool_text(client)
            installer_stdout, separator, installer_stderr = install_output.partition(
                "installer stderr:\n"
            )
            assert installer_stdout.startswith("installer stdout:\n") and separator, (
                install_output
            )
            assert installer_stderr.endswith("* DONE (mcpconsolelocalpkg)\n"), (
                installer_stderr
            )
            client.send(control="restart")
            restart_notices = (
                "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
            )
            assert last_tool_text(client) == restart_notices
            client.expect(
                "new sandbox library ready\n",
                # fmt: r
                r=code(r"""
                    stopifnot(
                      startsWith(
                        .libPaths()[[1L]],
                        paste0(normalizePath(tempdir()), .Platform$file.sep)
                      ),
                      dir.exists(.libPaths()[[1L]])
                    )
                    cat("new sandbox library ready\n")
                    """),
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
