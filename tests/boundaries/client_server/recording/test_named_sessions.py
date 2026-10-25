#!/usr/bin/env -S uv run --script

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from boundaries.client_server.python.test_startup import selected_python
from boundaries.client_server.recording.test_intervals import events
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code, normalize_recording
from support.records import Transcript
from support.suites import run_this_suite


@executions(RUNTIME)
def test_concurrent_sessions_keep_source_and_output_owners(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        sources = {
            session:
            # fmt: python
            code(f"""
                answer = input("{session}> ")
                print("{session} output:", answer)
                """)
            for session in ("alpha", "beta")
        }
        with McpClient(
            binary,
            execution.serve(),
            selected_python(root, Path(sys.executable)),
            root,
        ) as client:
            client.initialize_and_list_tools()
            for session, source in sources.items():
                client.expect(
                    f'[input requested: "{session}> "]\n[waiting for stdin]',
                    python=source,
                    session=session,
                )
            for session in sources:
                client.expect(
                    f"{session} output: {session} answer\n",
                    stdin=f"{session} answer\n",
                    session=session,
                )
            transcript = client.finish()

        (recording,) = (root / ".agents/console/sessions").iterdir()
        capture = events(recording)
        calls = {
            event["request"]["arguments"].get("python"): event["call_id"]
            for event in capture
            if event["event"] == "tool_call"
        }
        owners = {}
        markdown = (recording / "transcript.md").read_text()
        for session, source in sources.items():
            assert markdown.count(f"## Call {calls[source]}: Python\n") == 1, markdown
            assert f'"session": "{session}"' in markdown, markdown
            (cell,) = [
                event
                for event in capture
                if event["event"] == "cell_started"
                and event["call_id"] == calls[source]
            ]
            owners[session] = cell["owner"]
            (association,) = [
                event
                for event in capture
                if event["event"] == "source_associated"
                and event["call_id"] == calls[source]
            ]
            assert association["session_id"] == cell["owner"]["session_id"]
            (output,) = [
                event
                for event in capture
                if event["event"] == "cell_output" and event["call_id"] == calls[source]
            ]
            assert output["owner"] == cell["owner"], output
            assert (recording / output["path"]).read_text() == (
                f"{session} output: {session} answer\n"
            )
            document = (
                recording / f"sessions/session-{cell['owner']['session_id']:03}.qmd"
            ).read_text()
            assert source in document, document
            other = "beta" if session == "alpha" else "alpha"
            assert sources[other] not in document, document
        assert owners["alpha"]["session_id"] != owners["beta"]["session_id"]
        assert len(list((recording / "sessions").glob("*.qmd"))) == 2
        return normalize_recording(transcript, root)


if __name__ == "__main__":
    run_this_suite(__file__)
