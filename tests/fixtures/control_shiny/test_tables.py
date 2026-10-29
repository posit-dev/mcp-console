"""Exercise table actions against the real control channel and managed runtimes."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from boundaries.client_server.integrations.test_control import (
    ControlClient,
    result_text,
)
from support.client import McpClient


@pytest.fixture
def console():
    binary = Path(os.environ["MCP_CONSOLE_TEST_BINARY"])
    with tempfile.TemporaryDirectory(prefix="console-table-", dir="/tmp") as directory:
        root = Path(directory)
        endpoint = root / "control.sock"
        with McpClient(
            binary,
            (
                "serve",
                "--no-sandbox",
                "--control-socket",
                str(endpoint),
                "-c",
                "languages=[r,python,sql]",
            ),
            current_directory=root,
        ) as client:
            client.initialize_and_list_tools()
            client.expect(python="pass", requirements={"python": ["pandas", "duckdb"]})
            client.expect(r="invisible(reticulate::import_builtins())")
            with ControlClient(endpoint) as control:

                async def evaluate(language, source):
                    response = control.request(
                        op="evaluate",
                        language=language,
                        code=source,
                        execution="immediate",
                        record=False,
                    )
                    assert "error" not in response, response
                    return response["result"]

                yield client, control, evaluate


@pytest.mark.parametrize("language", ["r", "python"])
def test_table_snapshot_filter_sort_refresh_and_namespace_privacy(console, language):
    from tables import TableViewer

    client, control, evaluate = console
    if language == "r":
        client.expect(
            r='set.seed(1729); frame <- data.frame(n=0:9999, label=rep(c("alpha", "beta"), 5000), flag=rep(c(TRUE,FALSE),5000))'
        )
    else:
        client.expect(
            python='import pandas as pd\nframe = pd.DataFrame({"n": range(10000), "label": ["alpha", "beta"] * 5000, "flag": [True, False] * 5000})'
        )
    before_r = client.expect(None, r="dput(ls(all.names=TRUE))")["content"]
    before_seed = result_text(
        control.request(
            op="evaluate",
            language="r",
            execution="immediate",
            record=False,
            code="cat(jsonlite::toJSON(.Random.seed))",
        )
    )
    before_python = client.expect(None, python="print(sorted(globals()))")["content"]
    before_sql = client.expect(None, sql="SHOW TABLES")["content"]

    async def exercise():
        viewer = TableViewer(evaluate)
        metadata = await viewer.open(language, "frame")
        assert metadata["row_count"] == 10000
        assert [column["name"] for column in metadata["columns"]] == [
            "n",
            "label",
            "flag",
        ]
        page = await viewer.rows(
            metadata["id"],
            20,
            25,
            [{"colId": "c0", "sort": "desc"}],
            {"c1": {"filterType": "text", "type": "equals", "filter": "alpha"}},
        )
        assert [row["c0"] for row in page["rows"]] == [9958, 9956, 9954, 9952, 9950]
        assert page["row_count"] == 5000
        numeric = await viewer.rows(
            metadata["id"],
            0,
            5,
            [],
            {
                "c0": {
                    "filterType": "number",
                    "type": "inRange",
                    "filter": 10,
                    "filterTo": 12,
                }
            },
        )
        assert [row["c0"] for row in numeric["rows"]] == [10, 11, 12]
        mutation = (
            "frame$n <- -frame$n" if language == "r" else 'frame["n"] = -frame["n"]'
        )
        response = control.request(
            op="evaluate", language=language, code=mutation, busy="wait", wait_ms=60000
        )
        assert not response["result"]["execution_error"]
        unchanged = await viewer.rows(metadata["id"], 1, 3, [], {})
        assert [row["c0"] for row in unchanged["rows"]] == [1, 2]
        refreshed = await viewer.open(language, "frame")
        updated = await viewer.rows(refreshed["id"], 1, 3, [], {})
        assert [row["c0"] for row in updated["rows"]] == [-1, -2]
        with pytest.raises(ValueError, match="replaced"):
            await viewer.rows(metadata["id"], 0, 1, [], {})
        await viewer.close()

    asyncio.run(exercise())
    assert client.expect(None, r="dput(ls(all.names=TRUE))")["content"] == before_r
    assert (
        result_text(
            control.request(
                op="evaluate",
                language="r",
                execution="immediate",
                record=False,
                code="cat(jsonlite::toJSON(.Random.seed))",
            )
        )
        == before_seed
    )
    assert (
        client.expect(None, python="print(sorted(globals()))")["content"]
        == before_python
    )
    assert client.expect(None, sql="SHOW TABLES")["content"] == before_sql
    assert result_text(control.request(op="preview")) == ""


def test_table_filters_types_block_limits_and_close(console):
    from tables import TableViewer

    client, _, evaluate = console
    client.expect(
        python='import pandas as pd\nframe = pd.DataFrame({"n": [2**60, 2**60+1, None], "text": ["a%_quote\\"", "b", None], "date": pd.to_datetime(["2026-01-01", "2026-01-02", None])})\nframe["n"] = pd.array([2**60, 2**60+1, None], dtype="Int64")'
    )

    async def exercise():
        viewer = TableViewer(evaluate)
        metadata = await viewer.open("python", "frame")
        assert metadata["columns"][0]["kind"] == "text"
        precise = await viewer.rows(
            metadata["id"],
            0,
            3,
            [],
            {"c0": {"filterType": "text", "type": "equals", "filter": str(2**60 + 1)}},
        )
        assert precise["rows"][0]["rowId"] == "1"
        rows = await viewer.rows(metadata["id"], 0, 3, [], {})
        assert [row["c0"] for row in rows["rows"]] == [str(2**60), str(2**60 + 1), None]
        quote = await viewer.rows(
            metadata["id"],
            0,
            3,
            [],
            {"c1": {"filterType": "text", "type": "contains", "filter": '%_quote"'}},
        )
        assert len(quote["rows"]) == 1
        blank = await viewer.rows(
            metadata["id"], 0, 3, [], {"c2": {"filterType": "date", "type": "blank"}}
        )
        assert blank["rows"][0]["rowId"] == "2"
        dated = await viewer.rows(
            metadata["id"],
            0,
            3,
            [],
            {
                "c2": {
                    "filterType": "date",
                    "type": "equals",
                    "dateFrom": "2026-01-02 00:00:00",
                }
            },
        )
        assert dated["rows"][0]["rowId"] == "1"
        with pytest.raises(ValueError, match="200"):
            await viewer.rows(metadata["id"], 0, 201, [], {})
        with pytest.raises(RuntimeError, match="Unknown column"):
            await viewer.rows(
                metadata["id"],
                0,
                3,
                [{"colId": "c0; DROP TABLE snapshot", "sort": "asc"}],
                {},
            )
        with pytest.raises(RuntimeError, match="Unknown column"):
            await viewer.rows(
                metadata["id"], 0, 3, [], {"bad": {"type": "equals", "filter": "x"}}
            )
        await viewer.close()
        with pytest.raises(ValueError, match="replaced"):
            await viewer.rows(metadata["id"], 0, 1, [], {})

    asyncio.run(exercise())


@pytest.mark.parametrize("language", ["r", "python"])
def test_variable_inspection_is_bounded_json_with_table_choices(console, language):
    from inspection import PYTHON_SOURCE, R_SOURCE

    client, control, _ = console
    if language == "r":
        client.expect(
            r='frame <- data.frame(value=1); local(for (i in 1:150) assign(paste0("variable_", i), paste(rep("\\"", 80), collapse=""), envir=globalenv()))'
        )
    else:
        client.expect(
            python='import pandas as pd\nframe = pd.DataFrame({"value": [1]})\nexec("for i in range(150): globals()[f\'variable_{i}\'] = chr(34) * 80")'
        )
    response = control.request(
        op="evaluate",
        language=language,
        code=R_SOURCE if language == "r" else PYTHON_SOURCE,
        execution="immediate",
        record=False,
    )
    text = result_text(response)
    assert len(text.encode()) <= 7001
    payload = json.loads(text)
    assert payload["tables"] == ["frame"]
    assert payload["omitted_lines"] > 0
    assert "omitted" in payload["text"]


def test_table_large_blocks_supported_scalars_and_replacement(console):
    from tables import TableViewer

    client, control, evaluate = console
    client.expect(
        python='import pandas as pd\nframe = pd.DataFrame({"text": ["x" * 2000] * 200, "group": pd.Categorical(["a", "b"] * 100)})'
    )

    async def exercise():
        viewer = TableViewer(evaluate)
        metadata = await viewer.open("python", "frame")
        result = await viewer.rows(metadata["id"], 0, 200, [], {})
        assert len(result["rows"]) == 200
        assert result["truncated"]
        assert len(result["rows"][0]["c0"]) == 1025
        control.request(
            op="evaluate",
            language="python",
            code="del frame",
            busy="wait",
            wait_ms=60000,
        )
        retained = await viewer.rows(metadata["id"], 199, 200, [], {})
        assert retained["rows"][0]["rowId"] == "199"
        with pytest.raises(RuntimeError, match="KeyError"):
            await viewer.open("python", "frame")
        client.expect(
            None,
            control="restart",
            python='import pandas as pd\nframe = pd.DataFrame({"new": [42]})',
        )
        viewer.reset()
        with pytest.raises(ValueError, match="replaced"):
            await viewer.rows(metadata["id"], 0, 1, [], {})
        replacement = await viewer.open("python", "frame")
        assert (await viewer.rows(replacement["id"], 0, 1, [], {}))["rows"] == [
            {"rowId": "0", "c0": 42}
        ]
        await viewer.close()

    asyncio.run(exercise())


def test_blank_text_filters_and_unsupported_columns(console):
    from tables import TableViewer

    client, _, evaluate = console
    client.expect(
        python='import pandas as pd\nframe = pd.DataFrame({"text": [None, "", " \\t", "value"]})\nnested = pd.DataFrame({"items": [[1, 2]]})'
    )

    async def exercise():
        viewer = TableViewer(evaluate)
        metadata = await viewer.open("python", "frame")
        blank = await viewer.rows(
            metadata["id"],
            0,
            4,
            [],
            {"c0": {"filterType": "text", "type": "blank"}},
        )
        assert [row["rowId"] for row in blank["rows"]] == ["0", "1", "2"]
        present = await viewer.rows(
            metadata["id"],
            0,
            4,
            [],
            {"c0": {"filterType": "text", "type": "notBlank"}},
        )
        assert [row["rowId"] for row in present["rows"]] == ["3"]
        with pytest.raises(RuntimeError, match="Unsupported object column"):
            await viewer.open("python", "nested")
        assert (await viewer.rows(metadata["id"], 0, 4, [], {}))["row_count"] == 4
        await viewer.close()

    asyncio.run(exercise())
