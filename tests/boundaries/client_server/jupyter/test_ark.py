#!/usr/bin/env -S uv run --script

import base64
import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import wait_for_evaluation_output
from support.jupyter import jupyter_client
from support.normalization import code
from support.records import Transcript
from support.requirements import ARK, JUPYTER, JUPYTER_RETICULATE, R, requires
from support.suites import run_this_suite


@requires(JUPYTER, ARK, R)
def test_persistent_r_errors_and_plot_output(binary: Path) -> Transcript:
    from PIL import Image

    with jupyter_client(binary, "ark") as (client, _):
        client.expect("[1] 42\n", code="answer <- 40; answer + 2")
        wait_for_evaluation_output(
            client,
            lambda output: "intentional kernel error" in output,
            "ark error reply",
            expected_error=False,
            completion_timeout_seconds=30,
            code='stop("intentional kernel error")',
        )
        client.expect("[1] 42\n", code="answer + 2")
        result = client.send(
            # fmt: r
            code=code("""
                par(mar = rep(0, 4), bg = "white")
                plot.new()
                rect(0.25, 0.25, 0.75, 0.75, col = "red", border = NA)
                """),
        )
        assert not result.get("isError"), result
        assert len(result["content"]) == 1, result
        block = result["content"][0]
        assert block.keys() == {"type", "data", "mimeType"}, block
        assert block["type"] == "image" and block["mimeType"] == "image/png", block
        data = base64.b64decode(block["data"], validate=True)
        with Image.open(io.BytesIO(data)) as image:
            assert image.format == "PNG", image.format
            assert image.size == (800, 600), image.size
            rgb = image.convert("RGB")
            assert rgb.getpixel((400, 300)) == (255, 0, 0)
            assert rgb.getpixel((0, 0)) == (255, 255, 255)
        block["data"] = "<PNG: 800 x 600, red center, white corner>"
        return client.finish()


@requires(JUPYTER, ARK, R, JUPYTER_RETICULATE)
def test_reticulate_shares_process_and_objects(binary: Path) -> Transcript:
    with jupyter_client(binary, "ark", reticulate=True) as (client, _):
        schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
        assert set(schema["properties"]) == {
            "code",
            "r",
            "python",
            "stdin",
            "control",
            "timeout_ms",
            "requirements",
        }
        client.expect(
            "R ready\n",
            # fmt: r
            r=code("""
                shared <- 40L
                r_pid <- Sys.getpid()
                cat("R ready\\n")
                """),
        )
        client.expect(
            "same process: True\n42\n",
            # fmt: python
            python=code("""
                import os

                print("same process:", os.getpid() == r.r_pid)
                from_python = r.shared + 2
                print(from_python)
                """),
        )
        client.expect(
            "[1] 42\n",
            r="stopifnot(reticulate::py$from_python == 42L); print(reticulate::py$from_python)",
        )
        client.expect("42\n", code='cat(shared + 2L, "\\n", sep = "")')
        rejected = client.send(requirements={"r": ["DBI"]})
        assert rejected["isError"] is True, rejected
        client.expect("42\n", python="print(from_python)")
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
