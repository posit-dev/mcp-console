#!/usr/bin/env -S uv run --script

import json
import re
import subprocess
import sys
import tempfile
import threading
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.requirements import POSIX, R, SQL, command, requires
from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient, stop_client
from support.execution import DIRECT, RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code, normalize_python_resolution_error
from support.r import isolated_r_home
from support.records import Transcript
from support.suites import run_this_suite
from support.resolvers import (
    checkpoint_uv_environment,
    ir_run_records,
    recording_uv_environment,
    uv_python_row,
    write_uv_python_inventories,
)
from boundaries.client_server.requirements.test_r_automatic import (
    recording_fixture_r_environment,
)
from support.checkpoints import FifoCheckpoint, wait_for_path


def inspect(client: McpClient) -> dict:
    # Initial inspection includes cold preparation, not just manifest lookup.
    result = client.send(
        requirements={"action": "get"},
        timeout_ms=int(client.response_timeout * 1_000),
    )
    assert not result.get("isError"), result
    snapshot = result["structuredContent"]
    assert json.loads(last_tool_text(client)) == snapshot
    return snapshot


@executions(DIRECT, SANDBOXED)
def test_inspection_completes_while_prepared_cells_overlap(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                *(("--writable-root", str(workspace)) if execution == SANDBOXED else ())
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            declaration = inspect(client)
            client.expect("42\n", python="42")
            pending = client.start_send(
                # fmt: python
                python=code("""
                    from pathlib import Path
                    import time

                    Path("started").touch()
                    while not Path("release").exists():
                        time.sleep(0.01)
                    print(42)
                    """),
                requirements={"action": "add", "python": ["numpy"]},
                timeout_ms=int(client.response_timeout * 1_000),
            )
            try:
                wait_for_path(
                    workspace / "started",
                    "prepared cell is running",
                    client=client,
                    timeout=client.response_timeout,
                )
                # Receiving this request's ID before releasing the cell proves
                # inspection completes while the evaluation response is pending.
                assert inspect(client) == declaration
            finally:
                (workspace / "release").touch()
            client.receive(pending)
            assert not pending["result"]["isError"], pending
            assert pending["result"]["content"] == [{"type": "text", "text": "42\n"}], (
                pending
            )
            transcript = client.finish()
            return [
                *transcript[:3],
                {
                    "inspection_completed_before_cell_release": True,
                    "prepared_cell_completed": True,
                },
            ]


@executions(DIRECT, SANDBOXED)
def test_inspection_completes_under_repeated_overlap(
    binary: Path, execution: Execution
) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        declaration = inspect(client)
        client.expect("42\n", python="42")
        for _ in range(256):
            pending = [
                client.start_send(**arguments)
                for arguments in (
                    {
                        "python": "42",
                        "requirements": {"action": "add", "python": ["numpy"]},
                    },
                    *({"requirements": {"action": "get"}},) * 8,
                )
            ]
            client.receive_many(pending)
            for entry in pending:
                result = entry["result"]
                assert not result["isError"], result
                if entry["send"]["requirements"]["action"] == "get":
                    assert result["structuredContent"] == declaration, result
                else:
                    assert result["content"] == [{"type": "text", "text": "42\n"}], (
                        result
                    )
        idle = [
            client.start_send(session="default", **arguments)
            for arguments in ({}, {"requirements": {"action": "get"}}) * 8
        ]
        client.receive_many(idle)
        for entry in idle:
            result = entry["result"]
            assert not result["isError"], result
            if "requirements" in entry["send"]:
                assert result["structuredContent"] == declaration, result
            else:
                assert result["content"] == [{"type": "text", "text": "\n[idle]"}], (
                    result
                )
        assert client.request("ping")["result"] == {}
        client.finish()
    return [
        {
            "concurrent_inspections_and_prepared_cells_completed": 2304,
            "concurrent_inspections_and_idle_polls_completed": 16,
        }
    ]


@requires(R, SQL)
@executions(DIRECT, SANDBOXED)
def test_empty_declaration_and_round_trip(
    binary: Path, execution: Execution
) -> Transcript:
    # This case observes deliberate declarations without implicit preparation.
    client = McpClient(binary, execution.serve("--config", "r.resolution=explicit"))
    client.initialize_and_list_tools()
    startup = inspect(client)
    assert startup["prepared"] is True
    assert startup["requirements"]["python"] == [
        "numpy",
        "pandas",
        "matplotlib",
        "plotnine",
    ], startup
    assert startup["requirements"]["r"] == sorted(
        [
            "tidyverse",
            "dplyr",
            "dbplyr",
            "reticulate",
            "DBI",
            "duckdb",
            "nanoarrow",
            "yyjsonr",
            "ggplot2",
        ]
    ), startup
    assert "arrow" not in startup["runtime_requirements"]["r"], startup
    assert startup["runtime_requirements"]["python"] == []
    client.send(requirements=dict(startup["requirements"], action="set"))
    assert inspect(client) == startup
    client.send(requirements={"action": "reset"})
    assert inspect(client) == startup

    result = client.send(requirements={"action": "set"})
    assert not result.get("isError"), result
    empty = inspect(client)
    assert empty["prepared"] is True
    assert empty["requirements"] == {
        "r": [],
        "python": [],
        "duckdb": [],
        "python_version": [],
        "exclude_newer": None,
    }
    # fmt: python
    python = code("""
        import importlib.util
        import importlib.metadata
        import json
        import os
        import sys

        assert not {"numpy", "pandas", "matplotlib", "plotnine"} & {
            d.metadata["Name"] for d in importlib.metadata.distributions()
        }
        marker = 42
        original_pid = os.getpid()
        (
            json.loads("42"),
            importlib.util.find_spec("numpy"),
            importlib.util.find_spec("pandas"),
            importlib.util.find_spec("matplotlib"),
            importlib.util.find_spec("plotnine"),
        )
        """)
    client.send(python=python)
    assert last_tool_text(client) == "(42, None, None, None, None)\n"
    assert inspect(client) == empty
    result = client.send(
        sql="SELECT 42 AS answer", timeout_ms=int(client.response_timeout * 1_000)
    )
    assert not result.get("isError"), result
    assert inspect(client) == empty
    client.send(control="restart")
    client.send(python=python)
    assert last_tool_text(client) == "(42, None, None, None, None)\n"
    assert inspect(client) == empty

    edited = dict(
        empty["requirements"],
        python=["six"],
        python_version=[">=3.11", "<3.14"],
        exclude_newer="2026-01-01",
    )
    result = client.send(requirements=dict(edited, action="set"))
    assert result.get("isError"), result
    assert 'control="restart"' in result["content"][0]["text"]
    client.send(python="marker, os.getpid() == original_pid")
    assert last_tool_text(client) == "(42, True)\n"
    assert inspect(client) == empty

    result = client.send(
        control="restart",
        requirements=dict(edited, action="set"),
        python="import six; six.__name__",
    )
    assert not result.get("isError"), result
    assert "'six'" in last_tool_text(client)
    selected = inspect(client)
    assert selected["requirements"] == dict(
        edited, python_version=sorted(edited["python_version"])
    )
    client.expect(
        # fmt: python
        python=code("""
            import importlib.metadata

            assert not {"numpy", "pandas", "matplotlib", "plotnine"} & {
                d.metadata["Name"] for d in importlib.metadata.distributions()
            }
            """),
    )
    client.send(requirements=dict(selected["requirements"], action="set"))
    assert last_tool_text(client) == "[prepared]"
    client.send(python="marker = 42")
    client.send(
        control="restart",
        requirements=dict(selected["requirements"], action="set"),
        python='"marker" in globals()',
    )
    assert "False" in last_tool_text(client)
    assert inspect(client) == selected

    client.send(
        control="restart",
        requirements={"action": "set", "r": [], "python": [], "duckdb": []},
    )
    assert inspect(client) == empty
    client.send(requirements={"python": ["six"]})
    added = inspect(client)
    assert added["requirements"] == dict(empty["requirements"], python=["six"])
    client.send(requirements={"action": "add", "python": ["six", "six"]})
    assert inspect(client) == added
    client.send(control="restart")
    assert inspect(client) == added
    client.send(
        control="restart",
        requirements={"python_version": [">=3.11"], "exclude_newer": "2026-01-01"},
    )
    assert inspect(client)["requirements"] == dict(
        added["requirements"], python_version=[">=3.11"], exclude_newer="2026-01-01"
    )
    client.send(control="restart", requirements={"action": "reset"})
    assert inspect(client)["requirements"] == startup["requirements"]
    client.expect(
        # fmt: python
        python=code("""
            import importlib.metadata

            for package in ("numpy", "pandas", "matplotlib", "plotnine"):
                assert importlib.metadata.version(package)
            import matplotlib, plotnine
            """),
    )
    client.expect(r='stopifnot(requireNamespace("ggplot2", quietly = TRUE))')
    return client.finish()


@executions(RUNTIME)
def test_inspection_validation(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # Validation owns request admission, not the host-specific default inventory.
    client.expect("[prepared]", requirements={"action": "set"})
    initial = inspect(client)
    for extra in ({"python": "42"}, {"stdin": ""}, {"control": "restart"}):
        result = client.send(requirements={"action": "get"}, **extra)
        assert result.get("isError"), result
    for action in ("get", "reset"):
        for payload in (
            {"r": []},
            {"python": []},
            {"duckdb": []},
            {"python_version": []},
            {"exclude_newer": None},
            {"r": None},
            {"python_version": None},
        ):
            result = client.send(requirements=dict(payload, action=action))
            assert result.get("isError"), result
    result = client.send(requirements={})
    assert result.get("isError"), result
    assert inspect(client) == initial
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_inspection_during_input_and_replacement_resolution(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        environment, started, release = checkpoint_uv_environment(
            Path(directory), "six"
        )
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        try:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set"})
            old = inspect(client)
            client.send(python='marker = 42; print("before input"); answer = input()')
            assert "[waiting for stdin]" in last_tool_text(client)
            assert inspect(client) == old
            wait_for_evaluation_output(
                client,
                "[done]",
                "Python input completion",
                stdin="kept\n",
                timeout_ms=0,
            )
            client.send(python="(marker, answer)")
            assert last_tool_text(client) == "(42, 'kept')\n"
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="import six; six.__name__",
            )
            started.wait("replacement resolver")
            assert inspect(client) == old
            release.release()
            client.receive(pending)
            assert not pending["result"].get("isError"), pending
            assert "'six'" in pending["result"]["content"][0]["text"]
            assert inspect(client)["requirements"] == dict(
                old["requirements"], python=["six"]
            )
            return client.finish()
        finally:
            stop_client(client)
            started.close()
            release.close()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupted_replacement_preserves_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        temporary = Path(directory)
        environment, started, release = checkpoint_uv_environment(temporary, "six")
        interrupted = FifoCheckpoint.create(temporary / "interrupted")
        interrupt_release = FifoCheckpoint.create(temporary / "interrupt-release")
        environment["MCP_CONSOLE_TEST_UV_INTERRUPTED"] = str(interrupted.path)
        environment["MCP_CONSOLE_TEST_UV_INTERRUPT_RELEASE"] = str(
            interrupt_release.path
        )
        client = McpClient(binary, execution.serve("-c", "cache=host"), environment)
        try:
            client.initialize_and_list_tools()
            client.expect(
                python="import os; marker = 42; pid = os.getpid()",
                requirements={"action": "set"},
            )
            old = inspect(client)
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="marker = 0",
            )
            started.wait("replacement resolver")
            assert inspect(client) == old
            interrupt = client.start_send(control="interrupt")
            # Signal handling is a checkpoint, not a scheduling benchmark.
            interrupted.wait("interrupted replacement resolver", timeout=30)
            assert inspect(client) == old
            interrupt_release.release()
            client.receive(pending)
            client.receive(interrupt)
            assert pending["result"].get("isError"), pending
            error = pending["result"]["content"][0]["text"]
            assert "managed Python resolution failed" in error, pending
            pending["result"]["content"][0]["text"] = normalize_python_resolution_error(
                error
            )
            assert inspect(client) == old
            client.send(python="marker, os.getpid() == pid")
            assert last_tool_text(client) == "(42, True)\n"
            client.send(control="restart")
            assert inspect(client) == old
            return client.finish()
        finally:
            stop_client(client)
            for checkpoint in (started, release, interrupted, interrupt_release):
                checkpoint.close()


@requires(R, POSIX, SQL)
@executions(DIRECT, SANDBOXED)
@requires(command("ir"))
def test_r_duckdb_replacement_failure_and_reset(
    binary: Path, execution: Execution
) -> Transcript:
    missing_requests: list[str] = []

    class MissingExtension(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            missing_requests.append(self.path)
            self.send_error(404, "fixture extension does not exist")

        def log_message(self, *_: object) -> None:
            pass

    with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
        origin = stack.enter_context(
            ThreadingHTTPServer(("127.0.0.1", 0), MissingExtension)
        )
        thread = threading.Thread(target=origin.serve_forever)
        thread.start()
        stack.callback(thread.join)
        stack.callback(origin.shutdown)
        environment, record = recording_fixture_r_environment(
            Path(directory), ("mcpcleared",)
        )
        selected = isolated_r_home(Path(directory), environment)
        rscript = selected / "bin/Rscript"
        native_rscript = rscript.with_name("Rscript-original")
        rscript.rename(native_rscript)
        native_stderr = Path(directory) / "rscript.stderr"
        environment["MCP_CONSOLE_TEST_REAL_RSCRIPT"] = str(native_rscript)
        environment["MCP_CONSOLE_TEST_RSCRIPT_STDERR"] = str(native_stderr)
        environment["MCP_CONSOLE_TEST_MISSING_EXTENSION_PROXY"] = (
            f"http://127.0.0.1:{origin.server_port}"
        )
        # Capture the native resolver diagnostic before Console receives it.
        # Route only the missing-extension download to a controlled 404; the
        # real DuckDB installer and its complete R diagnostic stay under test.
        # fmt: python
        capture = code("""
            import os
            import subprocess
            import sys
            from pathlib import Path

            data = sys.stdin.buffer.read()
            environment = os.environ.copy()
            if b"not_a_real_duckdb_extension" in data:
                environment["HTTP_PROXY"] = environment["MCP_CONSOLE_TEST_MISSING_EXTENSION_PROXY"]
            result = subprocess.run(
                [os.environ["MCP_CONSOLE_TEST_REAL_RSCRIPT"], *sys.argv[1:]],
                input=data,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode != 0:
                Path(os.environ["MCP_CONSOLE_TEST_RSCRIPT_STDERR"]).write_bytes(result.stderr)
            sys.stdout.buffer.write(result.stdout)
            sys.stderr.buffer.write(result.stderr)
            raise SystemExit(result.returncode)
            """)
        rscript.write_text(f"#!{sys.executable}\n{capture}")
        rscript.chmod(0o755)
        environment["MCP_CONSOLE_TEST_IR_FAIL_REQUIREMENT"] = "missing.fixture"
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "python.managed.resolution=implicit",
                "-c",
                "python.managed.packages=[six]",
                "-c",
                "r.packages=[praise]",
                "-c",
                "cache=host",
                *(
                    ("-c", "resolver.sandbox.network=enabled")
                    if execution == SANDBOXED
                    else ()
                ),
            ),
            environment,
        ) as client:
            client.initialize_and_list_tools()
            startup = inspect(client)
            assert startup["prepared"] is True
            assert ir_run_records(record), "background startup did not prepare defaults"
            client.expect(
                r="marker <- 42L; pid <- Sys.getpid()", requirements={"action": "set"}
            )
            empty = inspect(client)
            failure = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "r": ["missing.fixture"],
                    "python": ["six"],
                },
                r="marker <- 0L",
            )
            assert failure.get("isError"), failure
            assert inspect(client) == empty
            failure = client.send(
                control="restart",
                requirements={
                    "action": "set",
                    "r": ["praise"],
                    "duckdb": ["not_a_real_duckdb_extension"],
                },
            )
            assert failure.get("isError"), failure
            error = failure["content"][0]["text"]
            prefix = "DuckDB extension resolution failed with exit status: 1: "
            expected = prefix + native_stderr.read_text().strip()
            assert error == expected, {"actual": error, "expected": expected}
            assert (
                'Failed to download extension "not_a_real_duckdb_extension"' in error
            ), error
            assert "(HTTP 404)" in error, error
            assert missing_requests and all(
                path.endswith("/not_a_real_duckdb_extension.duckdb_extension.gz")
                for path in missing_requests
            ), missing_requests
            failure["content"][0]["text"] = (
                prefix + "<stderr identical to live Rscript --vanilla>"
            )
            client.transcript[-1]["transcript_normalization"] = {
                "target": "result.content[0].text",
                "reference": "native Rscript --vanilla stderr captured before forwarding",
                "comparison": "exact equality after exit-status prefix; outer whitespace trimmed",
            }
            assert inspect(client) == empty
            client.expect(r="stopifnot(marker == 42L, pid == Sys.getpid())")
            client.expect(
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\n"
                "Loading required namespace: praise\n[done]",
                control="restart",
                requirements={"action": "set", "r": ["praise"], "duckdb": ["fts"]},
                r='stopifnot(requireNamespace("praise"), !exists("marker"))',
            )
            selected = inspect(client)
            assert selected["requirements"] == dict(
                empty["requirements"], r=["praise"], duckdb=["fts"]
            )
            client.expect(
                "# A tibble: 1 × 1\n   answer\n  <int32>\n1      42\n",
                sql="LOAD fts; SELECT 42 AS answer",
            )
            client.send(
                control="restart", requirements={"action": "set", "python": ["six"]}
            )
            assert inspect(client)["requirements"] == dict(
                empty["requirements"], python=["six"]
            )
            client.expect(
                "[resolved PyPI distribution 'py-yaml12' for Python import 'yaml12']\n"
                "'yaml12'\n",
                python="import yaml12; yaml12.__name__",
            )
            assert "py-yaml12" in inspect(client)["requirements"]["python"]
            client.expect(r='stopifnot(requireNamespace("mcpcleared", quietly = TRUE))')
            assert "mcpcleared" in inspect(client)["requirements"]["r"]
            client.send(control="restart", requirements={"action": "reset"})
            assert inspect(client)["requirements"] == startup["requirements"]
            return client.finish()


