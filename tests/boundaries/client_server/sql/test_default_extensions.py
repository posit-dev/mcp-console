"""Managed R and no-R DuckDB share semantic defaults, not download lists."""

import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_worker_ready
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.normalization import code
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, SQL, requires
from boundaries.client_server.sql.test_without_r import environment, sql_client
from support.resolvers import expose_uv


def assert_semantic_defaults(
    client: McpClient, workspace: Path, *, r_backed: bool
) -> None:
    wait_for_worker_ready(client, "managed DuckDB defaults")
    inspected = client.send(requirements={"action": "get"}, timeout_ms=0)
    assert "structuredContent" in inspected, inspected
    assert inspected["structuredContent"]["requirements"]["duckdb"] == [
        "icu",
        "json",
        "sqlite",
    ], inspected
    with closing(sqlite3.connect(workspace / "audit.sqlite")) as database, database:
        database.execute("CREATE TABLE events (payload TEXT)")
        database.execute("INSERT INTO events VALUES (?)", ('{"answer":42}',))

    client.send(sql="SET autoinstall_known_extensions = false; SET TimeZone = 'UTC'")
    assert "Error:" not in last_tool_text(client), last_tool_text(client)
    client.send(sql="ATTACH 'audit.sqlite' AS audit (TYPE sqlite, READ_ONLY)")
    assert "Error:" not in last_tool_text(client), last_tool_text(client)
    client.send(
        sql=code("""
            SELECT
              CASE WHEN json_extract_string('{"answer":42}', '$.answer') = '42'
                THEN 1234567 ELSE 0 END AS json_ok,
              CASE WHEN CAST(
                timezone('America/New_York', TIMESTAMP '2020-01-01') AS VARCHAR
              ) = '2020-01-01 05:00:00+00'
                THEN 1234567 ELSE 0 END AS timezone_ok,
              CASE WHEN (SELECT payload->>'$.answer' FROM audit.events) = '42'
                THEN 1234567 ELSE 0 END AS sqlite_ok
            """)
    )
    output = last_tool_text(client)
    expected = (
        ["1", "1234567", "1234567", "1234567"]
        if r_backed
        else ["1234567", "|", "1234567", "|", "1234567"]
    )
    assert output.splitlines()[-1].split() == expected, output
    client.send(sql="INSERT INTO audit.events VALUES ('{}')")
    output = last_tool_text(client)
    assert "Error:" in output and "read-only" in output, output
    if r_backed:
        assert (
            "Context: rapi_execute" in output and "Error type: INVALID_INPUT" in output
        ), output
        client.transcript[-1]["result"]["content"][0]["text"] = output.replace(
            "\ni Context:", "\nℹ Context:"
        ).replace("\ni Error type:", "\nℹ Error type:")
    client.send(sql="SELECT count(*) AS events FROM audit.events")
    expected = ["1", "1"] if r_backed else ["1"]
    assert last_tool_text(client).splitlines()[-1].split() == expected


@requires(SQL, R)
@executions(DIRECT, SANDBOXED)
def test_managed_r_defaults(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        env, _ = r_test_environment()
        env.update(
            RETICULATE_PYTHON="",
            MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(workspace / "extensions"),
        )
        with McpClient(
            binary,
            execution.serve("-c", "cache=host"),
            env,
            current_directory=workspace,
        ) as client:
            client.initialize_and_list_tools()
            assert_semantic_defaults(client, workspace, r_backed=True)
            return client.finish()


@requires(SQL)
@executions(DIRECT, SANDBOXED)
def test_managed_python_defaults(binary: Path, execution: Execution) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        workspace = Path(directory)
        expose_uv(workspace)
        extensions = workspace / "extensions"
        env = dict(
            environment(workspace),
            MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY=str(extensions),
        )
        assert not extensions.exists()
        with sql_client(
            binary,
            execution,
            env,
            current_directory=workspace,
            arguments=("-c", "cache=host"),
        ) as client:
            assert_semantic_defaults(client, workspace, r_backed=False)
            assert list(extensions.glob("v*/**/sqlite_scanner.duckdb_extension"))
            for builtin in ("icu", "json"):
                assert not list(extensions.glob(f"**/{builtin}.duckdb_extension"))
            return client.finish()
