#!/usr/bin/env -S uv run --script

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text
from support.client import McpClient
from support.execution import RUNTIME, Execution, executions
from support.normalization import code, normalize_recording
from support.previews import recording_events
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, SQL, command, requires
from support.suites import run_this_suite


@requires(R, SQL, command("ir"))
@executions(RUNTIME)
def test_named_sessions_isolate_r_sql_and_requirements(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        environment, _ = r_test_environment()
        environment["RETICULATE_PYTHON"] = sys.executable
        with McpClient(
            binary,
            execution.serve("-c", "r.packages=[]"),
            environment,
            root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(r="value <- 'default'")
            client.expect(
                # fmt: r
                r=code("""
                    value <- "alpha"
                    source <- data.frame(value = 1L)
                    """),
                session="alpha",
            )
            client.expect(
                sql="CREATE TABLE results AS SELECT value FROM source",
                session="alpha",
            )
            client.expect(
                # fmt: r
                r=code("""
                    stopifnot(
                      !exists("value"),
                      !DBI::dbExistsTable(.console$sql_connection(), "results")
                    )
                    value <- "beta"
                    source <- data.frame(value = 2L)
                    """),
                session="beta",
            )
            client.expect(
                sql="CREATE TABLE results AS SELECT value FROM source",
                session="beta",
            )
            # fmt: r
            verify = code(r"""
                result <- DBI::dbGetQuery(
                  .console$sql_connection(),
                  "SELECT value FROM results"
                )
                cat(sprintf("%s %d\n", value, result$value))
                """)
            client.expect("alpha 1\n", r=verify, session="alpha")
            client.expect("beta 2\n", r=verify, session="beta")
            client.expect(
                "default\n",
                # fmt: r
                r=code(r"""
                    stopifnot(!DBI::dbExistsTable(.console$sql_connection(), "results"))
                    cat(value, "\n", sep = "")
                    """),
            )

            def inspect(session: str) -> dict:
                result = client.send(session=session, requirements={"action": "get"})
                assert not result.get("isError"), result
                snapshot = result["structuredContent"]
                assert json.loads(last_tool_text(client)) == snapshot
                return snapshot

            baseline = inspect("alpha")
            assert inspect("beta") == baseline
            assert inspect("default") == baseline
            assert "core_functions" not in baseline["requirements"]["duckdb"]
            client.expect(
                "[prepared]",
                session="alpha",
                requirements={"duckdb": ["core_functions"]},
            )
            expected = dict(
                baseline,
                requirements=dict(
                    baseline["requirements"],
                    duckdb=sorted(
                        [*baseline["requirements"]["duckdb"], "core_functions"]
                    ),
                ),
            )
            assert inspect("alpha") == expected
            assert inspect("beta") == baseline
            assert inspect("default") == baseline
            client.expect("alpha 1\n", r=verify, session="alpha")
            client.expect("beta 2\n", r=verify, session="beta")
            transcript = client.finish()
        (recording,) = (root / ".agents/console/sessions").iterdir()
        workers = {
            event.get("session", "default"): event["session_id"]
            for event in recording_events(recording)
            if event["event"] == "worker_started"
        }
        assert workers.keys() == {"default", "alpha", "beta"}, workers
        for session, worker in workers.items():
            source = (recording / f"sessions/session-{worker:03}.qmd").read_text()
            assert ("    - core_functions\n" in source) == (session == "alpha"), (
                session,
                source,
            )
        return normalize_recording(transcript, root)


if __name__ == "__main__":
    run_this_suite(__file__)
