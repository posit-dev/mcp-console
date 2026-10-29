"""Typed integrator output retains worker streams independently of MCP delivery."""

import json
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from boundaries.client_server.python.test_without_r import selected_environment
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import DIRECT, Execution, executions
from support.normalization import code, normalize_recording
from support.records import Transcript
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(DIRECT)
def test_retains_typed_output_after_delivery(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-output-") as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        endpoint = root / "control.sock"
        with (
            closing(FifoCheckpoint.create(root / "release")) as release,
            closing(FifoCheckpoint.create(root / "human-release")) as human_release,
            McpClient(
                binary,
                execution.serve("--control-socket", str(endpoint)),
                selected_environment(root),
                root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(python="pass")
            with (
                ControlClient(endpoint) as observer,
                ControlClient(endpoint) as control,
            ):
                subscribed = observer.request(op="subscribe", format="events")
                assert subscribed.get("result") == {"subscribed": True}, subscribed
                json.loads(observer.reader.readline(timeout=30))
                # fmt: python
                source = code(f"""
                    import os, sys
                    print("partial", end="", flush=True)
                    with open({str(release.path)!r}, "rb", buffering=0) as gate:
                        gate.read(1)
                    print(" console", flush=True)
                    sys.stderr.write("diagnostic\\n")
                    sys.stderr.flush()
                    raise ValueError("typed failure")
                    """)
                pending = client.start_send(python=source, timeout_ms=60000)
                try:
                    while True:
                        event = json.loads(observer.reader.readline(timeout=30))
                        assert event["event"] == "output", event
                        snapshot = event["result"]
                        if any(
                            item.get("text") == "partial" for item in snapshot["events"]
                        ):
                            break
                    evaluation = next(
                        item
                        for item in snapshot["evaluations"]
                        if item["source"] == source
                    )
                    assert (
                        evaluation["origin"] == "mcp"
                        and evaluation["language"] == "python"
                    ), evaluation
                    assert evaluation["state"] == "running" and evaluation["started"], (
                        evaluation
                    )
                    release.release()
                    client.receive(pending)
                    complete = control.request(op="output")["result"]
                    metadata = next(
                        item
                        for item in complete["evaluations"]
                        if item["id"] == evaluation["id"]
                    )
                    assert metadata["state"] == "complete", metadata
                    events = [
                        item
                        for item in complete["events"]
                        if item["evaluation_id"] == evaluation["id"]
                    ]
                    assert {"output", "diagnostic", "execution_error"} <= {
                        item["type"] for item in events
                    }, events
                    assert [item["sequence"] for item in events] == sorted(
                        item["sequence"] for item in events
                    ), events
                    assert "partial console\n" == "".join(
                        item["text"] for item in events if item["type"] == "output"
                    ), events
                    assert (
                        next(
                            item for item in events if item["type"] == "execution_error"
                        )["error"]["ename"]
                        == "ValueError"
                    ), events
                    assert control.request(op="output")["result"] == complete
                    # fmt: python
                    human_source = code(f"""
                        print("human")
                        os.write(1, b"raw stdout\\n")
                        os.write(2, b"raw stderr\\n")
                        with open({str(human_release.path)!r}, "rb", buffering=0) as gate:
                            gate.read(1)
                        """)
                    human = control.start(
                        op="evaluate",
                        id="human-cell",
                        language="python",
                        code=human_source,
                        busy="wait",
                        wait_ms=30000,
                        include_output=True,
                    )
                    while True:
                        observed = json.loads(observer.reader.readline(timeout=30))[
                            "result"
                        ]
                        if {"stdout", "stderr"} <= {
                            item["type"] for item in observed["events"]
                        }:
                            break
                    human_release.release()
                    direct = control.receive(human)["result"]
                    assert {"stdout", "stderr"} <= {
                        item["type"] for item in direct["output"]["events"]
                    }, direct
                    assert (
                        direct["output"]["evaluations"][0]["request_id"] == "human-cell"
                    ), direct
                    assert (
                        direct["output"]["evaluations"][0]["id"]
                        == direct["evaluation_id"]
                    ), direct
                    private = control.request(
                        op="evaluate",
                        language="python",
                        code="print('private inspection')",
                        execution="immediate",
                        record=False,
                        include_output=True,
                    )
                    assert result_text(private) == "private inspection\n", private
                    assert private["result"]["output"]["events"], private
                    assert "private inspection" not in json.dumps(
                        control.request(op="output")
                    )
                    client.expect("\n[idle]")
                    return normalize_recording(client.finish(), root)
                finally:
                    release.release()
                    human_release.release()


@requires(POSIX)
@executions(DIRECT)
def test_bounds_history_and_replaces_worker(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-output-") as directory:
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
            client.expect(python="pass")
            with ControlClient(endpoint) as control:
                initial = control.request(op="output")["result"]
                hidden = control.request(
                    op="evaluate",
                    language="python",
                    code="print('hidden ordinary inspection')",
                    record=False,
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(hidden) == "hidden ordinary inspection\n", hidden
                assert control.request(op="output")["result"] == initial
                source = "#" + "s" * 16000 + "\nprint('x' * 262144)"
                result = control.request(
                    op="evaluate",
                    language="python",
                    code=source,
                    busy="wait",
                    wait_ms=30000,
                    include_output=True,
                )["result"]
                large = result["output"]
                assert large["omitted_bytes"] > 0, large
                assert large["evaluations"][0]["source_truncated"], large
                assert len(large["evaluations"][0]["source"].encode()) <= 8192, large
                assert (
                    sum(len(item.get("text", "").encode()) for item in large["events"])
                    <= 65536
                ), large
                overflow = control.request(
                    op="evaluate",
                    language="python",
                    code="for i in range(600): print(i)",
                    busy="wait",
                    wait_ms=30000,
                    include_output=True,
                )["result"]["output"]
                assert len(overflow["events"]) <= 512, overflow
                assert overflow["omitted_events"] > large["omitted_events"], overflow
                for index in range(40):
                    control.request(
                        op="evaluate",
                        language="python",
                        code=f"print({index})",
                        busy="wait",
                        wait_ms=30000,
                    )
                bounded = control.request(op="output")["result"]
                assert len(bounded["evaluations"]) == 32, bounded
                assert len(bounded["events"]) <= 512, bounded
                assert bounded["omitted_events"] > 0, bounded
                ids = {item["id"] for item in bounded["evaluations"]}
                assert all(
                    item["evaluation_id"] is None or item["evaluation_id"] in ids
                    for item in bounded["events"]
                ), bounded
                client.expect(
                    "[worker stopped: in-memory state lost]\n[starting new worker]\n[idle]",
                    control="restart",
                )
                client.expect("new worker\n", python="print('new worker')")
                replacement = control.request(op="output")["result"]
                assert replacement["worker_id"] > bounded["worker_id"], replacement
                assert not ids.intersection(
                    item["id"] for item in replacement["evaluations"]
                ), replacement
                assert replacement["next_sequence"] > bounded["next_sequence"], (
                    replacement
                )
                return normalize_recording(client.finish(), root)


@requires(POSIX, R)
@executions(DIRECT)
def test_retains_r_diagnostics_and_images(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-output-") as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="invisible(NULL)")
            with ControlClient(endpoint) as control:
                result = control.request(
                    op="evaluate",
                    language="r",
                    code='cat("console\\n"); message("diagnostic"); plot(1:3)',
                    busy="wait",
                    wait_ms=30000,
                    include_output=True,
                )["result"]
                evaluation = result["output"]["evaluations"][0]
                assert (
                    evaluation["language"] == "r"
                    and evaluation["origin"] == "integrator"
                ), evaluation
                events = result["output"]["events"]
                assert {"output", "diagnostic", "image"} <= {
                    item["type"] for item in events
                }, events
                image = next(item for item in events if item["type"] == "image")
                content = next(
                    item for item in result["content"] if item["type"] == "image"
                )
                assert (
                    image["data"] == content["data"]
                    and image["mimeType"] == content["mimeType"]
                )
                client.expect("\n[idle]")
                return normalize_recording(client.finish(), root)


@requires(POSIX)
@executions(DIRECT)
def test_private_output_does_not_depend_on_recording(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory(dir="/tmp", prefix="console-output-") as directory:
        root = Path(directory)
        (root / "python3").symlink_to(sys.executable)
        (root / ".agents/console").mkdir(parents=True)
        (root / ".agents/console/sessions").write_text("occupied", encoding="utf-8")
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            execution.serve("--control-socket", str(endpoint)),
            selected_environment(root),
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="pass")
            with ControlClient(endpoint) as control:
                initial = control.request(op="output")["result"]
                result = control.request(
                    op="evaluate",
                    language="python",
                    code="print('private despite recording failure')",
                    record=False,
                    busy="wait",
                    wait_ms=30000,
                )
                assert result_text(result) == "private despite recording failure\n", (
                    result
                )
                assert control.request(op="output")["result"] == initial
                client.expect("\n[idle]")
                transcript, stderr = client.finish_with_standard_error()
                assert stderr.count("transcript recording disabled:") == 1, stderr
                return normalize_recording(transcript, root)


if __name__ == "__main__":
    run_this_suite(__file__)
