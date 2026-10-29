"""Private worker-side snapshot and query implementation for the Shiny fixture.

Loaded into an isolated module, never Python's __main__. R calls capture only
from its owning thread; subsequent requests touch the DuckDB snapshot alone.
"""

import datetime
import decimal
import json
import math
import os
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd

connection = duckdb.connect(config={"preserve_insertion_order": True})
identifier: str | None = None
columns: list[dict[str, Any]] = []
row_count = 0


def emit(value: dict[str, Any], output: str) -> None:
    path = Path(output)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, allow_nan=False))
    os.replace(temporary, path)


def capture(frame: pd.DataFrame, view_id: str, output: str) -> None:
    global identifier, columns, row_count
    assert isinstance(frame, pd.DataFrame), "Select a data.frame or pandas DataFrame"
    assert 1 <= len(frame.columns) <= 100, "Table viewer supports 1–100 columns"
    assert all(isinstance(name, str) for name in frame.columns), (
        "Column names must be strings"
    )
    assert frame.columns.is_unique, "Column names must be unique"
    for dtype, (_, series) in zip(frame.dtypes, frame.items()):
        if dtype == object:
            inferred = pd.api.types.infer_dtype(series, skipna=True)
            assert inferred in {
                "string",
                "unicode",
                "bytes",
                "boolean",
                "integer",
                "floating",
                "mixed-integer-float",
                "decimal",
                "date",
                "datetime",
                "datetime64",
                "empty",
            }, f"Unsupported object column: {series.name} ({inferred})"
    renamed = frame.set_axis(
        [f"c{index}" for index in range(len(frame.columns))], axis=1
    )
    connection.register("input_frame", renamed)
    try:
        schema = connection.execute("DESCRIBE SELECT * FROM input_frame").fetchall()
        metadata = []
        for original, (field, kind, *_) in zip(frame.columns, schema):
            if kind in {"BOOLEAN", "VARCHAR"} or kind.startswith("ENUM("):
                filter_name = "text"
            elif kind in {
                "TINYINT",
                "SMALLINT",
                "INTEGER",
                "BIGINT",
                "HUGEINT",
                "UTINYINT",
                "USMALLINT",
                "UINTEGER",
                "UBIGINT",
                "UHUGEINT",
                "FLOAT",
                "DOUBLE",
            } or kind.startswith("DECIMAL("):
                filter_name = "number"
            elif kind == "DATE" or kind.startswith("TIMESTAMP"):
                filter_name = "date"
            else:
                raise ValueError(f"Unsupported column type: {original} ({kind})")
            metadata.append(
                {"field": field, "name": original, "type": kind, "kind": filter_name}
            )
        # Materialize once: the snapshot no longer holds a registration or source
        # object, and row position supplies a stable tie breaker for every sort.
        connection.execute(
            "CREATE OR REPLACE TABLE snapshot AS SELECT row_number() OVER () - 1 AS row_id, * FROM input_frame"
        ).fetchall()
        # JavaScript's number input cannot express all integers or decimals.
        # Keep exact text matching for those columns, with SQL numeric sorting.
        wide = [
            item
            for item in metadata
            if item["type"] in {"BIGINT", "UBIGINT", "HUGEINT", "UHUGEINT"}
        ]
        aggregates = [
            "count(*)",
            *[f'min("{item["field"]}"), max("{item["field"]}")' for item in wide],
        ]
        summary = connection.execute(
            f"SELECT {', '.join(aggregates)} FROM snapshot"
        ).fetchall()[0]
        row_count = summary[0]
        for index, item in enumerate(wide):
            bounds = summary[1 + index * 2 : 3 + index * 2]
            if any(value is not None and abs(value) > 2**53 - 1 for value in bounds):
                item["kind"] = "text"
        for item in metadata:
            if item["type"].startswith("DECIMAL("):
                item["kind"] = "text"
        identifier, columns = view_id, metadata
    finally:
        connection.unregister("input_frame")
    emit({"id": identifier, "columns": columns, "row_count": row_count}, output)


def column(field: str) -> str:
    if field not in {item["field"] for item in columns}:
        raise ValueError(f"Unknown column: {field}")
    return '"' + field + '"'


