#!/usr/bin/env -S uv run --script

import base64
import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import (
    assert_result_content,
    last_tool_text,
    wait_for_evaluation_output,
)
from support.checkpoints import FifoCheckpoint, wait_for_path
from support.evidence import compact_text
from support.execution import SANDBOXED, Execution, executions
from support.jupyter import jupyter_client, normalize_jupyter_paths
from support.normalization import code, normalize_recording
from support.progress import phase_progress, without_elapsed
from support.processes import capture_process_identity, kill_processes, live_processes
from support.records import Transcript
from support.requirements import JUPYTER, POSIX, PROCESS_EVENTS, requires
from support.suites import run_this_suite


@requires(JUPYTER)
@executions(SANDBOXED)
def test_ipc_kernel_with_explicit_socket_permissions(
    binary: Path, execution: Execution
) -> Transcript:
    with jupyter_client(binary, execution=execution) as (client, _):
        client.expect("42\n", code="6 * 7")
        return client.finish()


@requires(JUPYTER)
def test_persistent_state_streams_errors_and_recovery(binary: Path) -> Transcript:
    with jupyter_client(binary) as (client, workspace):
        schema = client.transcript[-1]["result"]["tools"][0]["inputSchema"]
        assert set(schema["properties"]) == {
            "code",
            "stdin",
            "control",
            "timeout_ms",
            "requirements",
        }
        requirements = schema["properties"]["requirements"]
        assert set(requirements["properties"]) == {"action"}
        assert requirements["properties"]["action"]["enum"] == ["get"]
        assert requirements["required"] == ["action"]
        inspected = client.send(requirements={"action": "get"})
        state = inspected["structuredContent"]
        assert json.loads(last_tool_text(client)) == state
        assert state["requirements"] == {
            "r": [],
            "python": [],
            "duckdb": [],
            "python_version": [],
            "exclude_newer": None,
        }, state
        assert state["runtime_requirements"] == {"r": [], "python": []}, state
        client.expect("42\n", code="answer = 40\nanswer + 2")
        large_source = "# " + "x" * (100 * 1024) + "\nanswer + 2"
        client.expect("42\n", code=large_source)
        client.transcript[-1]["send"]["code"] = compact_text(large_source, "x")
        client.expect(
            "stdout café\nstderr Ω\n",
            # fmt: python
            code=code("""
                import sys

                print("stdout café", flush=True)
                print("stderr Ω", file=sys.stderr, flush=True)
                """),
        )
        wait_for_evaluation_output(
            client,
            lambda output: "ValueError: intentional kernel error" in output,
            "ipykernel error reply",
            expected_error=False,
            completion_timeout_seconds=30,
            code='raise ValueError("intentional kernel error")',
        )
        client.expect("42\n", code="answer + 2")
        for arguments in (
            {"r": "1 + 1"},
            {"python": "1 + 1"},
            {"sql": "SELECT 1"},
            {"requirements": {"python": ["requests"]}},
        ):
            rejected = client.send(**arguments)
            assert rejected["isError"] is True, rejected
        client.expect("42\n", code="answer + 2")
        transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        (document,) = (recording / "sessions").glob("*.qmd")
        quarto = document.read_text(encoding="utf-8")
        assert "  jupyter-kernel: console-ipykernel\n" in quarto, quarto
        assert "execute:\n  eval: false\n" in quarto, quarto
        assert "\nir:\n" not in quarto and "ir render" not in quarto, quarto
        assert "```text\nanswer = 40\nanswer + 2\n```" in quarto, quarto
        assert large_source in quarto, quarto
        assert "```{code}" not in quarto, quarto
        transcript.append(
            {
                "recording": "Generic kernel source is retained in plain text Quarto fences."
            }
        )
        return transcript


@requires(JUPYTER)
def test_delayed_kernel_info_replies_survive_retries(binary: Path) -> Transcript:
    with jupyter_client(binary, kernel_fixture="jupyter_delayed_info.py") as (
        client,
        _,
    ):
        client.transcript[0]["scenario"] = (
            "A delayed readiness reply remains valid after another kernel_info request."
        )
        client.transcript[0]["fixture"] += (
            " A published IPythonKernel subclass constructs every readiness reply immediately, "
            "but delivers it only when the next request arrives. Normal IOPub idle messages "
            "are delivered immediately; no sleeps or adjusted deadlines are used."
        )
        client.expect("42\n", code="answer = 6 * 7\nanswer")
        client.expect("42\n", code="answer")
        return client.finish()


