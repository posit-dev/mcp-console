"""Shared public acceptance for R-free local execution."""

from support.assertions import last_result_text, wait_for_evaluation_output
from support.client import McpClient
from support.normalization import code


def exercise_no_r_catalog(client: McpClient) -> None:
    client.initialize_and_list_tools()
    properties = client.transcript[-1]["result"]["tools"][0]["inputSchema"][
        "properties"
    ]
    requirements = properties["requirements"]["properties"]
    assert "python" in requirements
    client.send(sql="CREATE TABLE answers AS SELECT 42 AS answer")
    # fmt: python
    python = code("""
        import ctypes
        import os
        import shutil

        assert shutil.which("R") is None and shutil.which("Rscript") is None
        assert not hasattr(ctypes.CDLL(None), "Rf_initialize_R")
        original_pid = os.getpid()
        answer = 41
        assert _console.sql_connection().execute("SELECT answer FROM answers").fetchone() == (
            42,
        )
        answer + 1
        """)
    client.send(python=python)
    assert last_result_text(client) == "42\n", last_result_text(client)
    client.send(requirements={"python": ["py-yaml12"]})
    assert last_result_text(client) == "[prepared]"
    client.send(r="1 + 1")
    assert (
        last_result_text(client)
        == "R cells are unavailable in Python sessions without R"
    )
    client.send(
        # fmt: python
        python=code("""
            assert os.getpid() == original_pid
            answer + 1
            """)
    )
    assert last_result_text(client) == "42\n"
    client.send(sql="SELECT answer FROM answers")
    assert "42" in last_result_text(client)
    client.send(python="input('target> ')")
    assert "[waiting for stdin]" in last_result_text(client)
    wait_for_evaluation_output(
        client,
        "'exact input'\n",
        "R-free target input receipt",
        stdin="exact input\n",
    )
    assert last_result_text(client) == "'exact input'\n", last_result_text(client)
