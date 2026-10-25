"""Session QMDs preserve journal prose across worker acquisition and restart."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_without_r import selected_environment
from boundaries.client_server.recording.test_intervals import events
from support.assertions import last_result_text, wait_for_worker_ready
from support.client import McpClient
from support.discovery import discovery_environment
from support.execution import DIRECT, RUNTIME, Execution, executions
from support.normalization import normalize_recording
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, command, requires


@executions(RUNTIME)
def test_notes_follow_cells_across_restart(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        before = "## First measurement\n\nRecord the initial value."
        first = "Use the original worker for the baseline."
        conclusion = "The baseline is ready; this concludes the first session."
        replacement = "## Follow-up\n\nRepeat with a fresh worker."
        ending = "The follow-up is complete."
        first_source = "print('original')"
        next_source = "print('replacement')"
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(workspace),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            wait_for_worker_ready(
                client, "worker acquired before the initial journal note"
            )
            client.expect(note=before)
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            note_only = (recording / "sessions/session-001.qmd").read_text(
                encoding="utf-8"
            )
            assert note_only.count(before) == 1, note_only
            assert "```{" not in note_only, note_only
            client.expect("original\n", python=first_source, note=first)
            client.expect(note=conclusion)
            client.expect(
                "[worker stopped: in-memory state lost]\n"
                "[starting new worker]\nreplacement\n[done]",
                control="restart",
                python=next_source,
                note=replacement,
            )
            client.expect(note=ending)
            transcript = client.finish()

        documents = sorted((recording / "sessions").glob("*.qmd"))
        assert [path.name for path in documents] == [
            "session-001.qmd",
            "session-002.qmd",
        ], documents
        original, restarted = [path.read_text(encoding="utf-8") for path in documents]
        original_parts = [before, first, first_source, conclusion]
        restarted_parts = [replacement, next_source, ending]
        for document, parts, excluded in (
            (original, original_parts, restarted_parts),
            (restarted, restarted_parts, original_parts),
        ):
            assert all(document.count(part) == 1 for part in parts), document
            assert all(part not in document for part in excluded), document
            positions = [document.index(part) for part in parts]
            assert positions == sorted(positions), document
        assert original.endswith(conclusion + "\n\n"), original
        assert restarted.endswith(ending + "\n\n"), restarted
        return normalize_recording(transcript, workspace)


@requires(R, command("rustc"))
def test_notes_keep_request_order_while_source_waits_for_discovery(
    binary: Path,
) -> Transcript:
    r_environment, _ = r_test_environment()
    with (
        tempfile.TemporaryDirectory() as directory,
        discovery_environment(r_home=Path(r_environment["R_HOME"])) as (
            environment,
            discovery,
        ),
    ):
        workspace = Path(directory)
        before = "Record the question before the worker starts."
        accompanying = "Compute the initial answer."
        after = "The cell is queued; retain this observation after its source."
        with McpClient(
            binary, DIRECT.serve(), environment, current_directory=workspace
        ) as client:
            try:
                client.transcript.append(
                    {
                        "scenario": "Notes retain request order while a source cell awaits its worker.",
                        "fixture": "A gated R discovery probe holds worker acquisition until both the cell and a later standalone note have returned.",
                    }
                )
                discovery.wait("runtime discovery is blocked before worker acquisition")
                client.initialize_and_list_tools()
                client.expect(note=before)
                pending = client.send(r="42L", note=accompanying, timeout_ms=0)
                assert not pending.get("isError"), pending
                assert last_result_text(client).endswith(
                    "[running; poll with an empty send]"
                ), pending
                client.expect(note=after)
                discovery.release()
                client.expect("[1] 42\n")
                transcript = client.finish()
            finally:
                discovery.release()

        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = events(recording)
        calls = [event for event in capture if event["event"] == "tool_call"]
        source_call = next(
            event for event in calls if event["request"]["arguments"].get("r") == "42L"
        )
        later_note = next(
            event
            for event in calls
            if event["request"]["arguments"].get("note") == after
        )
        association = next(
            event
            for event in capture
            if event["event"] == "source_associated"
            and event["call_id"] == source_call["call_id"]
        )
        assert later_note["sequence"] < association["sequence"], capture
        quarto = (recording / "sessions/session-001.qmd").read_text(encoding="utf-8")
        parts = [before, accompanying, "```{r}\n#| error: true\n42L\n```", after]
        assert all(quarto.count(part) == 1 for part in parts), quarto
        positions = [quarto.index(part) for part in parts]
        assert positions == sorted(positions), quarto
        return normalize_recording(transcript, workspace)