@requires(JUPYTER)
def test_tcp_startup_warning_preserves_output_and_state(binary: Path) -> Transcript:
    with jupyter_client(binary, transport="tcp") as (client, _):
        client.expect(
            "[IPKernelApp] WARNING | Kernel is running over TCP without encryption. "
            "All communication (including code and outputs) is sent in plain text and "
            "is susceptible to eavesdropping. Use IPC transport or launch with kernel "
            "manager-provisioned CurveZMQ keys to enable transport encryption.\n42\n",
            code="answer = 40\nanswer + 2",
        )
        client.expect("42\n", code="answer + 2")
        return client.finish()


@requires(JUPYTER)
def test_rich_output_and_prompted_input(binary: Path) -> Transcript:
    with jupyter_client(binary) as (client, _):
        encoded = (
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8"
            "/x8AAwMCAO+aSuoAAAAASUVORK5CYII="
        )
        client.send(
            # fmt: python
            code=code(f"""
                import base64
                from IPython.display import Image, display

                display(Image(data=base64.b64decode("{encoded}")))
                """),
        )
        assert_result_content(
            client,
            [base64.b64decode(encoded)],
            image_reference="the submitted one-pixel PNG",
        )
        wait_for_evaluation_output(
            client,
            '[input requested: "Name: "]\n[waiting for stdin]',
            "ipykernel input request",
            completion_timeout_seconds=30,
            code='name = input("Name: ")\nprint("hello", name)',
        )
        client.expect("hello Ada\n", stdin="Ada\n")
        client.expect("'Ada'\n", code="name")
        return client.finish()


@requires(JUPYTER)
def test_interrupt_and_restart_preserve_generation_ownership(
    binary: Path,
) -> Transcript:
    with jupyter_client(binary) as (client, workspace):
        client.transcript[0]["fixture"] += (
            " A file marker proves that an interruptible Python evaluation reached its gate."
        )
        client.expect("ready\n", code='answer = 42\nprint("ready")')
        client.send(
            # fmt: python
            code=code("""
                from pathlib import Path
                import threading

                try:
                    Path("interrupt-ready").touch()
                    threading.Event().wait()
                except KeyboardInterrupt:
                    print("interrupted")
                """),
            timeout_ms=0,
        )
        assert without_elapsed(last_tool_text(client)) == (
            "\n[running; poll with an empty send]"
        )
        wait_for_path(
            workspace / "interrupt-ready",
            "ipykernel reached its interruptible gate",
            client=client,
        )
        client.expect("interrupted\n", control="interrupt")
        client.expect("42\n", code="answer")
        restarted = client.send(control="restart")
        assert not restarted.get("isError"), restarted
        assert last_tool_text(client) == (
            "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]"
        )
        client.expect("False\n", code='"answer" in globals()')
        client.expect("42\n", code="6 * 7")
        return normalize_recording(client.finish(), workspace)


@requires(JUPYTER, POSIX, PROCESS_EVENTS)
def test_slow_kernel_startup_remains_pending_until_ready(binary: Path) -> Transcript:
    identities = []
    try:
        with (
            tempfile.TemporaryDirectory(prefix="jupyter-startup-gate-") as directory,
            closing(FifoCheckpoint.create(Path(directory) / "startup.fifo")) as gate,
            jupyter_client(binary, startup_gate=gate.path) as (client, workspace),
        ):
            client.transcript[0]["scenario"] = (
                "A live kernel remains in startup until its Jupyter channels are ready."
            )
            client.transcript[0]["fixture"] += (
                " A FIFO holds the acquired kernel before ipykernel initialization across a 1.5-second public poll."
            )
            try:
                client.send(code="answer = 42\nprint(answer)", timeout_ms=0)
                assert without_elapsed(last_tool_text(client)) == (
                    "\n[running; poll with an empty send]"
                )
                wait_for_path(
                    gate.path.with_suffix(".ready"),
                    "kernel process acquired before Jupyter readiness",
                    client=client,
                )
                pid = int(gate.path.with_suffix(".pid").read_text(encoding="utf-8"))
                identity = capture_process_identity(pid)
                identities.append(identity)
                client.send(timeout_ms=1_500)
                assert without_elapsed(last_tool_text(client)) == (
                    "\n[running; poll with an empty send]"
                )
                assert live_processes((identity,)) == [pid], (
                    "live startup process was mistaken for a failed kernel"
                )
                gate.release()
                client.expect("42\n")
                client.expect("42\n", code="answer")
                return normalize_jupyter_paths(
                    normalize_recording(client.finish(), workspace)
                )
            finally:
                gate.release()
    finally:
        kill_processes(identities)


