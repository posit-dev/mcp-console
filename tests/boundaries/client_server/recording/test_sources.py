"""Source documents follow actual worker lifetimes and accepted declarations."""

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.assertions import last_result_text
from support.requirements import R, requires
from support.records import Transcript
from support.suites import run_this_suite
from boundaries.client_server.python.test_without_r import selected_environment

from boundaries.client_server.recording.test_intervals import events
from support.previews import live_recording_events
from support.normalization import normalize_recording


@executions(DIRECT, SANDBOXED)
def test_controlled_cells_belong_to_their_selected_session(
    binary: Path, execution: Execution
) -> Transcript:
    return controlled_cells_recording(binary, execution, "python")


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_controlled_r_cells_belong_to_their_selected_session(
    binary: Path, execution: Execution
) -> Transcript:
    return controlled_cells_recording(binary, execution, "r")


def controlled_cells_recording(
    binary: Path, execution: Execution, language: str
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if language == "python" and os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        cells = [
            (None, "original worker source"),
            ("restart", "replacement worker source"),
            ("interrupt", "surviving worker source"),
        ]
        sources = [f"print({label!r})" for _, label in cells]
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(workspace)
            if language == "python"
            else dict(os.environ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            for (control, label), source in zip(cells, sources, strict=True):
                result = client.send(
                    **{language: source}, **({"control": control} if control else {})
                )
                assert not result.get("isError"), result
                assert label in last_result_text(client), result
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        documents = sorted((recording / "sessions").glob("*.qmd"))
        assert len(documents) == 2, documents
        original, replacement = [path.read_text() for path in documents]
        assert sources[0] in original, original
        assert all(source not in original for source in sources[1:]), original
        assert sources[0] not in replacement, replacement
        assert all(replacement.count(source) == 1 for source in sources[1:]), (
            replacement
        )
        capture = events(recording)
        calls = {
            event["request"]["arguments"].get(language): event["call_id"]
            for event in capture
            if event["event"] == "tool_call"
        }
        associations = [
            event["session_id"]
            for event in capture
            if event["event"] == "source_associated"
            and event["call_id"] == calls[sources[1]]
        ]
        assert associations == [2], associations
        return normalize_recording(transcript, workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_rejected_restart_source_stays_with_the_surviving_session(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", r='token <- new.env(); cat("ready\\n")')
            sources = {
                "r": "rejected_restart_r <- TRUE",
                "python": "rejected_restart_python = True",
            }
            for language, source in sources.items():
                rejected = client.send(
                    control="restart", requirements={}, **{language: source}
                )
                assert rejected["isError"], rejected
            client.expect(
                r='stopifnot(is.environment(token), !exists("rejected_restart_r"))'
            )
            client.expect(python="assert 'rejected_restart_python' not in globals()")
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            managed = (recording / "sessions/session-001.qmd").read_text()
            for language, source in sources.items():
                assert f"```{{{language}}}\n#| error: true\n{source}\n```" in managed, (
                    managed
                )
            capture = live_recording_events(recording)
            assert len([e for e in capture if e["event"] == "worker_started"]) == 1
            return normalize_recording(client.finish(), workspace)


@executions(DIRECT, SANDBOXED)
def test_automatic_replacement_separates_source_and_failed_cells(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        if os.name != "nt":
            (workspace / "python3").symlink_to(sys.executable)
        with McpClient(
            binary,
            execution.serve(),
            selected_environment(workspace),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("original session\n", python="print('original session')")
            failed = client.send(python="import os; os._exit(17)")
            assert failed["isError"], failed
            client.expect(
                "automatic replacement\n", python="print('automatic replacement')"
            )
            rejected = client.send(python="raise ValueError('retained failure')")
            assert not rejected.get("isError"), rejected
            assert "ValueError: retained failure" in last_result_text(client), rejected
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        documents = sorted((recording / "sessions").glob("*.qmd"))
        assert [path.name for path in documents] == [
            "session-001.qmd",
            "session-002.qmd",
        ]
        original, replacement = [path.read_text() for path in documents]
        assert "print('original session')" in original
        assert "os._exit(17)" in original and "os._exit(17)" not in replacement
        assert (
            "automatic replacement" in replacement
            and "automatic replacement" not in original
        )
        assert "#| error: true\nraise ValueError('retained failure')" in replacement
        assert "ValueError: retained failure" not in replacement
        assert "root.dir:" not in original + replacement
        assert not (recording / "transcript.qmd").exists()
        markdown = (recording / "transcript.md").read_text()
        assert "ValueError: retained failure" in markdown
        for id in (1, 2):
            assert f"## Session {id:03} started" in markdown
            assert f"sessions/session-{id:03}.qmd" in markdown
        assert (
            len([e for e in events(recording) if e["event"] == "worker_started"]) == 2
        )
        for path in recording.rglob("*"):
            if path.is_file():
                assert not path.stat().st_mode & 0o222, path
                if os.name != "nt":
                    assert not path.stat().st_mode & 0o077, path
        return normalize_recording(transcript, workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
