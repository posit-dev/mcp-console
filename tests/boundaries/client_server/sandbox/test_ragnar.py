#!/usr/bin/env -S uv run --script

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import SANDBOXED
from support.normalization import (
    code,
    normalize_duckdb_progress,
    normalize_onnx_device_probe,
)
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import LINUX_SANDBOX, MACOS_SANDBOX, requires
from support.suites import run_this_suite


@requires(MACOS_SANDBOX)
def test_creates_ragnar_store_after_workspace_write_denial(binary: Path) -> Transcript:
    return creates_ragnar_store_after_workspace_write_denial(
        binary, "Operation not permitted"
    )


@requires(LINUX_SANDBOX)
def test_creates_ragnar_store_after_workspace_write_denial_on_linux(
    binary: Path,
) -> Transcript:
    return creates_ragnar_store_after_workspace_write_denial(
        binary, "Read-only file system", languages="r,sql"
    )


def creates_ragnar_store_after_workspace_write_denial(
    binary: Path, denial: str, *, languages: str | None = None
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    if languages is not None:
        # Match Rscript without loading optional Python document converters.
        environment["MCP_CONSOLE_LANGUAGES"] = languages
    # fmt: r
    r = code(r"""
        ragnar::ragnar_store_create(
          "knowledge.ragnar.duckdb",
          embed = NULL
        )
        """)
    with tempfile.TemporaryDirectory() as temporary:
        workspace = Path(temporary)
        # Match the worker's interactive error display in the batch reference.
        (workspace / "reference.R").write_text(
            "options(width = 200L, showErrorCalls = FALSE, rlang_interactive = TRUE)\n"
            + r
        )
        with McpClient(
            binary,
            SANDBOXED.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            requirements = {"r": ["ragnar"]}
            if languages is None:
                # Python-enabled launches load ragnar's optional document converters.
                requirements.update(
                    python=["markitdown[all]"], python_version=["<3.14"]
                )
            client.send(requirements=requirements)
            assert last_tool_text(client) == "[prepared]"

            client.send(r=r)
            output = normalize_duckdb_progress(client)
            assert "knowledge.ragnar.duckdb" in output, output
            assert denial in output
            assert not (workspace / "knowledge.ragnar.duckdb").exists()
            denied = client.transcript[-1]

            client.send(
                # fmt: r
                r=code(r"""
                    reference <- suppressWarnings(system2(
                      file.path(R.home("bin"), "Rscript"),
                      c("--vanilla", "reference.R"),
                      stdout = TRUE,
                      stderr = TRUE
                    ))
                    stopifnot(
                      identical(attr(reference, "status"), 1L),
                      identical(tail(reference, 1L), "Execution halted")
                    )
                    writeLines(jsonlite::toJSON(
                      paste0(paste(head(reference, -1L), collapse = "\n"), "\n"),
                      auto_unbox = TRUE
                    ))
                    """),
            )
            reference = normalize_onnx_device_probe(json.loads(last_tool_text(client)))
            assert output == reference, (output, reference)
            assert not (workspace / "knowledge.ragnar.duckdb").exists()
            for entry in (denied, client.transcript[-1]):
                entry["result"]["content"][0]["text"] = (
                    "<error output identical to live sandboxed Rscript --vanilla>"
                )
                entry["transcript_normalization"] = {
                    "target": "result.content[0].text",
                    "comparison": "exact equality with live sandboxed Rscript --vanilla",
                    "reference_adjustments": (
                        "interactive error display; remove Execution halted footer"
                    ),
                }

            # fmt: r
            r = code(r"""
                store_path <- file.path(tempdir(), "knowledge.ragnar.duckdb")
                store <- suppressMessages(ragnar::ragnar_store_create(
                  store_path,
                  embed = NULL
                ))
                stopifnot(DBI::dbIsValid(store@con), file.exists(store_path))
                writeLines("created store under the worker tempdir")
                """)
            client.send(r=r)
            assert (
                normalize_duckdb_progress(client)
                == "created store under the worker tempdir\n"
            )
            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
