# MCP Console's private Python DB-API SQL backend.
import _mcp_console as _runtime
import _mcp_console_services as _services
import builtins as _builtins
import traceback as _traceback
import unicodedata as _unicodedata
from types import SimpleNamespace as _SimpleNamespace

_PREVIEW_ROWS = 20
_PREVIEW_COLUMNS = 12
_CELL_WIDTH = 160
_PREVIEW_WIDTH = 200
_RESPONSE_BYTES = 12 * 1024
_PROVIDER_R = 0
_PROVIDER_MANAGED = 1
_PROVIDER_HANDLED = 2
_UNSET = object()
_r_selected = False
_last_result = None
_result_epoch = 0


def _close_candidates(connection: object, candidates: tuple) -> None:
    seen = set()
    for candidate in candidates:
        if candidate is None or id(candidate) in seen:
            continue
        seen.add(id(candidate))
        try:
            if candidate is connection:
                _clear_duckdb_result(connection)
                continue
            close = getattr(candidate, "close", None)
            if callable(close):
                close()
        except Exception as error:
            print(f"Error: {error}")
        except BaseException:
            _traceback.print_exc()


def _release_result(result: dict) -> None:
    connection, candidates = result["connection"], result["candidates"]
    result["connection"], result["candidates"], result["cursor"] = None, (), None
    # Cleanup also runs from language cells, outside the SQL dispatch guard.
    _runtime.without_automatic_resolution(_close_candidates, connection, candidates)


def _invalidate_result() -> None:
    global _last_result, _result_epoch
    _result_epoch += 1
    result, _last_result = _last_result, None
    if result is not None:
        _release_result(result)


def sql_last_result(n: int | None = None) -> dict:
    if n is not None and (type(n) is not int or n < 0):
        raise ValueError("`n` must be a nonnegative integer or None")
    if _last_result is None:
        raise RuntimeError(
            "No retained Python SQL result; retrieve in the connection's owning language"
        )
    result = _last_result
    try:
        take = len(result["rows"]) if n is None else min(n, len(result["rows"]))
        rows = result["rows"][:take]
        del result["rows"][:take]
        remaining = None if n is None else n - take
        cursor = result["cursor"]
        if cursor is not None:
            if remaining is None:
                rows.extend(cursor.fetchall())
                _release_result(result)
            else:
                while remaining > 0:
                    # Native drivers need a bounded integer even when n is huge.
                    size = min(remaining, 65536)
                    fetched = list(cursor.fetchmany(size))
                    rows.extend(fetched)
                    remaining -= len(fetched)
                    if not fetched:
                        _release_result(result)
                        break
                    if len(fetched) < size:
                        break
        return {"columns": result["columns"], "rows": rows}
    except BaseException:
        _invalidate_result()
        raise


try:
    _connection
except NameError:
    _connection = None

try:
    _restore_managed
except NameError:
    _restore_managed = False


def _validate_connection(connection):
    cursor = getattr(connection, "cursor", None)
    if not callable(cursor):
        raise TypeError(
            "`connection` must provide a callable cursor() method or be None"
        )


def sql_connection(connection: object = _UNSET) -> object:
    global _connection, _restore_managed, _r_selected

    if connection is _UNSET:
        if _connection is None:
            if not _native_enabled:
                raise RuntimeError(
                    "The active SQL connection belongs to R; use .console$sql_connection() in R"
                )
            _connection = _ensure_managed_connection()
        return _connection

    if connection is None:
        _services.close_r_sql_result()
        _invalidate_result()
        _r_selected = False
        _connection = None
        _restore_managed = True
        return None

    _validate_connection(connection)
    _services.close_r_sql_result()
    _invalidate_result()
    _r_selected = False
    _connection = connection
    _restore_managed = False
    return None


def has_selected_connection() -> bool:
    return _connection is not None


def use_r():
    global _connection, _restore_managed, _r_selected

    _invalidate_result()
    _r_selected = True

    _connection = None
    _restore_managed = False
    return None


def _display_cell(value):
    if value is None:
        return "NULL", False

    text = _escape_nonprinting(repr(value))
    if len(text) > _CELL_WIDTH:
        return text[: _CELL_WIDTH - 1] + "…", True
    return text, False


def _display_name(value):
    text = _escape_nonprinting(str(value))
    if len(text) > _CELL_WIDTH:
        return text[: _CELL_WIDTH - 1] + "…"
    return text


def _escape_nonprinting(text):
    text = "".join(
        character if character.isprintable() else repr(character)[1:-1]
        for character in text
    )
    return text


def _fit(text, width):
    if _display_width(text) <= width:
        return text
    if width == 1:
        return "…"
    return _slice_to_width(text, width - 1) + "…"


def _character_width(character):
    if _unicodedata.combining(character):
        return 0
    if _unicodedata.category(character) in {"Cf", "Me"}:
        return 0
    if _unicodedata.east_asian_width(character) in {"F", "W"}:
        return 2
    return 1


