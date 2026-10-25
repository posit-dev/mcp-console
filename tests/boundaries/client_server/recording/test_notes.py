"""Journal annotations are prose, independent of worker execution."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import selected_environment
from boundaries.client_server.recording.test_intervals import events
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import normalize_recording
from support.records import Transcript


@executions(RUNTIME)
def test_records_notes_with_and_without_code(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        notes = [
            "## Investigation\n\nCompare the café measurements.\n",
            "Check the **total** before comparing groups.",
            "The total is 42.\n\n```python\nraise RuntimeError('prose only')\n```",
            "Waiting for the measurement label; preserve the active cell.",
        ]
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(workspace),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                note=notes[0],
                timeout_ms=0,
                r=None,
                stdin=None,
                control=None,
                requirements=None,
            )
            client.expect(
                "42\n", python="total = 6 * 7; print(total)", r=None, note=notes[1]
            )
            client.expect(note=notes[2])
            client.expect(
                '[input requested: "label: "]\n[waiting for stdin]',
                python="label = input('label: '); print(label, total)",
            )
            client.expect(note=notes[3])
            client.expect("sample 42\n", stdin="sample\n", note="Use sample A.")
            invalid = client.send(python="total = -1", note=42)
            assert invalid["isError"], invalid
            client.expect("42\n", python="print(total)", note=None)
            transcript = client.finish()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = events(recording)
        calls = [event for event in capture if event["event"] == "tool_call"]
        assert [event["request"]["arguments"].get("note") for event in calls] == [
            notes[0],
            notes[1],
            notes[2],
            None,
            notes[3],
            "Use sample A.",
            42,
            None,
        ]
        assert [
            event["call_id"] for event in capture if event["event"] == "cell_started"
        ] == [2, 4, 8]
        markdown = (recording / "transcript.md").read_text(encoding="utf-8")
        for note in notes:
            assert markdown.count(note) == 1, markdown
        assert markdown.index(notes[0]) < markdown.index(notes[1])
        assert markdown.index(notes[1]) < markdown.index("```python\ntotal = 6 * 7")
        assert markdown.index(notes[2]) < markdown.index(notes[3])
        assert "## Call 1: Note\n\n" + notes[0] in markdown
        assert "Use sample A.\n\n### Submitted stdin" in markdown
        return normalize_recording(transcript, workspace)
