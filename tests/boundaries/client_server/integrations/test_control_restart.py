"""Integrator restart retains MCP delivery and settles private evaluations."""

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from boundaries.client_server.python.test_without_r import selected_environment
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.progress import RUNNING, without_elapsed
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


def text(result: dict) -> str:
    return "".join(block.get("text", "") for block in result["content"])


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_retains_uncollected_mcp_output(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-restart-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            selected_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="sentinel = 42")
            with (
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                observer.request(op="subscribe", format="events")
                json.loads(observer.reader.readline(timeout=30))
                for mode in ("running", "complete"):
                    with closing(
                        FifoCheckpoint.create(root / f"release-{mode}")
                    ) as release:
                        # fmt: python
                        source = code(f"""
                            import threading
                            with open({str(release.path)!r}, "rb", buffering=0) as gate:
                                gate.read(1)
                            try:
                                print("retained {mode} MCP output", flush=True)
                                if {mode == "running"}:
                                    threading.Event().wait()
                            except KeyboardInterrupt:
                                pass
                            """)
                        pending = client.send(python=source, timeout_ms=0)
                        assert without_elapsed(text(pending)) == RUNNING, pending
                        release.release()
                        while True:
                            snapshot = json.loads(observer.reader.readline(timeout=30))[
                                "result"
                            ]
                            evaluation = next(
                                (
                                    item
                                    for item in snapshot["evaluations"]
                                    if item["source"] == source
                                ),
                                None,
                            )
                            if evaluation is None:
                                continue
                            output_seen = f"retained {mode} MCP output" in "".join(
                                item.get("text", "")
                                for item in snapshot["events"]
                                if item["evaluation_id"] == evaluation["id"]
                            )
                            if output_seen and (
                                mode == "running" or evaluation["state"] == "complete"
                            ):
                                break
                        restarted = control.request(op="restart")["result"]
                        assert restarted == {
                            "accepted": True,
                            "worker_id": snapshot["worker_id"] + 1,
                        }, restarted
                        collected = client.expect(None)
                        assert (
                            text(collected).count(f"retained {mode} MCP output") == 1
                        ), collected
                        assert "[starting new worker]" in text(collected), collected
                        client.expect("\n[idle]")
                client.expect("False\n", python="print('sentinel' in globals())")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


@requires(POSIX)
@executions(DIRECT, SANDBOXED)
def test_restart_settles_private_active_evaluation(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-restart-"
    ) as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary, execution.serve(*arguments), selected_environment(root), root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="pass")
            # fmt: python
            source = code(f"""
                print("private output", flush=True)
                with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                    gate.write(b"1")
                with open({str(release.path)!r}, "rb", buffering=0) as gate:
                    gate.read(1)
                """)
            with (
                ControlClient(endpoint) as evaluator,
                ControlClient(endpoint) as control,
            ):
                pending = evaluator.start(
                    op="evaluate",
                    language="python",
                    code=source,
                    busy="wait",
                    wait_ms=30000,
                )
                try:
                    entered.wait(client=client)
                    restarted = control.request(op="restart")["result"]
                    assert restarted["accepted"] and "content" not in restarted, (
                        restarted
                    )
                    stopped = evaluator.receive(pending)["result"]
                    assert stopped["isError"], stopped
                    assert text(stopped).startswith("private output\n"), stopped
                    assert "stopped by session restart request" in text(stopped), (
                        stopped
                    )
                    collected = client.expect(None)
                    assert "private output" not in text(collected), collected
                    client.expect("\n[idle]")
                    client.expect(
                        "replacement usable\n", python="print('replacement usable')"
                    )
                    transcript = normalize_recording(client.finish(), root)
                finally:
                    release.release()
            return TranscriptWithCompanions(
                transcript,
                {
                    "control.yaml": control.transcript,
                    "evaluation.yaml": normalize_recording(evaluator.transcript, root),
                },
            )


if __name__ == "__main__":
    run_this_suite(__file__)