def _display_width(text):
    return sum(_character_width(character) for character in text)


def _slice_to_width(text, width):
    result = []
    used = 0
    for character in text:
        character_width = _character_width(character)
        if used + character_width > width:
            break
        result.append(character)
        used += character_width
    return "".join(result)


def _pad(text, width):
    return text + " " * (width - _display_width(text))


def _column_widths(names, rows, columns):
    widths = []
    for index in range(columns):
        width = _display_width(names[index])
        for row in rows:
            width = max(width, _display_width(row[index][0]))
        widths.append(min(_CELL_WIDTH, max(1, width)))

    available = _PREVIEW_WIDTH - 3 * (columns - 1)
    while sum(widths) > available:
        widest = max(range(columns), key=widths.__getitem__)
        widths[widest] -= 1
    return widths


def _format_preview(
    names,
    rows,
    total_columns,
    fetched_rows,
    more_rows,
    visible_rows,
    visible_columns,
):
    widths = _column_widths(names, rows[:visible_rows], visible_columns)

    def line(values):
        return " | ".join(
            _pad(_fit(values[index], widths[index]), widths[index])
            for index in range(visible_columns)
        ).rstrip()

    lines = [
        line(names),
        "-+-".join("-" * width for width in widths),
    ]
    lines.extend(line([cell[0] for cell in row]) for row in rows[:visible_rows])

    if fetched_rows == 0:
        lines.append("[0 rows]")
    if more_rows or visible_rows < fetched_rows:
        lines.append("[additional rows omitted]")
    omitted = total_columns - visible_columns
    if omitted:
        suffix = "column" if omitted == 1 else "columns"
        lines.append(f"[{omitted} additional {suffix} omitted]")
    if any(cell[1] for row in rows[:visible_rows] for cell in row[:visible_columns]):
        lines.append(f"[cell values truncated to {_CELL_WIDTH} characters]")
    return "\n".join(lines)


def _fallback_show(cursor):
    description = cursor.description or ()
    total_columns = len(description)
    if total_columns == 0:
        return

    columns = min(total_columns, _PREVIEW_COLUMNS)
    names = [_display_name(description[index][0]) for index in range(columns)]
    fetched = list(cursor.fetchmany(_PREVIEW_ROWS + 1))
    values = fetched[:_PREVIEW_ROWS]
    rows = [[_display_cell(row[index]) for index in range(columns)] for row in values]
    visible_rows = len(rows)
    visible_columns = columns

    while True:
        output = _format_preview(
            names,
            rows,
            total_columns,
            len(values),
            len(fetched) > _PREVIEW_ROWS,
            visible_rows,
            visible_columns,
        )
        if len(output.encode("utf-8")) + 1 <= _RESPONSE_BYTES:
            print(output)
            return fetched
        if visible_rows > 0:
            visible_rows -= 1
        elif visible_columns > 1:
            visible_columns -= 1
        else:
            raise RuntimeError("SQL preview cannot fit within the response budget")


def _evaluate(source):
    global _last_result
    connection = _connection
    if connection is None:
        raise RuntimeError("no Python DB-API connection is selected")

    executor = None
    result = None
    retained = False
    epoch = _result_epoch
    try:
        execute = getattr(connection, "execute", None)
        executor = connection if callable(execute) else connection.cursor()
        result = executor.execute(source)
        result_cursor = (
            result if getattr(result, "description", None) is not None else executor
        )
        if getattr(result_cursor, "description", None) is None:
            return None
        rows = _fallback_show(result_cursor)
        columns = tuple(column[0] for column in result_cursor.description)
        # Driver callbacks can select another connection while reading metadata.
        # Display this cell, but do not publish a result after its invalidation.
        if epoch == _result_epoch:
            _last_result = {
                "connection": connection,
                "candidates": (result, executor),
                "cursor": result_cursor,
                "rows": rows,
                "columns": columns,
            }
            retained = True
            if not rows:
                _release_result(_last_result)
    except Exception as error:
        rendered = _traceback.TracebackException.from_exception(error)
        _services.report_execution_error(error, str(rendered))
        print(f"Error: {rendered}")
    except BaseException as error:
        rendered = _traceback.TracebackException.from_exception(error)
        _services.report_execution_error(error, str(rendered))
        rendered.print()
    finally:
        if not retained:
            _close_candidates(connection, (result, executor))
    return None


def _dispatch(source):
    _invalidate_result()
    if _r_selected:
        return _PROVIDER_R
    if _connection is not None or _select_native_connection():
        _evaluate(source)
        return _PROVIDER_HANDLED
    if _restore_managed and not _native_enabled:
        use_r()
        return _PROVIDER_MANAGED
    return _PROVIDER_HANDLED if _native_enabled else _PROVIDER_R


