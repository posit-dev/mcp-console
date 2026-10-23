"""Offline raghilda stores shared with Console SQL through Python connections."""

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from support.assertions import last_tool_text, wait_for_evaluation_output
from support.client import McpClient
from support.execution import DIRECT, SANDBOXED, Execution, executions
from support.installation import installed_console
from support.linux_sandbox import retain_system_bwrap
from support.normalization import code, normalize_onnx_device_probe
from support.r import r_test_environment
from support.records import Transcript
from support.requirements import R, SQL, command, requires
from support.resolvers import expose_uv


def exercise_store(client: McpClient) -> None:
    client.expect("[prepared]", requirements={"python": ["raghilda==0.2.1"]})
    output = wait_for_evaluation_output(
        client,
        None,
        "raghilda store creation",
        completion_timeout_seconds=client.response_timeout,
        # fmt: python
        python=code("""
            from collections.abc import Sequence
            from importlib.metadata import version
            from pathlib import Path
            from tempfile import TemporaryDirectory

            from raghilda.chunker import MarkdownChunker
            from raghilda.document import MarkdownDocument
            from raghilda.embedding import EmbeddingProvider, EmbedInputType
            from raghilda.store import DuckDBStore

            assert version("raghilda") == "0.2.1"


            class LocalEmbeddings(EmbeddingProvider):
                def embed(
                    self,
                    x: Sequence[str],
                    input_type: EmbedInputType = EmbedInputType.DOCUMENT,
                ) -> list[list[float]]:
                    return [[float("banana" in text.lower()), 1.0] for text in x]

                def get_config(self) -> dict[str, str]:
                    return {"type": "LocalEmbeddings"}

                @classmethod
                def from_config(cls, config: dict[str, str]) -> "LocalEmbeddings":
                    return cls()


            store_directory = TemporaryDirectory()
            store = DuckDBStore.create(
                Path(store_directory.name) / "knowledge.duckdb",
                embed=LocalEmbeddings(),
            )
            chunker = MarkdownChunker(chunk_size=1000, target_overlap=0)
            for origin, text in (
                ("alpha.md", "Apples are red fruit."),
                ("beta.md", "Bananas are yellow fruit."),
            ):
                store.upsert(chunker.chunk(MarkdownDocument(text, origin=origin)))

            # Scalar vector retrieval needs no downloaded index or model.
            hits = store.retrieve_vss("bananas", top_k=1)
            assert [(hit.origin, hit.text) for hit in hits] == [
                ("beta.md", "Bananas are yellow fruit.")
            ]
            print(f"{hits[0].origin}: {hits[0].text}")
            _console.sql_connection(store.con)
            """),
    )
    output = normalize_onnx_device_probe(output)
    assert output == "beta.md: Bananas are yellow fruit.\n", repr(output)
    client.transcript[-1]["result"]["content"][0]["text"] = output
    client.send(sql="SELECT origin, text FROM chunks ORDER BY origin LIMIT 2")
    rows = last_tool_text(client).splitlines()[2:]
    assert [tuple(cell.strip() for cell in row.split("|")) for row in rows] == [
        ("'alpha.md'", "'Apples are red fruit.'"),
        ("'beta.md'", "'Bananas are yellow fruit.'"),
    ], last_tool_text(client)
    client.send(sql="CREATE TEMP TABLE sql_marker AS SELECT 42 AS value")
    assert last_tool_text(client).splitlines()[-1] == "1"
    client.expect(
        "alpha.md: Apples are red fruit.; SQL marker: 42\n",
        # fmt: python
        python=code("""
            marker = store.con.execute("SELECT value FROM sql_marker").fetchone()
            assert marker == (42,)
            hits = store.retrieve_vss("apples", top_k=1)
            assert [(hit.origin, hit.text) for hit in hits] == [
                ("alpha.md", "Apples are red fruit.")
            ]
            print(f"{hits[0].origin}: {hits[0].text}; SQL marker: {marker[0]}")
            """),
    )


def close_store(client: McpClient) -> None:
    client.expect(
        "user-owned store survived restoration; private files removed\n",
        # fmt: python
        python=code("""
            assert store.con.execute("SELECT value FROM sql_marker").fetchone() == (42,)
            hits = store.retrieve_vss("bananas", top_k=1)
            assert [(hit.origin, hit.text) for hit in hits] == [
                ("beta.md", "Bananas are yellow fruit.")
            ]
            store.con.close()
            store_directory.cleanup()
            assert not Path(store_directory.name).exists()
            print("user-owned store survived restoration; private files removed")
            """),
    )


@requires(SQL, command("uv"))
@executions(DIRECT, SANDBOXED)
def test_queries_a_python_created_store_without_r(
    binary: Path, execution: Execution
) -> Transcript:
    with tempfile.TemporaryDirectory() as directory:
        tools = Path(directory)
        expose_uv(tools)
        retain_system_bwrap(tools)
        # Unused document converters import ONNX Runtime; keep its startup offline.
        environment = dict(os.environ, PATH=str(tools), ORT_DISABLE_TELEMETRY="1")
        for name in (
            "R_HOME",
            "R_LIBS",
            "R_LIBS_SITE",
            "R_LIBS_USER",
            "RETICULATE_PYTHON",
            "RETICULATE_UV",
        ):
            environment.pop(name, None)
        assert shutil.which("R", path=environment["PATH"]) is None
        assert shutil.which("Rscript", path=environment["PATH"]) is None
        with McpClient(
            installed_console(binary), execution.serve(), environment
        ) as client:
            client.initialize_and_list_tools()
            client.expect(
                "no R executable visible\n",
                # fmt: python
                python=code("""
                    import shutil

                    assert shutil.which("R") is None
                    assert shutil.which("Rscript") is None
                    print("no R executable visible")
                    """),
            )
            client.send(sql="CREATE TABLE managed_marker AS SELECT 7 AS value")
            assert last_tool_text(client).splitlines()[-1] == "1"
            exercise_store(client)
            client.expect(python="_console.sql_connection(None)")
            client.send(sql="SELECT value FROM managed_marker")
            assert last_tool_text(client).splitlines()[-1] == "7"
            close_store(client)
            return client.finish()[3:]


@requires(SQL, R, command("ir"), command("uv"))
@executions(DIRECT, SANDBOXED)
def test_restores_the_r_catalog_after_querying_a_python_store(
    binary: Path, execution: Execution
) -> Transcript:
    environment, _ = r_test_environment()
    environment["RETICULATE_PYTHON"] = ""
    environment["ORT_DISABLE_TELEMETRY"] = "1"
    with McpClient(binary, execution.serve(), environment) as client:
        client.initialize_and_list_tools()
        client.expect(sql="CREATE TABLE managed_marker AS SELECT 7 AS value")
        exercise_store(client)
        client.expect(python="_console.sql_connection(None)")
        client.expect(
            "R-owned managed catalog restored: 7\n",
            # fmt: r
            r=code("""
                restored <- .console$sql_connection()
                stopifnot(
                  inherits(restored, "duckdb_connection"),
                  DBI::dbGetQuery(restored, "SELECT value FROM managed_marker")$value == 7
                )
                writeLines("R-owned managed catalog restored: 7")
                """),
        )
        client.send(sql="SELECT value FROM managed_marker")
        assert last_tool_text(client).splitlines()[-1].split() == ["1", "7"]
        close_store(client)
        return client.finish()[3:]
