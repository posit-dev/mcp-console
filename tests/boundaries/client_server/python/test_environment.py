#!/usr/bin/env -S uv run --script

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.progress import without_elapsed
from support.assertions import last_result_text
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.events import Events
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.snapshots import execution_snapshots, platform_snapshots
from support.requirements import (
    OLD_PYTHON,
    OLD_PYTHON_EXECUTABLE,
    POSIX,
    PROCESS_EVENTS,
    R,
    requires,
)
from support.suites import run_this_suite
from boundaries.client_server.server.test_no_r import no_r_environment


def bootstrap_diagnostic(client: McpClient, ending: str) -> str:
    # Establish transport readiness, then observe startup diagnostics before
    # admitting code. Otherwise their idle/cell placement races with admission.
    result = client.send(requirements={"action": "get"}, timeout_ms=600_000)
    assert not result["isError"], result
    client.transcript.pop()
    start = len(client.transcript)
    deadline = time.monotonic() + client.response_timeout
    output = ""
    while True:
        result = client.send(timeout_ms=0)
        current = last_result_text(client)
        assert not result["isError"] and current.endswith("\n[idle]"), result
        output += current.removesuffix("\n[idle]")
        if output.endswith(ending):
            break
        assert time.monotonic() < deadline, "bootstrap diagnostic did not arrive"
    result["content"][0]["text"] = output + "\n[idle]"
    client.transcript[start:] = [client.transcript[-1]]
    return output