def dispatch(source):
    return _runtime.without_automatic_resolution(_dispatch, source)


_builtins._console = _SimpleNamespace(
    sql_connection=sql_connection, sql_last_result=sql_last_result
)


def take_managed_restore_request():
    global _restore_managed
    requested = _restore_managed
    _restore_managed = False
    return requested


# Native Python sessions use the same evaluator and preview formatter. Keep
# their connection setup below the R-present adapter so its traceback lines
# remain stable in public transcripts.
import os as _os
from pathlib import Path as _Path

_native_enabled = False
_native_storage = None
_native_extension_directory = None
_managed_connection = None


def enable_native() -> None:
    global _native_enabled, _native_storage, _native_extension_directory

    _native_enabled = True
    if _os.environ.get("MCP_CONSOLE_MANAGED_TEMPDIR") == "1":
        _native_storage = _Path(_os.environ["TMPDIR"]) / "mcp-console-duckdb"
    _native_extension_directory = _os.environ.get(
        "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY", ""
    )


def _ensure_managed_connection():
    global _managed_connection

    if _managed_connection is None:
        try:
            import duckdb
        except ImportError as error:
            message = (
                "DuckDB is unavailable; add duckdb with requirements.python and control: restart "
                "in a managed session, install it before starting a selected Python environment, "
                "or select a DB-API connection with _console.sql_connection(connection)"
            )
            raise RuntimeError(message) from error
        config = {
            "extension_directory": _native_extension_directory,
            "python_enable_replacements": "false",
        }
        # Only the sandbox's managed mode owns DuckDB temporary storage.
        # Other modes retain DuckDB's ordinary storage settings and lifetime.
        if _native_storage is not None:
            config["secret_directory"] = str(_native_storage / "stored-secrets")
            config["temp_directory"] = str(_native_storage / "spill")
        connection = duckdb.connect(":memory:", config=config)
        try:
            connection.execute("SET enable_progress_bar = false")
        except BaseException:
            try:
                connection.close()
            except BaseException:
                _traceback.print_exc()
            raise
        _managed_connection = connection
    return _managed_connection


def initialize_managed_connection() -> None:
    try:
        import importlib.metadata
        import importlib.util

        specification = importlib.util.find_spec("duckdb")
        if specification is None or not specification.submodule_search_locations:
            return
        try:
            distribution = importlib.metadata.distribution("duckdb")
        except importlib.metadata.PackageNotFoundError:
            return
        path = _os.path.realpath(distribution.locate_file("duckdb"))
        # A workspace package can shadow an installed optional provider. Bootstrap
        # must not execute that candidate or enter managed missing-import resolution.
        if list(map(_os.path.realpath, specification.submodule_search_locations)) != [
            path
        ]:
            return
        _runtime.without_automatic_resolution(_ensure_managed_connection)
    except KeyboardInterrupt:
        raise
    except BaseException:
        _traceback.print_exc()


def _select_native_connection():
    global _connection

    if not _native_enabled:
        return False
    try:
        _connection = _ensure_managed_connection()
    except Exception as error:
        rendered = _traceback.TracebackException.from_exception(error)
        _services.report_execution_error(error, str(rendered))
        print(f"Error: {rendered}")
        return False
    return True


def initialize_connection(source: str) -> int:
    import __main__

    try:
        exec(compile(source, "<console startup>", "exec"), __main__.__dict__)
        if _connection is None or _connection is _managed_connection or _r_selected:
            raise RuntimeError(
                "startup must select a native connection with _console.sql_connection(connection)"
            )
        # DB-API has no common is-open predicate. Probe without executing a query,
        # and close only cursors distinct from the user-owned connection.
        cursor = _connection.cursor()
        if cursor is not _connection:
            cursor.close()
        return 1
    except BaseException as error:
        rendered = _traceback.TracebackException.from_exception(error)
        if isinstance(error, SyntaxError):
            # Configuration is not a model-visible cell. Keep the syntax location
            # and diagnosis without echoing a potentially credential-bearing line.
            rendered.text = None
        print("".join(rendered.format()), end="", file=__import__("sys").stderr)
        return -1 if isinstance(error, KeyboardInterrupt) else 0
    finally:
        _runtime.finalize_plots()


def r_connection_selected() -> bool:
    return _r_selected and not _restore_managed


def _clear_duckdb_result(connection: object) -> None:
    import sys

    duckdb = sys.modules.get("duckdb")
    if duckdb is not None and isinstance(connection, duckdb.DuckDBPyConnection):
        # DuckDB 2 keeps unfinished execute() results attached to the connection.
        # An empty execute supersedes the result on both 1.x and 2.x: it releases
        # reads and completes pending writes without fetching rows, closing the
        # connection, or committing/rolling back its transaction. abort() would
        # instead invalidate a transaction whose unfinished statement writes.
        connection.execute("")
