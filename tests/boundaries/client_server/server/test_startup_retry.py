"""Explicit restart retries failed initial setup without replaying rejected cells."""

import base64
import json
import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_startup import selected_python
from support.python import isolated_python
from boundaries.client_server.python.test_without_r import environment as without_r
from boundaries.client_server.server.test_startup import (
    wait_for_send_admission,
)
from support.checkpoints import FifoCheckpoint, wait_for_checkpoint
from support.assertions import (
    last_result_text,
    last_tool_text,
    wait_for_evaluation_output,
    wait_for_idle_output,
)
from support.client import McpClient
from support.discovery import discovery_environment
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.native import LOADER_VARIABLE, build_interposer
from support.normalization import code, normalize_process_diagnostic
from support.previews import assert_preview, compact_previews
from support.progress import without_elapsed
from support.snapshots import execution_snapshots
from support.processes import (
    capture_process_identity,
    child_process_identities,
    kill_processes,
    live_processes,
)
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import (
    NATIVE_FIXTURES,
    POSIX,
    PROCESS_EVENTS,
    R,
    command,
    requires,
)
from support.resolvers import checkpoint_uv_environment, expose_uv
from support.suites import run_this_suite


def managed_python_environment(root: Path) -> dict[str, str]:
    uv = shutil.which("uv")
    assert uv is not None, "real uv is required"
    installations = root / "python-installations"
    subprocess.run(
        [uv, "python", "install", "3.13", "--install-dir", str(installations)],
        env=os.environ | {"UV_PYTHON_DOWNLOADS": "automatic"},
        check=True,
        capture_output=True,
    )
    return {
        "UV_PYTHON_INSTALL_DIR": str(installations),
        "UV_PYTHON_DOWNLOADS": "never",
    }


def preparation_reap_environment(root: Path, python: Path) -> dict[str, str]:
    environment = selected_python(root, python)
    environment.pop("R_HOME", None)
    environment.update(
        {
            "PATH": str(root),
            LOADER_VARIABLE: str(build_interposer(root, "preparation_reap_interposer")),
            "MCP_CONSOLE_TEST_REAP_PID": str(root / "resolver-pid"),
            "MCP_CONSOLE_TEST_REAP_DONE": str(root / "reaped"),
        }
    )
    return environment