@requires(JUPYTER, POSIX, PROCESS_EVENTS)
def test_interrupt_cancels_kernel_startup_and_reaps_process(binary: Path) -> Transcript:
    identities = []
    try:
        with (
            tempfile.TemporaryDirectory(prefix="jupyter-startup-gate-") as directory,
            closing(FifoCheckpoint.create(Path(directory) / "startup.fifo")) as gate,
            jupyter_client(binary, startup_gate=gate.path) as (client, workspace),
        ):
            client.transcript[0]["scenario"] = (
                "Interrupt cancels kernel startup before Jupyter readiness and discards the pending cell."
            )
            client.transcript[0]["fixture"] += (
                " A Python launcher records its PID and blocks on a FIFO before starting the released ipykernel."
            )
            try:
                evaluation = client.start_send(
                    # fmt: python
                    code=code("""
                        from pathlib import Path

                        Path("must-not-run").write_text("unexpected execution")
                        """),
                )
                wait_for_path(
                    gate.path.with_suffix(".ready"),
                    "kernel process acquired before Jupyter readiness",
                    client=client,
                )
                pid = int(gate.path.with_suffix(".pid").read_text(encoding="utf-8"))
                identity = capture_process_identity(pid)
                identities.append(identity)
                interrupt = client.start_send(control="interrupt", timeout_ms=3_000)
                client.receive_many([evaluation, interrupt])
                result = evaluation["result"]
                assert result["isError"] is True, result
                output = result["content"][0]["text"]
                assert "Jupyter startup interrupted" in output, output
                assert interrupt["result"]["isError"] is False, interrupt
                assert phase_progress(last_tool_text(client)) == "startup"
                assert without_elapsed(last_tool_text(client)) == (
                    "\n[running; poll with an empty send]"
                )
                assert live_processes((identity,)) == [], (
                    "interrupted startup leaked its kernel"
                )
                assert not (workspace / "must-not-run").exists(), (
                    "cancelled startup evaluated pending source"
                )
                return normalize_jupyter_paths(
                    normalize_recording(client.finish(), workspace)
                )
            finally:
                # Release before transport teardown even on the old broken
                # implementation, so the regression does not wait for readiness's timeout.
                gate.release()
    finally:
        kill_processes(identities)


@requires(JUPYTER, PROCESS_EVENTS)
def test_kernel_crash_recovery_and_process_retirement(binary: Path) -> Transcript:
    identities = []
    try:
        with jupyter_client(binary) as (client, workspace):
            client.transcript[0]["scenario"] = (
                "A kernel crash loses state; explicit restart recovers and reaps prior processes."
            )
            client.send(code="import os\nprint(os.getpid())")
            pid = int(last_tool_text(client).strip())
            original = capture_process_identity(pid)
            identities.append(original)
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<original kernel PID>\n"
            )
            result = client.send(control="restart")
            assert not result.get("isError"), result
            assert last_tool_text(client).endswith("[starting new worker]\n[idle]")
            assert live_processes((original,)) == [], (
                "normal restart leaked its old kernel"
            )

            client.send(code="import os\nprint(os.getpid())")
            pid = int(last_tool_text(client).strip())
            crashing = capture_process_identity(pid)
            identities.append(crashing)
            assert crashing != original
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<crashing kernel PID>\n"
            )
            result = client.send(code="os._exit(7)")
            assert result["isError"] is True, result
            text = result["content"][0]["text"]
            assert "Jupyter kernel exited with status 7" in text, text
            assert live_processes((crashing,)) == [], "crashed kernel was not reaped"
            result = client.send(control="restart")
            assert not result.get("isError"), result
            assert last_tool_text(client).endswith("[starting new worker]\n[idle]")
            client.expect("42\n", code="6 * 7")
            client.send(code="import os\nprint(os.getpid())")
            pid = int(last_tool_text(client).strip())
            replacement = capture_process_identity(pid)
            identities.append(replacement)
            assert replacement != crashing
            client.transcript[-1]["result"]["content"][0]["text"] = (
                "<replacement kernel PID>\n"
            )
            transcript = normalize_jupyter_paths(
                normalize_recording(client.finish(), workspace)
            )
            assert live_processes(identities) == [], (
                "server shutdown leaked a kernel process"
            )
            transcript.append(
                {
                    "processes": "Restart, crash, and shutdown each retired the kernel process."
                }
            )
            return transcript
    finally:
        kill_processes(identities)


if __name__ == "__main__":
    run_this_suite(__file__)
