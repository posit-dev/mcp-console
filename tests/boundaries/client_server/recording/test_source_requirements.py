"""Source documents follow actual worker lifetimes and accepted declarations."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.requirements import R, SQL, requires
from support.records import Transcript
from support.suites import run_this_suite

from boundaries.client_server.recording.test_intervals import events
from support.normalization import normalize_recording


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_records_accepted_live_duckdb_additions(
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
            client.send(python="session_identity = object()")
            before = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert "fts" not in before["duckdb"], before
            client.expect("[prepared]", requirements={"duckdb": ["fts"]})
            accepted = client.send(requirements={"action": "get"})["structuredContent"][
                "requirements"
            ]
            assert "fts" in accepted["duckdb"], accepted
            client.send(
                python="assert session_identity is not None; print('same worker')"
            )
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            managed = list((recording / "sessions").glob("*.qmd"))
            assert len(managed) == 1, managed
            assert "    - fts\n" in managed[0].read_text()
            return normalize_recording(client.finish(), workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_replacement_records_retained_r_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        source = (
            "retained <- packageVersion('praise'); cat('retained praise available\\n')"
        )
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "initial praise available\n",
                r="initial <- packageVersion('praise'); cat('initial praise available\\n')",
                requirements={"r": ["praise"]},
            )
            rejected = client.send(
                r="stop('rejected source')",
                requirements={"r": ["uncommittedpackage"], "python": [""]},
            )
            assert rejected["isError"], rejected
            client.send(control="restart")
            client.expect("retained praise available\n", r=source)
            selected = client.send(requirements={"action": "get"})["structuredContent"]
            assert selected["requirements"]["r"] == ["praise"], selected
            transcript = client.finish()
        (recording,) = (workspace / ".agents/console/sessions").iterdir()
        capture = events(recording)
        (call,) = [
            event
            for event in capture
            if event["event"] == "tool_call"
            and event["request"].get("arguments", {}).get("r") == source
        ]
        (cell,) = [
            event
            for event in capture
            if event["event"] == "cell_started" and event["call_id"] == call["call_id"]
        ]
        session = cell["owner"]["session_id"]
        path = recording / f"sessions/session-{session:03}.qmd"
        qmd = path.read_text()
        assert "  packages:\n    - praise\n" in qmd, qmd
        assert "uncommittedpackage" not in qmd, qmd
        assert source in qmd, qmd
        return normalize_recording(transcript, workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_restart_requirements_leave_retired_source_unchanged(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("original session\n", r='cat("original session\\n")')
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            original = recording / "sessions/session-001.qmd"
            before = original.read_text()
            client.send(
                control="restart", requirements={"action": "set", "r": ["praise"]}
            )
            client.expect(
                "replacement session\n",
                r='stopifnot(requireNamespace("praise", quietly = TRUE)); cat("replacement session\\n")',
            )
            assert original.read_text() == before, original.read_text()
            path = recording / "sessions/session-002.qmd"
            qmd = path.read_text()
            assert "  packages:\n    - praise\n" in qmd, qmd
            assert "original session" not in qmd and "replacement session" in qmd
            assert "Requirements selected" not in qmd and "eval: false" not in qmd
            return normalize_recording(client.finish(), workspace)


@requires(R)
@executions(DIRECT, SANDBOXED)
def test_rejected_additions_leave_session_metadata_unchanged(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        with McpClient(
            binary,
            execution.serve(
                "-c",
                "r.packages=[]",
            ),
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            client.expect("ready\n", r='token <- new.env(); cat("ready\\n")')
            client.expect("[prepared]", requirements={"r": ["praise"]})
            before = client.send(requirements={"action": "get"})["structuredContent"]
            rejected = client.send(
                requirements={"r": ["uncommittedpackage"], "python": [""]}
            )
            assert rejected["isError"], rejected
            after = client.send(requirements={"action": "get"})["structuredContent"]
            assert after == before, (before, after)
            client.expect(r="stopifnot(is.environment(token))")
            (recording,) = (workspace / ".agents/console/sessions").iterdir()
            path = recording / "sessions/session-001.qmd"
            qmd = path.read_text()
            assert "uncommittedpackage" not in qmd, qmd
            assert "    - praise\n" in qmd, qmd
            capture = events(recording)
            assert len([e for e in capture if e["event"] == "worker_started"]) == 1
            return normalize_recording(client.finish(), workspace)


if __name__ == "__main__":
    run_this_suite(__file__)