@executions(RUNTIME)
def test_large_manifest_round_trip(binary: Path, execution: Execution) -> Transcript:
    with McpClient(binary, execution.serve()) as client:
        client.initialize_and_list_tools()
        client.send(requirements={"action": "set"})
        packages = [
            f"absent-fixture-{i}; python_version < '0' and platform_system == '{'x' * 120}'"
            for i in range(65)
        ]
        for group in (packages[:64], packages[64:]):
            result = client.send(requirements={"python": group})
            assert not result.get("isError"), result
        result = client.send(requirements={"action": "get"})
        selected = result["structuredContent"]["requirements"]
        assert selected["python"] == sorted(packages)
        assert (
            "complete requirements declaration is in structuredContent"
            in last_tool_text(client)
        )
        result = client.send(requirements=dict(selected, action="set"))
        assert not result.get("isError"), result
        assert (
            client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            == selected
        )
        original = json.dumps(client.finish())
        token = "<x*120>"
        assert token not in original
        compacted = original.replace("x" * 120, token)
        assert compacted.replace(token, "x" * 120) == original
        transcript = json.loads(compacted)
        transcript.insert(
            3,
            {
                "transcript_normalization": {
                    "fixture_padding": {"token": token, "character": "x", "count": 120}
                }
            },
        )
        return transcript


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(command("yamark"))
def test_records_requirement_boundaries(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        environment, _ = recording_uv_environment(workspace)
        inventories = workspace / "uv-python-inventories.json"
        environment["MCP_CONSOLE_TEST_UV_PYTHON_INVENTORIES"] = str(inventories)
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            environment,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.send(requirements={"action": "set"}, python="first_environment = 1")
            client.send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                python="second_environment = 2",
            )
            # Keep the complete failure diagnostic independent of uv's changing
            # installed/downloadable version inventory. Successful preparation
            # continues to use the real resolver.
            write_uv_python_inventories(
                inventories,
                {"only-managed": [uv_python_row("3.12.12"), uv_python_row("3.11.14")]},
            )
            result = client.send(
                control="restart",
                requirements={"action": "set", "python_version": [">3", "<2"]},
            )
            assert result.get("isError"), result
            inventories.unlink()
            client.send(control="restart", requirements={"action": "reset"})
            transcript = client.finish()
        session = next((workspace / ".agents/console/sessions").iterdir())
        events = [
            json.loads(line)
            for line in (session / "capture/events.jsonl").read_text().splitlines()
        ]
        boundaries = [
            event for event in events if event["event"] == "requirements_selected"
        ]
        assert [event["action"] for event in boundaries] == ["set", "set", "reset"]
        assert boundaries[0]["snapshot"]["requirements"]["python"] == []
        assert boundaries[1]["snapshot"]["requirements"]["python"] == ["six"]
        assert boundaries[2]["snapshot"]["requirements"]["python"] == [
            "numpy",
            "pandas",
            "matplotlib",
            "plotnine",
        ]
        documents = [
            path.read_text()
            for path in sorted((session / "sessions").glob("session-*.qmd"))
        ]
        assert len(documents) == 2, documents
        first, second = documents
        assert "first_environment = 1" in first and "second_environment" not in first
        assert "second_environment = 2" in second and "first_environment" not in second
        assert "    - six\n" in second
        assert all("captured output" not in document for document in documents)
        markdown = (session / "transcript.md").read_text()
        assert "Requirements selected" in markdown
        formatting = subprocess.run(
            [
                "yamark",
                "format",
                "--diff",
                "--wrap",
                "sentence",
                "--skip-embedded-formatters",
                "transcript.md",
                *[
                    str(path.relative_to(session))
                    for path in sorted((session / "sessions").glob("session-*.qmd"))
                ],
            ],
            cwd=session,
            capture_output=True,
            text=True,
        )
        assert formatting.returncode == 0, formatting.stdout + formatting.stderr
        return transcript


if __name__ == "__main__":
    run_this_suite(__file__)
