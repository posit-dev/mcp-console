"""Assertions shared by public MCP discovery cases."""

import re
from collections.abc import Iterator


def descriptions(value: dict) -> Iterator[str]:
    for key, item in value.items():
        if key == "description":
            yield item
        elif isinstance(item, dict):
            yield from descriptions(item)


def without_descriptions(value: object) -> object:
    if isinstance(value, dict):
        return {
            key: without_descriptions(item)
            for key, item in value.items()
            if key != "description"
        }
    if isinstance(value, list):
        return [without_descriptions(item) for item in value]
    return value


def assert_tool_guidance(tool: dict, languages: set[str], *, custom: bool) -> None:
    for description in descriptions(tool):
        names = set(
            re.findall(r"(?<![a-z])(?:r|python|sql)(?![a-z])", description, re.I)
        )
        assert {name.lower() for name in names} <= languages, description
    description = tool["description"]
    for instruction in (
        "ir run script.R",
        "#| packages:",
        "#| r-version:",
        "imports, data inputs",
        "without live Console objects",
    ):
        r_description = (
            tool["inputSchema"]["properties"].get("r", {}).get("description", "")
        )
        assert (instruction in r_description) == (not custom and "r" in languages)
    for instruction in (
        "An error can leave earlier changes in place",
        "do not resubmit the cell",
        "use only trusted dependencies",
    ):
        assert instruction in description, description
    switching = (
        "Switch languages when useful"
        if custom
        else "Choose whichever language makes each step"
    )
    assert (switching in description) == (len(languages) > 1)
    assert ("reticulate within an R cell" in description) == (
        not custom and {"r", "python"} <= languages
    )
    properties = tool["inputSchema"]["properties"]
    assert "discards" in properties["control"]["description"]
    assert "not rolled back" in " ".join(
        properties["requirements"]["description"].split()
    )
    if custom:
        assert "does not supply built-in runtime packages" in description
        assert "Managed requirements need" in description
    elif "sql" in languages:
        assert "CSV, Parquet, JSON/JSONL file globs" in description
        assert "databases via extensions" in description
        assert "provider" not in description.lower()
        sql = properties["sql"]["description"]
        assert "read_json('data/*.json*')" in sql
        assert "as tables, including nested fields" in sql
        assert "selected driver supplies its own SQL" in sql
        assert "CLI dot commands are not supported" in sql
        assert ("With R-owned managed DuckDB" in sql) == ("r" in languages)
        if "r" in languages:
            assert "DBI" in sql
        else:
            assert "from an R cell" not in sql
        if "python" in languages:
            python = properties["python"]["description"]
            for instruction in (
                "_console.sql_connection(connection)",
                "register(name, frame)",
                "active native Python connection",
            ):
                assert instruction in python, python
            if "r" in languages:
                assert "errors if SQL uses R" in python
            else:
                assert "errors if another runtime owns SQL" in python
                assert "on Python-owned DuckDB" in python
                assert "`r.name`" not in python
                assert "R plot rules" not in python
        else:
            assert "register(name, frame)" not in sql
        assert {"r", "python", "duckdb"} <= set(
            properties["requirements"]["properties"]
        )
