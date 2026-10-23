#!/usr/bin/env -S uv run --script

import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.normalization import normalize_process_diagnostic
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment
from support.records import Transcript
from support.suites import run_this_suite


@executions(DIRECT, SANDBOXED)
def test_probes_ambient_reticulate_before_first_use_bootstrap(
    binary: Path,
    execution: Execution,
) -> Transcript:
    environment, rscript = r_test_environment()
    fixture = Path(__file__).resolve().parents[3] / "fixtures" / "ambient_reticulate"
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        library = temporary / "library"
        library.mkdir()
        record = temporary / "reticulate-calls"
        environment["MCP_CONSOLE_TEST_RETICULATE_RECORD"] = str(record)
        environment["UV_TOOL_DIR"] = str(temporary)
        subprocess.run(
            [
                rscript.with_name("R.exe" if os.name == "nt" else "R"),
                "CMD",
                "INSTALL",
                f"--library={library}",
                fixture,
            ],
            check=True,
            capture_output=True,
            text=True,
            env=environment,
        )
        record.write_text("", encoding="utf-8")
        for name in ("R_LIBS", "R_LIBS_SITE", "R_LIBS_USER"):
            environment[name] = str(library)
        environment = bare_runtime_environment(environment, library)
        if os.name == "nt":
            environment["PATH"] = os.pathsep.join(
                entry
                for entry in environment["PATH"].split(os.pathsep)
                if not (Path(entry) / "python.exe").exists()
            )

        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=temporary,
        ) as client:
            client.initialize_and_list_tools()
            tools = client.transcript[-1]["result"]["tools"]
            assert "requirements" in tools[0]["inputSchema"]["properties"], tools
            prepared = client.send(requirements={"action": "get"})
            assert prepared["isError"] is True, prepared
            assert "fixture ambient reticulate bootstrap failed" in str(prepared), (
                prepared
            )
            eager_calls = ["namespace:--probe", "namespace:", "uv_binary"]
            assert record.read_text(encoding="utf-8").splitlines() == eager_calls

            result = client.send(r='stop("cell must not run")')
            assert result.get("isError") is True, result
            content = result["content"]
            assert len(content) == 1 and content[0]["type"] == "text", content
            output = content[0]["text"]
            assert "fixture ambient reticulate bootstrap failed" in output, output
            assert "cell must not run" not in output, output
            assert record.read_text(encoding="utf-8").splitlines() == [
                *eager_calls,
                "namespace:",
                "uv_binary",
            ]

            retry = client.send(r='stop("retry cell must not run")')
            assert retry.get("isError") is True, retry
            assert "fixture ambient reticulate bootstrap failed" in str(retry), retry
            assert "retry cell must not run" not in str(retry), retry
            assert record.read_text(encoding="utf-8").splitlines() == [
                *eager_calls,
                "namespace:",
                "uv_binary",
                "namespace:",
                "uv_binary",
            ]

            listed_again = client.request("tools/list")
            assert listed_again["result"]["tools"] == tools, listed_again
            listed_again["result"]["tools"] = "<unchanged from initialization>"
            transcript = client.finish()
            for entry in transcript:
                for part in entry.get("result", {}).get("content", []):
                    if part["type"] == "text":
                        part["text"] = normalize_process_diagnostic(part["text"])
            return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
