#!/usr/bin/env -S uv run --script
"""Successful send results echo display facts in _meta for chat UIs."""

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code
from support.records import Transcript
from support.requirements import POSIX, R, requires
from support.suites import run_this_suite

KEY = "posit.mcp-console/send"


@requires(POSIX, R)
@executions(RUNTIME)
def test_send_results_carry_display_facts(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary).resolve()
        with McpClient(binary, execution.serve(), current_directory=root) as client:
            client.initialize_and_list_tools()

            noted = client.send(
                # fmt: r
                r=code("""
                    cat("hi\n")
                    """),
                note="Greeting from R\n\nsecond line stays intact",
            )
            facts = noted["_meta"][KEY]
            assert facts == {
                "note": "Greeting from R\n\nsecond line stays intact",
                "language": "r",
            }, facts

            # The note is verbatim: no first-line extraction, no escaping.
            markup = client.send(r="6 * 7", note="<b>stay</b> & literal")
            assert markup["_meta"][KEY]["note"] == "<b>stay</b> & literal"

            plain = client.send(r="6 * 7")
            assert plain["_meta"][KEY] == {"language": "r"}, plain["_meta"]

            note_only = client.send(note="Standalone observation")
            assert note_only["content"] == [{"type": "text", "text": "[done]"}], (
                note_only
            )
            assert note_only["_meta"][KEY] == {"note": "Standalone observation"}, (
                note_only["_meta"]
            )

            inspection = client.send(requirements={"action": "get"})
            assert "_meta" not in inspection, inspection

            poll = client.send()
            assert "_meta" not in poll, poll

            return client.finish()


if __name__ == "__main__":
    run_this_suite(__file__)