def predicate(field: str, model: dict[str, Any], values: list[Any]) -> str:
    name = column(field)
    operation = model["type"]
    kind = model["filterType"]
    if operation in {"blank", "notBlank"}:
        blank = (
            f"({name} IS NULL OR regexp_full_match(CAST({name} AS VARCHAR), '[[:space:]]*'))"
            if kind == "text"
            else f"{name} IS NULL"
        )
        return f"NOT ({blank})" if operation == "notBlank" else blank
    assert kind in {"text", "number", "date"}, "Unknown filter type"
    value = model.get("dateFrom") if kind == "date" else model.get("filter")
    if kind == "text":
        assert isinstance(value, str), "Text filters require text"
        name = f"lower(CAST({name} AS VARCHAR))"
        value = value.lower()
        if operation in {"contains", "notContains", "startsWith", "endsWith"}:
            values.append(value)
            if operation in {"contains", "notContains"}:
                return (
                    f"strpos({name}, ?) {'=' if operation == 'notContains' else '>'} 0"
                )
            function = "starts_with" if operation == "startsWith" else "ends_with"
            return f"{function}({name}, ?)"
    elif kind == "date":
        assert isinstance(value, str), "Date filters require a date"
        value = datetime.date.fromisoformat(value[:10])
        name = f"CAST({name} AS DATE)"
    else:
        assert isinstance(value, (float, int)) and math.isfinite(value), (
            "Number filters require a finite number"
        )
    comparisons = {
        "equals": "=",
        "notEqual": "!=",
        "lessThan": "<",
        "lessThanOrEqual": "<=",
        "greaterThan": ">",
        "greaterThanOrEqual": ">=",
    }
    values.append(value)
    if operation == "inRange":
        upper = model.get("dateTo") if kind == "date" else model.get("filterTo")
        if kind == "date":
            upper = datetime.date.fromisoformat(upper[:10])
        else:
            assert isinstance(upper, (float, int)) and math.isfinite(upper), (
                "Range filters require a finite upper value"
            )
        values.append(upper)
        return f"{name} BETWEEN ? AND ?"
    assert operation in comparisons, "Unknown filter comparison"
    return f"{name} {comparisons[operation]} ?"


def rows(
    view_id: str,
    start: int,
    end: int,
    sort_model: list[dict[str, str]],
    filter_model: dict[str, Any],
    output: str,
) -> None:
    assert view_id == identifier, "Table was closed or replaced; reopen it"
    assert 0 <= start < end and end - start <= 200, "Request 1–200 rows"
    values: list[Any] = []
    where = (
        " AND ".join(
            predicate(field, model, values) for field, model in filter_model.items()
        )
        or "TRUE"
    )
    order = []
    for item in sort_model:
        direction = item["sort"]
        assert direction in {"asc", "desc"}, "Unknown sort direction"
        order.append(f"{column(item['colId'])} {direction.upper()} NULLS LAST")
    order.append("row_id ASC")
    count = connection.execute(
        f"SELECT count(*) FROM snapshot WHERE {where}", values
    ).fetchall()[0][0]
    result = connection.execute(
        f"SELECT * FROM snapshot WHERE {where} ORDER BY {', '.join(order)} LIMIT ? OFFSET ?",
        [*values, end - start, start],
    ).fetchall()
    clipped = False

    def scalar(value: Any) -> Any:
        nonlocal clipped
        if isinstance(value, str) and len(value) > 1024:
            clipped = True
            return value[:1024] + "…"
        if isinstance(value, int) and abs(value) > 2**53 - 1:
            return str(value)
        if isinstance(value, (datetime.date, datetime.datetime)):
            return value.isoformat()
        if isinstance(value, decimal.Decimal):
            return str(value)
        if isinstance(value, float) and not math.isfinite(value):
            return str(value)
        return value

    data = [
        {
            "rowId": str(record[0]),
            **{
                item["field"]: scalar(value) for item, value in zip(columns, record[1:])
            },
        }
        for record in result
    ]
    emit({"rows": data, "row_count": count, "truncated": clipped}, output)
