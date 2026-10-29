"""Human requirements use normal preparation without claiming Console output."""

import json
import tempfile
import sys
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import (
    code,
    normalize_recording,
    normalize_python_resolution_error,
)
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, R, requires
from support.resolvers import recording_uv_environment
from support.suites import run_this_suite


def text(result: dict) -> str:
    return "".join(block.get("text", "") for block in result["content"])


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_prepares_live_requirements_without_replacing_session(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-prepare-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="kept_r <- new.env(); kept_r$value <- 42L")
            client.expect(python="kept_python = {'value': 42}")
            with ControlClient(endpoint) as control:
                worker = control.request(op="output")["result"]["worker_id"]
                prepared = control.request(
                    op="prepare",
                    requirements={
                        "r": ["praise"],
                        "python": ["more-itertools"],
                        "duckdb": ["json"],
                    },
                )["result"]
                assert prepared["status"] == "prepared", prepared
                assert prepared["worker_id"] == worker, prepared
                assert prepared["prepared"], prepared
                assert "praise" in prepared["requirements"]["r"], prepared
                assert "more-itertools" in prepared["requirements"]["python"], prepared
                assert "json" in prepared["requirements"]["duckdb"], prepared
                inspected = control.request(op="requirements")["result"]
                assert {key: prepared[key] for key in inspected} == inspected, prepared
                client.expect(
                    r="stopifnot(kept_r$value == 42L, requireNamespace('praise', quietly = TRUE))"
                )
                client.expect(
                    python="import more_itertools; assert kept_python['value'] == 42; assert more_itertools.one([42]) == 42"
                )
                client.expect("\n[idle]")
                for action in ("get", "set", "reset"):
                    rejected = control.request(
                        op="prepare", requirements={"action": action}
                    )
                    assert rejected["error"]["code"] == "invalid_request", rejected
                transcript = client.finish()
            return TranscriptWithCompanions(
                transcript, {"control.yaml": control.transcript}
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_busy_preparation_preserves_private_evaluation(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-prepare-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        arguments = ["--control-socket", str(endpoint)]
        if execution == SANDBOXED:
            arguments += ["--writable-root", str(root)]
        with (
            closing(FifoCheckpoint.create(root / "entered")) as entered,
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary, execution.serve(*arguments), current_directory=root
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="pass")
            with (
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as evaluator,
            ):
                baseline = control.request(op="requirements")["result"]
                # fmt: python
                source = code(f"""
                    print("private evaluation output", flush=True)
                    with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                        gate.write(b"1")
                    with open({str(release.path)!r}, "rb", buffering=0) as gate:
                        gate.read(1)
                    """)
                pending = evaluator.start(op="evaluate", language="python", code=source)
                try:
                    entered.wait(client=client)
                    busy = control.request(
                        op="prepare", requirements={"python": ["more-itertools"]}
                    )["result"]
                    assert busy["status"] == "busy", busy
                    assert {key: busy[key] for key in baseline} == baseline, busy
                finally:
                    release.release()
                completed = evaluator.receive(pending)["result"]
                assert text(completed) == "private evaluation output\n", completed
                client.expect("\n[idle]")
                transcript = client.finish()
            return TranscriptWithCompanions(
                transcript,
                {
                    "control.yaml": control.transcript,
                    "evaluation.yaml": normalize_recording(evaluator.transcript, root),
                },
            )


@requires(POSIX, R)
@executions(DIRECT, SANDBOXED)
def test_failed_preparation_retains_manifest_state_and_mcp_output(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-prepare-"
    ) as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        candidate = "mcp-console-control-prepare-failure"
        environment, _ = recording_uv_environment(root, fail_requirement=candidate)
        with (
            closing(FifoCheckpoint.create(root / "release")) as release,
            McpClient(
                binary,
                execution.serve("--control-socket", str(endpoint), "-c", "cache=host"),
                environment,
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="kept = {'value': 42}")
            executable_result = client.send(python="import sys; print(sys.executable)")
            executable = text(executable_result).strip()
            assert Path(executable).is_absolute(), executable
            executable_result["content"][0]["text"] = "<running Python>\n"
            with (
                ControlClient(endpoint) as control,
                ControlClient(endpoint) as observer,
            ):
                baseline = control.request(op="requirements")["result"]
                observer.request(op="subscribe", format="events")
                json.loads(observer.reader.readline(timeout=30))
                # fmt: python
                source = code(f"""
                    import os
                    import threading

                    def background_output():
                        with open({str(release.path)!r}, "rb", buffering=0) as gate:
                            gate.read(1)
                        os.write(1, b"uncollected MCP output\\n")

                    threading.Thread(target=background_output).start()
                    """)
                client.expect(python=source)
                release.release()
                while True:
                    snapshot = json.loads(observer.reader.readline(timeout=30))[
                        "result"
                    ]
                    if any(
                        "uncollected MCP output" in event.get("text", "")
                        for event in snapshot["events"]
                    ):
                        break
                failed = control.request(
                    op="prepare", requirements={"python": [candidate]}
                )["result"]
                assert failed["status"] == "failed", failed
                assert "synthetic uv failure" in failed["message"], failed
                assert "uncollected MCP output" not in failed["message"], failed
                assert {key: failed[key] for key in baseline} == baseline, failed
                failed["message"] = normalize_python_resolution_error(
                    failed["message"], executable=executable
                )
                collected = client.send()
                assert text(collected).count("uncollected MCP output") == 1, collected
                for block in collected["content"]:
                    if (
                        block["type"] == "text"
                        and "synthetic uv failure" in block["text"]
                    ):
                        block["text"] = normalize_python_resolution_error(
                            block["text"], executable=executable
                        )
                client.expect("\n[idle]")
                client.expect("42\n", python="print(kept['value'])")
                transcript = normalize_recording(client.finish(), root)
            return TranscriptWithCompanions(
                transcript,
                {"control.yaml": normalize_recording(control.transcript, root)},
            )


if __name__ == "__main__":
    run_this_suite(__file__)
