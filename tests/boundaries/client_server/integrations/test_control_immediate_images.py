"""Immediate images retain their call identity without a foreground interval."""

import base64
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.integrations.test_control import ControlClient
from support.assertions import assert_result_content
from support.checkpoints import FifoCheckpoint
from support.client import McpClient
from support.execution import RUNTIME, SANDBOXED, Execution, executions
from support.normalization import code, normalize_recording
from support.previews import recording_events
from support.records import TranscriptWithCompanions
from support.requirements import POSIX, requires
from support.suites import run_this_suite


@requires(POSIX)
@executions(RUNTIME)
def test_retains_immediate_images_without_claiming_foreground_recording(
    binary: Path, execution: Execution
) -> TranscriptWithCompanions:
    with tempfile.TemporaryDirectory(
        dir="/tmp", prefix="console-immediate-images-"
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
                binary,
                execution.serve(*arguments),
                current_directory=root,
            ) as client,
        ):
            client.initialize_and_list_tools()
            client.expect(
                python="import matplotlib.pyplot as plt",
                requirements={"python": ["matplotlib"]},
            )
            # fmt: python
            foreground = code(f"""
                with open({str(entered.path)!r}, "wb", buffering=0) as gate:
                    _ = gate.write(b"1")
                with open({str(release.path)!r}, "rb", buffering=0) as gate:
                    _ = gate.read(1)
                _ = plt.plot([3, 1])
                plt.show()
                """)
            pending = client.start_send(python=foreground, timeout_ms=60000)
            try:
                entered.wait(client=client)
                with ControlClient(endpoint) as control:
                    images = []
                    for record, values in ((True, [1, 2]), (False, [2, 1])):
                        result = control.request(
                            op="evaluate",
                            language="python",
                            execution="immediate",
                            record=record,
                            code=f"_ = plt.plot({values!r}); plt.show()",
                        )
                        assert result["result"]["execution_error"] is None, result
                        (image,) = result["result"]["content"]
                        assert image["type"] == "image", result
                        images.append(base64.b64decode(image["data"], validate=True))
                    release.release()
                    client.receive(pending)
                    (ordinary_image,) = pending["result"]["content"]
                    assert ordinary_image["type"] == "image", pending
                    ordinary_bytes = base64.b64decode(
                        ordinary_image["data"], validate=True
                    )
                    assert_result_content(
                        client, [ordinary_bytes], image_reference="ordinary plot"
                    )
                    client.expect("\n[idle]")
                    transcript = normalize_recording(client.finish(), root)

                (recording,) = (root / ".agents/console/sessions").iterdir()
                events = recording_events(recording)
                artifacts = [e for e in events if e["event"] == "image_retained"]
                assert len(artifacts) == 3, artifacts
                calls = [
                    e
                    for e in events
                    if e["event"] == "tool_call" and e.get("origin") == "integrator"
                ]
                assert len(calls) == 2, calls
                (ordinary,) = [
                    e
                    for e in events
                    if e["event"] == "tool_call"
                    and e["request"]["arguments"].get("python") == foreground
                ]
                (cell,) = [
                    e
                    for e in events
                    if e["event"] == "cell_started"
                    and e["call_id"] == ordinary["call_id"]
                ]
                markdown = (recording / "transcript.md").read_text()
                (qmd,) = (recording / "sessions").glob("*.qmd")
                source_document = qmd.read_text()
                assert "Image not retained" not in markdown, markdown
                for call, artifact, image, record in zip(
                    calls, artifacts, images, (True, False)
                ):
                    assert artifact["call_id"] == call["call_id"], artifact
                    expected_owner = {
                        "session_id": cell["owner"]["session_id"],
                        "call_id": call["call_id"],
                    }
                    if not record:
                        expected_owner["omit_from_projections"] = True
                    assert artifact["owner"] == expected_owner, artifact
                    assert (recording / artifact["path"]).read_bytes() == image
                    assert (artifact["path"] in markdown) is record, markdown
                    assert (
                        call["request"]["arguments"]["python"] in source_document
                    ) is record
                    assert not any(
                        e["event"] in ("cell_started", "cell_ended")
                        and e["call_id"] == call["call_id"]
                        for e in events
                    ), events
                    (result,) = [
                        e
                        for e in events
                        if e["event"] == "tool_result"
                        and e["call_id"] == call["call_id"]
                    ]
                    (block,) = result["result"]["content"]
                    assert block["path"] == artifact["path"], result
                    assert block.get("retained", True), result
                    assert all(part["owner"] is None for part in result["parts"]), (
                        result
                    )
                assert artifacts[-1]["call_id"] == ordinary["call_id"]
                assert artifacts[-1]["owner"] == cell["owner"]
                assert (
                    recording / artifacts[-1]["path"]
                ).read_bytes() == ordinary_bytes
                for entry in control.transcript:
                    entry["response"]["result"]["content"][0]["data"] = (
                        "<PNG byte-identical to captured immediate plot>"
                    )
                return TranscriptWithCompanions(
                    transcript, {"control.yaml": control.transcript}
                )
            finally:
                release.release()


if __name__ == "__main__":
    run_this_suite(__file__)