@executions(DIRECT, SANDBOXED)
@requires(R)
@platform_snapshots(
    "win32",
    reason="Windows rejects an invalid explicit Python before interpreter startup",
)
def test_preserves_configured_python_environment(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = "configured-by-user"
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    if os.name == "nt":
        # Windows inspects explicit selections on the host before launching
        # either interpreter; retain that earlier public startup failure.
        result = client.send(requirements={"action": "get"})
        assert result["isError"] is True, result
        assert last_result_text(client) == "explicit Python executable is not on PATH"
        transcript, errors = client.finish_with_standard_error(expected_exit_status=1)
        assert errors == "explicit Python executable is not on PATH\n", errors
        return transcript + [{"stderr": errors}]
    expected = "Error: explicit Python executable is not on PATH\n"
    assert bootstrap_diagnostic(client, expected) == expected
    # fmt: r
    r = code(r"""
        external_python_worker <- Sys.getpid()
        external_python_state <- 42L
        stopifnot(
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        "configured-by-user"
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "configured-by-user"\n', last_result_text(
        client
    )
    disabled = (
        "managed Python requirements are disabled because the session uses a "
        "user-selected Python environment"
    )
    for call_shape in ({}, {"control": "restart"}):
        client.send(
            **call_shape,
            requirements={"python": ["numpy"]},
        )
        result = client.transcript[-1]["result"]
        assert result["isError"] is True, result
        assert last_result_text(client) == disabled

    client.send(
        r="external_python_combined_side_effect <- TRUE",
        requirements={"python": ["numpy"]},
    )
    result = client.transcript[-1]["result"]
    assert result["isError"] is True, result
    assert last_result_text(client) == disabled
    # Reach the same worker-originated resolver request used by reticulate's
    # managed hooks. The server must enforce the external-selection policy
    # even though those hooks are not installed for this worker.
    # fmt: r
    r = code(r"""
        environment_request <- jsonlite::toJSON(list(
          requirements = list(packages = I("numpy")),
          retained_requirements = list(packages = I("numpy"))
        ), auto_unbox = TRUE)
        environment_error <- tryCatch(
          .Call("mcp_console_resolve_python", environment_request),
          error = conditionMessage
        )
        version_request <- jsonlite::toJSON(list(
          constraints = I(">=3.11")
        ), auto_unbox = TRUE)
        version_error <- tryCatch(
          .Call("mcp_console_resolve_python_version", version_request),
          error = conditionMessage
        )
        cat(environment_error, version_error, sep = "\n")
        """)
    client.send(r=r)
    output = last_result_text(client)
    assert output == f"{disabled}\n{disabled}\n", repr(output)
    # fmt: r
    r = code(r"""
        stopifnot(
          identical(Sys.getpid(), external_python_worker),
          identical(external_python_state, 42L),
          !exists("external_python_combined_side_effect", inherits = FALSE),
          identical(
            Sys.getenv("RETICULATE_PYTHON", unset = NA_character_),
            "configured-by-user"
          )
        )
        42L
        """)
    client.send(r=r)
    assert last_result_text(client) == "[1] 42\n"
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_preserves_empty_python_environment(
    binary: Path, execution: Execution
) -> Transcript:
    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = ""
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        """)
    client.send(r=r)
    assert last_result_text(client) == '[1] "managed"\n'
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(OLD_PYTHON)
@platform_snapshots(
    "win32",
    reason="Windows rejects an old explicit Python before interpreter startup",
)
def test_rejects_python_older_than_3_10(
    binary: Path, execution: Execution
) -> Transcript:
    # Resolve Apple's dispatcher before entering the sandbox; its Xcode probes
    # can emit unrelated diagnostics even when Python itself starts correctly.
    probe = subprocess.run(
        (
            OLD_PYTHON_EXECUTABLE,
            "-I",
            "-c",
            "import json, sys; print(json.dumps([sys.executable, sys.version_info[:2]]))",
        ),
        check=True,
        capture_output=True,
        text=True,
    )
    interpreter, version = json.loads(probe.stdout)
    assert version == [3, 9], version
    assert Path(interpreter).is_absolute()

    environment = os.environ.copy()
    environment["RETICULATE_PYTHON"] = str(interpreter)
    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    if os.name == "nt":
        result = client.send(requirements={"action": "get"})
        assert result["isError"] is True, result
        error = last_result_text(client)
        assert error.startswith(
            "selected Python inspection failed (exit code: 1): Traceback"
        ), error
        assert error.endswith(
            "RuntimeError: MCP Console requires Python 3.10 or later\r\n"
        ), error
        result = client.send(python="6 * 7")
        assert result["isError"] is True and last_result_text(client) == error, result
        transcript, errors = client.finish_with_standard_error(expected_exit_status=1)
        assert errors == error + "\n", errors
        return transcript + [{"stderr": errors}]
    startup_error = bootstrap_diagnostic(
        client, "RuntimeError: MCP Console requires Python 3.10 or later\n\n"
    )
    client.send(python="6 * 7")
    result = client.transcript[-1]["result"]
    assert result["isError"] is False, result
    output = result["content"][0]["text"]
    assert output == startup_error, output
    assert output.startswith(
        "Error: selected Python inspection failed (exit status: 1): Traceback"
    ), output
    assert output.endswith(
        "RuntimeError: MCP Console requires Python 3.10 or later\n\n"
    ), output
    assert "[worker stopped" not in output, output
    # Inspection rejects the selection before interpreter mutation. R remains usable.
    client.send(r="stopifnot(!reticulate::py_available(initialize = FALSE)); 42L")
    assert last_result_text(client) == "[1] 42\n", client.transcript[-1]
    return client.finish()


def managed_python_transcript(
    binary: Path, execution: Execution, configured: bool
) -> Transcript:
    environment = os.environ.copy()
    if configured:
        environment["RETICULATE_PYTHON"] = "managed"
    else:
        environment.pop("RETICULATE_PYTHON", None)
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required for managed-Python tests"
    environment.pop("RETICULATE_UV", None)
    environment["UV_OFFLINE"] = "1"

    client = McpClient(binary, execution.serve(), environment)
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        python <- Sys.getenv("RETICULATE_PYTHON", unset = NA_character_)
        config <- reticulate::py_config()
        history <- reticulate::py_require()$history
        stopifnot(
          identical(python, "managed"),
          file.exists(config$python),
          isTRUE(config$ephemeral),
          "pandas" %in% reticulate::py_require()$packages,
          !any(vapply(
            history,
            function(request) identical(request$requested_from, "base"),
            logical(1L)
          ))
        )
        """)
    client.send(r=r)
    assert last_result_text(client) == "[done]", client.transcript[-1]
    # fmt: python
    python = code("""
        import io
        import pandas as pd

        frame = pd.read_csv(io.StringIO("value\\n40\\n2\\n"))
        int(frame["value"].sum())
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output == "42\n", repr(output)
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_evaluates_with_default_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    return managed_python_transcript(binary, execution, configured=False)


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_evaluates_with_explicit_managed_python(
    binary: Path, execution: Execution
) -> Transcript:
    return managed_python_transcript(binary, execution, configured=True)


@executions(DIRECT, SANDBOXED)
def test_does_not_import_local_psutil_during_bootstrap(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        directory = Path(temporary)
        (directory / "psutil.py").write_text(
            "import builtins\n"
            "builtins.mcp_console_local_psutil_imported = True\n"
            "answer = 42\n",
            encoding="utf-8",
        )
        environment = os.environ.copy()
        environment.pop("RETICULATE_PYTHON", None)
        client = McpClient(
            binary,
            execution.serve(),
            environment,
            current_directory=directory,
        )
        client.initialize_and_list_tools()
        # fmt: python
        python = code("""
            import builtins
            import sys

            (
                40 + 2,
                hasattr(builtins, "mcp_console_local_psutil_imported"),
                "psutil" in sys.modules,
            )
            """)
        client.send(python=python)
        output = last_result_text(client)
        assert output == "(42, False, False)\n", repr(output)
        client.send(python="import psutil; psutil.answer")
        assert last_result_text(client) == "42\n"
        return client.finish()


@executions(DIRECT, SANDBOXED)
def test_sends_python_cell_with_initial_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import yaml12

        print(yaml12.__name__)
        """)
    client.send(
        python=python,
        requirements={"python": ["py-yaml12"]},
    )
    assert last_result_text(client) == "yaml12\n"
    return client.finish()


@execution_snapshots
@executions(DIRECT, SANDBOXED)
@requires(PROCESS_EVENTS)
def test_compacts_native_duckdb_progress_bar(
    binary: Path, execution: Execution
) -> Transcript:
    # fmt: python
    python = code(r"""
        import os
        import tempfile
        from pathlib import Path

        import duckdb

        connection = duckdb.connect()
        assert connection.execute(
            "SELECT "
            "current_setting('enable_progress_bar'), "
            "current_setting('enable_progress_bar_print'), "
            "current_setting('progress_bar_time')"
        ).fetchone() == (True, True, 2000)
        connection.execute("SET progress_bar_time = 1000000")
        connection.execute("SET threads = 1")
        row_count = 15_000_000
        # DuckDB 1.5.6 emits at least 100 native progress redraws while
        # processing this single-threaded physical table scan.
        connection.execute(
            "CREATE TABLE progress_rows AS "
            "SELECT CAST(value AS INTEGER) AS value "
            f"FROM range({row_count}) AS values(value)"
        )

        saved_stdout = os.dup(1)
        try:
            with tempfile.TemporaryFile() as capture:
                os.dup2(capture.fileno(), 1)
                connection.execute("SET progress_bar_time = 0")
                result = connection.execute(
                    "SELECT sum(hash(value)) FROM progress_rows"
                ).fetchone()
                capture.seek(0)
                progress = capture.read()
        finally:
            os.dup2(saved_stdout, 1)
            os.close(saved_stdout)

        assert result[0] is not None
        assert progress.count(b"\r") >= 100
        _ = Path("progress.bin").write_bytes(progress)
        with open("progress-ready", "wb", buffering=0) as ready:
            _ = ready.write(b"1")
        with open("progress-release", "rb", buffering=0) as release:
            assert release.read(1) == b"1"
        with os.fdopen(os.dup(1), "wb") as stdout:
            stdout.write(progress)
        with open("completion-release", "rb", buffering=0) as release:
            assert release.read(1) == b"1"
        """)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        with (
            closing(FifoCheckpoint.create(root / "progress-ready")) as ready,
            closing(FifoCheckpoint.create(root / "progress-release")) as release,
            closing(FifoCheckpoint.create(root / "completion-release")) as completion,
            McpClient(
                binary,
                execution.serve(
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ())
                ),
                no_r_environment(root),
                current_directory=root,
            ) as client,
            Events() as events,
        ):
            client.initialize_and_list_tools()
            # Finish startup and preparation before observing native progress.
            client.send(
                python="pass",
                requirements={"python": ["duckdb==1.5.6"]},
                timeout_ms=600_000,
            )
            assert last_result_text(client) == "[done]"
            try:
                initial = client.start_send(
                    python=python,
                    timeout_ms=0,
                )
                ready.wait(
                    "native DuckDB progress captured", timeout=client.response_timeout
                )
                client.receive(initial)
                assert (
                    without_elapsed(last_result_text(client))
                    == "\n[running; poll with an empty send]"
                )

                progress = (root / "progress.bin").read_bytes()
                session = next((root / ".agents/console/sessions").iterdir())
                raw = session / "capture/sessions/001/cell-002.txt"
                events.watch_file(raw)
                response = client.start_send(timeout_ms=220_000)
                release.release()
                # Native stdout and completion use independent transports. The
                # public recording proves the server received every redraw before
                # the worker is allowed to complete this response interval.
                deadline = time.monotonic() + 10
                while raw.read_bytes() != progress:
                    remaining = deadline - time.monotonic()
                    assert remaining > 0 and events.wait(remaining), (
                        "native progress did not reach the server recording"
                    )
                completion.release()
                client.receive(response)
            finally:
                release.release()
                completion.release()
            output = last_result_text(client)
            assert "\r" not in output, repr(output)
            final = output.rstrip()
            assert final.count("% ▕") == 1, repr(final)
            graphic, separator, elapsed = final.rpartition(" (")
            assert graphic.startswith("100% ▕"), repr(final)
            assert graphic.endswith("▏"), repr(final)
            assert separator and elapsed.endswith(" elapsed)"), repr(final)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                f"{graphic} (<elapsed>)\n"
            )
            client.transcript[-1]["transcript_normalization"] = {
                "target": "result.content[0].text",
                "elapsed": "omitted",
                "trailing_progress_padding": "omitted",
            }
            return client.finish()


@executions(DIRECT, SANDBOXED)
def test_uses_200_column_default(binary: Path, execution: Execution) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: python
    python = code("""
        import shutil

        import numpy as np
        import pandas as pd

        print(f"terminal columns: {shutil.get_terminal_size().columns}")
        print(f"pandas display.width: {pd.get_option('display.width')}")
        print(f"NumPy linewidth: {np.get_printoptions()['linewidth']}")
        pd.DataFrame(
            [range(12)],
            columns=[f"column_{column:02}" for column in range(12)],
        )
        """)
    client.send(python=python)
    output = last_result_text(client)
    assert output.startswith(
        """terminal columns: 200
pandas display.width: 200
NumPy linewidth: 200
"""
    ), repr(output)
    for column in range(12):
        assert f"column_{column:02}" in output
    assert "..." not in output
    assert "[1 rows x 12 columns]" not in output
    return client.finish()


@executions(DIRECT, SANDBOXED)
@requires(R)
def test_uses_200_column_default_after_r_initializes_python(
    binary: Path,
    execution: Execution,
) -> Transcript:
    client = McpClient(binary, execution.serve())
    client.initialize_and_list_tools()
    # fmt: r
    r = code(r"""
        reticulate::py_run_string(
          "
        import numpy as np
        import pandas as pd
        "
        )
        cat(
          "R-first NumPy linewidth: ",
          reticulate::py_eval("np.get_printoptions()['linewidth']"),
          "\nR-first pandas display.width: ",
          reticulate::py_eval("pd.get_option('display.width')"),
          "\n",
          sep = ""
        )
        """)
    client.send(r=r)
    assert last_result_text(client) == (
        "R-first NumPy linewidth: 200\nR-first pandas display.width: 200\n"
    )
    return client.finish()


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
@requires(R)
def test_prints_requirements_with_host_uv_cache(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary_directory:
        temporary = Path(temporary_directory)
        environment = os.environ.copy()
        trusted_cache = temporary / "trusted-uv-cache"
        worker_cache = temporary / "worker-uv-cache"
        uv_record = temporary / "uv-environment.jsonl"
        real_uv = shutil.which("uv")
        assert real_uv is not None, "real uv is required"
        environment["RETICULATE_UV"] = str(
            Path(__file__).parents[3] / "fixtures" / "record_uv_environment"
        )
        environment["MCP_CONSOLE_TEST_REAL_UV"] = real_uv
        environment["MCP_CONSOLE_TEST_UV_RECORD"] = str(uv_record)
        environment["MCP_CONSOLE_TEST_WORKER_UV_CACHE"] = str(worker_cache)
        environment["UV_CACHE_DIR"] = str(trusted_cache)
        environment["UV_TOOL_DIR"] = str(temporary)
        environment["UV_DEFAULT_INDEX"] = "https://pypi.org/simple"
        environment["UV_OFFLINE"] = "1"
        client = McpClient(
            binary,
            execution.serve(
                "-c", "python.managed.resolution=implicit", "-c", "cache=host"
            ),
            environment,
            current_directory=temporary,
        )
        client.initialize_and_list_tools()
        uv_record.write_text("", encoding="utf-8")
        # fmt: r
        r = code(r"""
            worker_cache <- Sys.getenv("MCP_CONSOLE_TEST_WORKER_UV_CACHE")
            Sys.setenv(
              UV_CACHE_DIR = worker_cache,
              UV_DEFAULT_INDEX = "file:///worker-selected-index"
            )
            reticulate::py_require("py-yaml12")
            invisible(reticulate::py_config())
            stopifnot(
              reticulate::py_module_available("yaml12"),
              identical(Sys.getenv("UV_CACHE_DIR"), worker_cache),
              identical(
                Sys.getenv("UV_DEFAULT_INDEX"),
                "file:///worker-selected-index"
              )
            )
            """)
        client.send(r=r)
        assert last_result_text(client) == "[done]", client.transcript[-1]
        records = [
            json.loads(line)
            for line in uv_record.read_text(encoding="utf-8").splitlines()
        ]
        assert records, "runtime managed resolution did not invoke uv"
        expected = {
            "UV_CACHE_DIR": str(trusted_cache),
            "UV_DEFAULT_INDEX": "https://pypi.org/simple",
            "UV_OFFLINE": None,
        }
        assert all(record == expected for record in records), records
        if execution == DIRECT:
            # Bridge attachment checks the bootstrapped declaration before
            # py_require adds yaml12. This local dry-run inherits worker settings.
            diagnostics = [
                json.loads(line)
                for line in Path(str(uv_record) + ".diagnostics")
                .read_text()
                .splitlines()
            ]
            assert len(diagnostics) == 1, diagnostics
            diagnostic = diagnostics[0]
            assert diagnostic["arguments"][:10] == [
                "pip",
                "install",
                "--dry-run",
                "--no-deps",
                "--color",
                "never",
                "--no-progress",
                "--no-build",
                "--no-config",
                "--python",
            ], diagnostic
            assert diagnostic["arguments"][11:] == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
            ], diagnostic
            assert diagnostic["environment"] == {
                "UV_CACHE_DIR": str(worker_cache),
                "UV_DEFAULT_INDEX": "file:///worker-selected-index",
                "UV_OFFLINE": "1",
            }, diagnostic
        else:
            assert not worker_cache.exists(), worker_cache
        return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