@requires(NATIVE_FIXTURES)
def test_retry_retains_preparation_startup_and_idle_output(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        python, site = isolated_python(root)
        python.unlink()
        environment = selected_python(root, python)
        environment.pop("R_HOME", None)
        environment["PATH"] = str(root)
        environment[LOADER_VARIABLE] = str(
            build_interposer(root, "discovery_diagnostic")
        )
        startup_text = "recorded Python startup\n"
        idle_text = "idle head\n" + "s" * 32768 + "\nidle tail\n"
        with (
            closing(FifoCheckpoint.create(root / "startup-release")) as startup,
            closing(FifoCheckpoint.create(root / "idle-release")) as idle,
            McpClient(
                binary,
                DIRECT.serve(),
                environment,
                root,
            ) as client,
        ):
            (site / "sitecustomize.py").write_text(
                # fmt: python
                code(f"""
                    import sys
                    from pathlib import Path

                    if sys.argv[0] != "-c":
                        with Path({str(startup.path)!r}).open("rb", buffering=0) as gate:
                            assert gate.read(1) == b"1"
                        print({startup_text!r}, end="", flush=True)
                    """),
            )
            client.initialize_and_list_tools()
            failure = client.send()
            assert failure.get("isError"), failure
            assert "selected Python executable is not an absolute file" in str(failure)
            (recording,) = (root / ".agents/console/sessions").iterdir()
            journal = recording / "capture/events.jsonl"
            preparation = b"preparation detail\n"

            def preparation_capture() -> bytes:
                return b"".join(
                    base64.b64decode(event["data"])
                    for line in journal.read_text().splitlines()
                    if (event := json.loads(line))["event"] == "process_output"
                )

            assert preparation_capture() == preparation
            assert not (recording / "capture/sessions/001").exists()

            python.symlink_to(sys.executable)
            restart = client.start_send(control="restart", timeout_ms=60_000)
            wait_for_checkpoint(
                lambda: journal if preparation_capture() == preparation * 2 else None,
                "retry preparation remains process output before worker launch",
                root=recording,
                client=client,
            )
            client.receive(restart)
            repaired = restart["result"]
            assert not repaired.get("isError"), repaired
            assert last_tool_text(client) == preparation.decode() + "\n[idle]", repaired
            startup.release()
            wait_for_idle_output(
                client, startup_text + "\n[idle]", "repaired startup output"
            )
            startup_log = recording / "capture/sessions/001/startup.txt"
            assert startup_log.read_bytes() == startup_text.encode()
            inode = startup_log.stat().st_ino

            client.expect(
                "[done]",
                # fmt: python
                python=code(f"""
                    from pathlib import Path
                    from threading import Thread

                    def emit_idle():
                        with Path({str(idle.path)!r}).open("rb", buffering=0) as gate:
                            assert gate.read(1) == b"1"
                        print("idle head\\n" + "s" * 32768 + "\\nidle tail", flush=True)

                    thread = Thread(target=emit_idle)
                    thread.start()
                    """),
            )
            idle.release()
            log = recording / "capture/sessions/001/cell-001-post.txt"
            expected_raw = idle_text.encode()
            wait_for_checkpoint(
                lambda: (
                    log if log.exists() and log.read_bytes() == expected_raw else None
                ),
                "post-repair idle text is retained outside the completed cell",
                root=log.parent,
                client=client,
            )
            client.send()
            preview = last_tool_text(client)
            assert preview.endswith("\n[idle]"), preview
            omitted = assert_preview(preview.removesuffix("\n[idle]"), idle_text)
            assert (
                f"raw log: .agents/console/sessions/{recording.name}/capture/sessions/001/cell-001-post.txt"
                in preview
            )
            client.expect("42\n", python="thread.join(); 42")
            assert startup_log.stat().st_ino == inode
            assert startup_log.read_bytes() == startup_text.encode()
            assert preparation_capture() == preparation * 2
            compact_previews(client, "s")
            transcript = client.finish()
            assert log.read_bytes() == expected_raw
            events = [json.loads(line) for line in journal.read_text().splitlines()]
            (summary,) = [
                event
                for event in events
                if event["event"] == "session_output"
                and event["path"] == "capture/sessions/001/cell-001-post.txt"
            ]
            assert summary["retained_bytes"] == len(expected_raw), summary
            assert summary["discarded_bytes"] == 0, summary
            assert summary["inline_omitted_bytes"] == omitted, summary
            return json.loads(
                json.dumps(transcript)
                .replace(str(root), "<workspace>")
                .replace(recording.name, "<run ID>")
            ) + [
                {
                    "session_output": {
                        key: summary[key]
                        for key in (
                            "path",
                            "retained_bytes",
                            "discarded_bytes",
                            "inline_omitted_bytes",
                        )
                    },
                    "startup_log_preserved": True,
                    "preparation_capture_preserved": True,
                }
            ]


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_code_free_retry_starts_prepared_replacement(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        hooks = root / "hooks"
        hooks.mkdir()
        with closing(FifoCheckpoint.create(root / "prepared-startup")) as started:
            (hooks / "sitecustomize.py").write_text(
                # fmt: python
                code(f"""
                    import importlib.util
                    from pathlib import Path

                    # Only the replacement declaration omits default NumPy.
                    if importlib.util.find_spec("numpy") is None:
                        import six

                        print("prepared Python startup ran", flush=True)
                        with Path({str(started.path)!r}).open("wb", buffering=0) as ready:
                            assert ready.write(b"1") == 1
                    """),
            )
            environment = without_r(root)
            environment.update(managed_python_environment(root))
            environment.update(
                {
                    "UV_TOOL_DIR": str(root),
                    "RETICULATE_PYTHONPATH": str(hooks),
                }
            )
            with McpClient(
                binary,
                execution.serve(
                    "-c",
                    "cache=host",
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
                ),
                environment,
                root,
            ) as client:
                client.initialize_and_list_tools()
                failure = client.send(python="raise AssertionError('must not run')")
                assert failure.get("isError"), failure
                assert "require `uv` on PATH" in str(failure), failure
                expose_uv(root)
                assert client.send() == failure
                client.expect(
                    "[runtime discovery retried]\n[idle]",
                    control="restart",
                    requirements={"action": "set", "python": ["six"]},
                )
                started.wait("prepared replacement runs startup without another cell")
                wait_for_idle_output(
                    client,
                    "prepared Python startup ran\n\n[idle]",
                    "prepared replacement startup output",
                )
                client.expect(
                    "prepared cell ran\n",
                    python="import six; print('prepared cell ran')",
                )
                return client.finish()


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_restart_accepts_repair_after_preparation_reaping(binary: Path) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        python = root / "selected-python"
        environment = preparation_reap_environment(root, python)
        with McpClient(binary, DIRECT.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            failure = client.send(python="42")
            assert failure.get("isError"), failure
            assert (root / "resolver-pid").exists(), "preparation did not close"
            assert (root / "reaped").exists(), "startup finished before reaping"
            python.symlink_to(sys.executable)
            client.expect(
                "[runtime discovery retried]\n42\n[done]",
                control="restart",
                python="42",
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(NATIVE_FIXTURES, PROCESS_EVENTS)
def test_restart_rejects_unretired_preparation_after_selection_failure(
    binary: Path,
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        python = root / "selected-python"
        environment = preparation_reap_environment(root, python)
        environment.update(
            {
                "MCP_CONSOLE_TEST_REAP_BLOCK_CLOSE": str(root / "blocked-close"),
                "MCP_CONSOLE_TEST_REAP_DENY_KILL": str(root / "denied-kill"),
            }
        )
        identity = None
        with (
            closing(FifoCheckpoint.create(root / "blocked-close")) as blocked,
            McpClient(binary, DIRECT.serve(), environment, root) as client,
        ):
            try:
                client.initialize_and_list_tools()
                tools = client.transcript[2]["result"]
                initial = client.start_send(python="42")
                blocked.wait(
                    "selection failed and preparation cannot acknowledge Close"
                )
                identity = capture_process_identity(
                    int((root / "resolver-pid").read_text())
                )
                python.symlink_to(sys.executable)
                client.receive(initial)
                failure = initial["result"]
                assert failure.get("isError"), failure
                assert "retirement unconfirmed" in str(failure), failure
                assert (root / "denied-kill").exists()
                assert not (root / "reaped").exists()
                assert live_processes([identity]), "preparation must still be alive"

                rejected = client.send(control="restart", python="42")
                assert rejected.get("isError"), rejected
                assert rejected["content"] == [
                    {
                        "type": "text",
                        "text": "runtime discovery retry requires confirmed preparation cleanup",
                    }
                ], rejected
                assert client.send(python="42") == failure
                assert client.send(control="restart") == rejected
                assert live_processes([identity]), "retry must retain the old owner"
                assert child_process_identities(
                    capture_process_identity(client.process.pid)
                ) == (identity,), "retry started another preparation process"
                assert client.request("tools/list")["result"] == tools
                client.transcript[-1] = {"tools_schema_unchanged": True}
                transcript, stderr = client.finish_with_standard_error(
                    expected_exit_status=1
                )
                return json.loads(
                    json.dumps(
                        transcript
                        + [
                            {
                                "stderr": stderr,
                                "unretired_preparation_blocked_retry": True,
                            }
                        ]
                    ).replace(str(root), "<workspace>")
                )
            finally:
                # A failing regression can admit a second preparation process.
                # Retire all fixture children before disposing the server.
                if client.process.poll() is None:
                    kill_processes(
                        child_process_identities(
                            capture_process_identity(client.process.pid)
                        )
                    )
                if identity is not None:
                    kill_processes([identity])


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_repairs_missing_selected_python(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        python = root / "selected-python"
        environment = without_r(root)
        environment["RETICULATE_PYTHON"] = str(python)
        with McpClient(binary, execution.serve(), environment, root) as client:
            client.initialize_and_list_tools()
            tools = client.transcript[2]["result"]
            failure = client.send(python="rejected_cell = True")
            assert failure.get("isError"), failure
            python.symlink_to(sys.executable)
            assert client.send(python="rejected_cell = True") == failure
            repaired = client.send(
                control="restart",
                python="assert 'rejected_cell' not in globals(); answer = 42; answer",
            )
            assert not repaired.get("isError"), repaired
            assert repaired["content"] == [
                {"type": "text", "text": "[runtime discovery retried]\n42\n[done]"}
            ], repaired
            assert client.send(python="answer + 1")["content"] == [
                {"type": "text", "text": "43\n"}
            ]
            assert client.request("tools/list")["result"] == tools
            client.transcript[-1] = {"tools_schema_unchanged": True}
            client.send(
                # fmt: python
                python=code("""
                    import os

                    os.environ["RETICULATE_PYTHON"] = "/unavailable/python"
                    os.environ["PATH"] = "/unavailable/tools"
                    """),
            )
            restarted = client.send(
                control="restart", python="assert 'answer' not in globals(); 42"
            )
            assert not restarted.get("isError"), restarted
            assert restarted["content"][0]["text"].endswith("42\n[done]"), restarted
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(R, command("rustc"))
def test_restart_repairs_failed_r_discovery(binary: Path) -> Transcript:
    r_environment, _ = r_test_environment()
    with discovery_environment() as (environment, discovery):
        with McpClient(binary, DIRECT.serve(), environment) as client:
            discovery.wait("initial R discovery is blocked")
            client.initialize_and_list_tools()
            discovery.release()
            failure = client.send(r="stop('rejected cell must not run')")
            assert failure.get("isError"), failure
            (discovery.root / "r-home").write_text(
                r_environment["R_HOME"], encoding="utf-8"
            )
            assert client.send(r="stop('ordinary send must not retry')") == failure
            repaired = client.send(control="restart", r="answer <- 42; answer")
            assert not repaired.get("isError"), repaired
            assert repaired["content"] == [
                {"type": "text", "text": "[runtime discovery retried]\n[1] 42\n[done]"}
            ], repaired
            return json.loads(normalize_process_diagnostic(json.dumps(client.finish())))


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_refreshes_failed_inspection_evidence(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        python = root / "selected-python"
        attempts = root / "attempts"
        environment = without_r(root)
        environment["RETICULATE_PYTHON"] = str(python)
        environment["UV_TOOL_DIR"] = str(root)

        def break_inspection(evidence: str) -> None:
            python.write_text(
                "#!/bin/sh\nprintf 'probe\\n' >> " + shlex.quote(str(attempts)) + "\n"
                "printf '%s\\n' " + shlex.quote(evidence) + " >&2\nexit 17\n"
            )
            python.chmod(0o755)

        break_inspection("initial setup is broken")
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "cache=host",
                *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
            ),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            failure = client.send(python="raise AssertionError('must not run')")
            assert "initial setup is broken" in str(failure), failure
            break_inspection("repair is still incomplete")
            assert client.send() == failure
            refreshed = client.send(control="restart")
            assert refreshed.get("isError"), refreshed
            assert "repair is still incomplete" in str(refreshed), refreshed
            assert "initial setup is broken" not in str(refreshed), refreshed
            assert attempts.read_text().splitlines() == ["probe", "probe"]
            assert client.send() == refreshed
            transcript, stderr = client.finish_with_standard_error(
                expected_exit_status=1
            )
            assert "repair is still incomplete" in stderr, stderr
            return transcript + [{"stderr": stderr, "inspection_attempts": 2}]


@requires(POSIX, command("uv"), command("rustc"))
@executions(DIRECT, SANDBOXED)
def test_retry_discards_failed_cell_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with discovery_environment() as (environment, discovery):
        root = discovery.root
        expose_uv(root)
        environment.update(managed_python_environment(root))
        environment["UV_TOOL_DIR"] = str(root)
        with McpClient(
            binary, execution.serve("-c", "cache=host"), environment, root
        ) as client:
            discovery.wait("initial discovery is blocked before cell admission")
            client.initialize_and_list_tools()
            pending = client.start_send(
                python="rejected_cell = True",
                requirements={"action": "set", "python": ["six"]},
            )
            wait_for_send_admission(client)
            discovery.release()
            # Keep the accepted cell's response owner until discovery fails;
            # a readiness inspection can otherwise drain its shared output.
            client.receive(pending)
            cell_failure = pending["result"]
            assert cell_failure.get("isError"), cell_failure
            assert "fixture R discovery failed" in str(cell_failure), cell_failure
            failure = client.send(requirements={"action": "get"})
            assert failure.get("isError"), failure
            assert "fixture R discovery failed" in str(failure), failure
            discovery.executable.unlink()
            client.expect("\n[idle]", control="restart")
            inspection = client.send(requirements={"action": "get"})
            assert not inspection.get("isError"), inspection
            retained = inspection["structuredContent"]["requirements"]
            assert retained["python"] == [
                "numpy",
                "pandas",
                "matplotlib",
                "plotnine",
                "duckdb",
            ], retained
            assert retained["duckdb"] == ["icu", "json", "sqlite"], retained
            client.expect(
                "42\n",
                python="assert 'rejected_cell' not in globals(); answer = 42; answer",
            )
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@contextmanager
def retry_inspection(
    binary: Path, execution: Execution, *, interruptible: bool = False
) -> Iterator[tuple[McpClient, FifoCheckpoint, FifoCheckpoint, Path]]:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        python, site = isolated_python(root)
        python.unlink()
        attempts = root / "attempts"
        mode = root / "inspection-mode"
        mode.write_text("interrupt" if interruptible else "block")
        environment = without_r(root)
        environment["RETICULATE_PYTHON"] = str(python)
        environment["UV_TOOL_DIR"] = str(root)
        with (
            closing(FifoCheckpoint.create(root / "reached")) as reached,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                execution.serve(
                    "-c",
                    "cache=host",
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
                ),
                environment,
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            assert client.send().get("isError")
            (site / "sitecustomize.py").write_text(
                # fmt: python
                code(f"""
                    import os
                    import signal
                    import sys
                    from pathlib import Path

                    if sys.argv[0] == "-c":
                        with Path({str(attempts)!r}).open("a") as attempts:
                            attempts.write("probe\\n")
                        mode = Path({str(mode)!r}).read_text()
                        if mode == "interrupt":
                            signal.pthread_sigmask(signal.SIG_BLOCK, {{signal.SIGINT}})
                        if mode != "ready":
                            with Path({str(reached.path)!r}).open("wb", buffering=0) as reached:
                                assert reached.write(b"1") == 1
                            if mode == "interrupt":
                                assert signal.sigwait({{signal.SIGINT}}) == signal.SIGINT
                                os._exit(130)
                            with Path({str(release.path)!r}).open("rb", buffering=0) as release:
                                assert release.read(1) == b"1"
                    """)
            )
            python.symlink_to(sys.executable)
            yield client, reached, release, attempts


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_retry_stdin_reaches_early_input_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with retry_inspection(binary, execution) as (client, reached, release, attempts):
        pending = client.start_send(
            control="restart", stdin="repaired input\n", timeout_ms=10_000
        )
        reached.wait("stdin-bearing restart owns the shared retry")
        client.send(python="answer = input('retry> '); print(answer)", timeout_ms=0)
        assert without_elapsed(last_tool_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        release.release()
        client.response_timeout = 15
        deadline = client.response_deadline()
        try:
            client.receive(pending)
        except TimeoutError:
            # Release the blocked evaluation before reporting the regression.
            interrupt = client.start_send(control="interrupt")
            client.receive_many([pending, interrupt])
            raise
        wait_for_evaluation_output(
            client,
            '[input requested: "retry> "]\nrepaired input\n',
            "retry input reaches the admitted cell",
            received=pending,
            completion_timeout_seconds=max(0, deadline - time.monotonic()),
        )
        assert attempts.read_text().splitlines() == ["probe"]
        client.expect("'repaired input'\n", python="answer")
        return json.loads(
            json.dumps(client.finish()).replace(str(attempts.parent), "<workspace>")
        )


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_requirements_retry_stdin_reaches_early_input_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "python3").symlink_to(sys.executable)
        fixture, reached, release = checkpoint_uv_environment(root, "run")
        environment = without_r(root)
        environment.update(
            {
                name: value
                for name, value in fixture.items()
                if name.startswith("MCP_CONSOLE_TEST_") or name == "UV_TOOL_DIR"
            }
        )
        environment.update(managed_python_environment(root))
        with (
            closing(reached),
            closing(release),
            McpClient(
                binary, execution.serve("-c", "cache=host"), environment, root
            ) as client,
        ):
            client.initialize_and_list_tools()
            failure = client.send()
            assert failure.get("isError"), failure
            assert "require `uv` on PATH" in str(failure), failure
            (root / "uv").symlink_to(fixture["RETICULATE_UV"])
            pending = client.start_send(
                control="restart",
                requirements={"action": "reset"},
                stdin="repaired input\n",
                # Reset prepares defaults before the worker consumes queued input.
                timeout_ms=int(client.response_timeout * 1_000),
            )
            reached.wait("requirements-bearing restart owns the shared retry")
            client.send(python="answer = input('retry> '); print(answer)", timeout_ms=0)
            assert without_elapsed(last_tool_text(client)) == (
                "\n[running; poll with an empty send]"
            )
            release.release()
            deadline = client.response_deadline()
            try:
                client.receive(pending)
            except TimeoutError:
                # Release the blocked evaluation before reporting the regression.
                interrupt = client.start_send(control="interrupt")
                client.receive_many([pending, interrupt])
                raise
            wait_for_evaluation_output(
                client,
                '[input requested: "retry> "]\nrepaired input\n',
                "requirements retry input reaches the admitted cell",
                received=pending,
                completion_timeout_seconds=max(0, deadline - time.monotonic()),
            )
            client.expect("'repaired input'\n", python="answer")
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_cancelled_restart_shares_retry_and_preserves_next_cell(
    binary: Path, execution: Execution
) -> Transcript:
    with retry_inspection(binary, execution) as (client, reached, release, attempts):
        pending = client.start_send(control="restart", python="cancelled_cell = True")
        reached.wait("explicit restart owns Python inspection")
        # Completed public observations prove the callers have joined the
        # blocked attempt before cancellation and repair are released.
        for _ in range(2):
            result = client.send(control="restart", timeout_ms=0)
            assert result["content"] == [
                {
                    "type": "text",
                    "text": "\n[phase: dependency preparation]\n[worker starting]",
                }
            ], result
        timed_out = client.send(
            control="restart", python="timed_out_cell = True", timeout_ms=0
        )
        assert timed_out == {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "[worker starting]\n[phase: dependency preparation]\n"
                        "startup is pending; control was not applied and cell was not run"
                    ),
                }
            ],
            "isError": True,
        }, timed_out
        client.notify("notifications/cancelled", requestId=pending["id"])
        client.request("ping")
        (journal,) = attempts.parent.glob(
            ".agents/console/sessions/*/capture/events.jsonl"
        )

        def cancellation_recorded() -> Path | None:
            # Cancellation suppresses the wire response. Its recorded outcome
            # proves the wait ended before shared inspection is released.
            events = [
                json.loads(line)
                for line in journal.read_text().rpartition("\n")[0].splitlines()
            ]
            call = next(
                event
                for event in events
                if event["event"] == "tool_call"
                and event["request_id"] == pending["id"]
            )
            for event in events:
                if (
                    event["event"] == "tool_result"
                    and event["call_id"] == call["call_id"]
                ):
                    assert event["error"] == {
                        "code": -32603,
                        "message": "request cancelled; poll with an empty send for any admitted cell",
                    }, event
                    return journal
            return None

        wait_for_checkpoint(
            cancellation_recorded,
            "cancelled restart stops waiting before shared inspection completes",
            root=journal.parent,
            client=client,
        )
        next_cell = client.start_send(
            # fmt: python
            python=code("""
                assert "cancelled_cell" not in globals()
                assert "timed_out_cell" not in globals()
                answer = 42
                answer
                """)
        )
        wait_for_send_admission(client)
        release.release()
        client.receive(next_cell)
        assert next_cell["result"]["content"] == [{"type": "text", "text": "42\n"}], (
            next_cell
        )
        assert attempts.read_text().splitlines() == ["probe"]
        assert client.send(python="answer + 1")["content"] == [
            {"type": "text", "text": "43\n"}
        ]
        return json.loads(
            json.dumps(client.finish()).replace(str(attempts.parent), "<workspace>")
        )


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_cancelled_retry_preparation_withholds_bundled_cell(
    binary: Path, execution: Execution
) -> Transcript:
    return cancelled_retry_preparation(binary, execution, with_cell=True)


@requires(POSIX, command("uv"))
@executions(DIRECT, SANDBOXED)
@execution_snapshots
def test_cancelled_retry_preparation_withholds_stdin_only(
    binary: Path, execution: Execution
) -> Transcript:
    return cancelled_retry_preparation(binary, execution, with_cell=False)


def cancelled_retry_preparation(
    binary: Path, execution: Execution, *, with_cell: bool
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory).resolve()
        (root / "python3").symlink_to(sys.executable)
        fixture, reached, release = checkpoint_uv_environment(root, "six")
        environment = without_r(root)
        environment.update(
            {
                name: value
                for name, value in fixture.items()
                if name.startswith("MCP_CONSOLE_TEST_") or name == "UV_TOOL_DIR"
            }
        )
        environment.update(managed_python_environment(root))
        with (
            closing(reached),
            closing(release),
            McpClient(
                binary,
                execution.serve(
                    "-c",
                    "cache=host",
                    *(("--writable-root", str(root)) if execution == SANDBOXED else ()),
                ),
                environment,
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            failure = client.send()
            assert failure.get("isError"), failure
            assert "require `uv` on PATH" in str(failure), failure
            (root / "uv").symlink_to(fixture["RETICULATE_UV"])
            following_cell: dict[str, str] = {}
            if with_cell:
                # fmt: python
                following_cell["python"] = code("""
                    from pathlib import Path

                    Path("cancelled-cell").write_text("ran")
                    print("cancelled cell ran")
                    """)
            pending = client.start_send(
                control="restart",
                requirements={"action": "set", "python": ["six"]},
                stdin="cancelled input\n",
                timeout_ms=int(client.response_timeout * 1_000),
                **following_cell,
            )
            reached.wait(
                "changed retry requirements are preparing before input or cell admission",
                # Retry can prepare defaults before reaching the changed resolver.
                timeout=client.response_timeout,
            )
            client.notify("notifications/cancelled", requestId=pending["id"])
            client.request("ping")
            (journal,) = root.glob(".agents/console/sessions/*/capture/events.jsonl")

            def cancellation_recorded() -> Path | None:
                events = [
                    json.loads(line)
                    for line in journal.read_text().rpartition("\n")[0].splitlines()
                ]
                call = next(
                    event
                    for event in events
                    if event["event"] == "tool_call"
                    and event["request_id"] == pending["id"]
                )
                for event in events:
                    if (
                        event["event"] == "tool_result"
                        and event["call_id"] == call["call_id"]
                    ):
                        assert event["error"] == {
                            "code": -32603,
                            "message": "request cancelled; poll with an empty send for any admitted cell",
                        }, event
                        return journal
                return None

            wait_for_checkpoint(
                cancellation_recorded,
                "retry cancellation is recorded while changed preparation is gated",
                root=journal.parent,
                client=client,
            )
            release.release()
            poll_start = len(client.transcript)
            cancellation_text = (
                "[runtime discovery retried]\nrequest cancelled before cell admission; cell was not run"
                if with_cell
                else "[runtime discovery retried]\n[request cancelled before stdin admission]"
            )

            def retry_finished() -> Path | None:
                # A committed declaration does not mean the cancelled control
                # owner has published its recovered response. Consume that exact
                # result before the next cell can capture it as idle output.
                events = [
                    json.loads(line)
                    for line in journal.read_text().rpartition("\n")[0].splitlines()
                ]
                if not any(
                    event["event"] == "requirements_selected"
                    and event["action"] == "set"
                    and event["snapshot"]["requirements"]["python"] == ["six"]
                    for event in events
                ):
                    return None
                result = client.send()
                return (
                    journal
                    if result.get("isError")
                    and last_result_text(client) == cancellation_text
                    else None
                )

            wait_for_checkpoint(
                retry_finished,
                "retry cancellation result is recovered before further admission",
                root=journal.parent,
                client=client,
            )
            client.transcript[poll_start:] = [client.transcript[-1]]
            assert not (root / "cancelled-cell").exists(), last_result_text(client)
            retry_result = client.transcript[-1]["result"]
            retry_text = last_result_text(client)
            retained = client.send(requirements={"action": "get"})
            assert retained["structuredContent"]["requirements"]["python"] == ["six"], (
                retained
            )
            if not with_cell:
                client.send(python="answer = input('fresh> '); print(answer)")
                assert last_tool_text(client) == (
                    '[input requested: "fresh> "]\n[waiting for stdin]'
                ), last_tool_text(client)
                client.expect("fresh input\n", stdin="fresh input\n")
                client.expect("'fresh input'\n", python="answer")
            client.expect(
                "42\n",
                # fmt: python
                python=code("""
                    import six
                    from pathlib import Path

                    assert not Path("cancelled-cell").exists()
                    answer = 42
                    answer
                    """),
            )
            client.expect("43\n", python="answer + 1")
            assert retry_result.get("isError"), retry_result
            assert retry_text == cancellation_text, retry_text
            return json.loads(
                json.dumps(client.finish()).replace(str(root), "<workspace>")
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_interrupted_retry_can_be_restarted(
    binary: Path, execution: Execution
) -> Transcript:
    with retry_inspection(binary, execution, interruptible=True) as (
        client,
        reached,
        _,
        attempts,
    ):
        pending = client.start_send(control="restart", python="rejected_cell = True")
        reached.wait("retried Python inspection can receive interrupt")
        interrupt = client.start_send(control="interrupt")
        client.receive_many([pending, interrupt])
        observation = interrupt["result"]
        assert not observation.get("isError"), observation
        assert observation["content"] in (
            [{"type": "text", "text": "[worker starting]"}],
            [{"type": "text", "text": "\n[phase: startup]\n[worker starting]"}],
        ), observation
        # The phase announcement depends on which response observes startup
        # first; the interrupted inspection and subsequent retry settle below.
        observation["content"] = [{"type": "text", "text": "[worker starting]"}]
        failure = pending["result"]
        assert failure == {
            "content": [
                {
                    "type": "text",
                    "text": "selected Python inspection failed (exit status: 130): ",
                }
            ],
            "isError": True,
        }, failure
        assert client.send() == failure
        (attempts.parent / "inspection-mode").write_text("ready")
        repaired = client.send(
            control="restart", python="assert 'rejected_cell' not in globals(); 42"
        )
        assert repaired["content"] == [
            {"type": "text", "text": "[runtime discovery retried]\n42\n[done]"}
        ], repaired
        assert attempts.read_text().splitlines() == ["probe", "probe"]
        return json.loads(
            json.dumps(client.finish()).replace(str(attempts.parent), "<workspace>")
        )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_connection_closure_cancels_retried_inspection(
    binary: Path, execution: Execution
) -> Transcript:
    with retry_inspection(binary, execution) as (client, reached, _, attempts):
        client.start_send(control="restart")
        reached.wait("retry owns inspection before connection closure")
        client.stdin.close()
        assert client.process.wait(timeout=client.shutdown_timeout) == 0
        assert client.stderr.read() == ""
        assert attempts.read_text().splitlines() == ["probe"]
        return [{"connection_closure_retired_retried_inspection": True}]


if __name__ == "__main__":
    run_this_suite(__file__)
