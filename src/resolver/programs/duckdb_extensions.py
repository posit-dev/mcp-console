"""Install validated extension names with the selected managed Python DuckDB."""

import json
import os
import re
import sys
from tempfile import TemporaryDirectory

request = json.load(sys.stdin)
if request.get("check_available"):
    import importlib.util

    print(json.dumps(importlib.util.find_spec("duckdb") is not None))
    sys.exit(0)

extensions = request["extensions"]
assert isinstance(extensions, list) and extensions
assert all(
    isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
    for name in extensions
)

try:
    import duckdb
except ImportError as error:
    raise SystemExit(
        "DuckDB extension preparation needs an importable duckdb package in the "
        "candidate Python environment; include duckdb in requirements.python"
    ) from error

with TemporaryDirectory(prefix="mcp-console-duckdb-builtins-") as builtin_directory:
    # Discover built-ins without reading another installer's metadata.
    config = {"extension_directory": builtin_directory}
    if os.environ.get("CODEX_NETWORK_PROXY_ACTIVE") == "1":
        # Extension INSTALL does not consume the proxy environment itself.
        config["http_proxy"] = os.environ["HTTP_PROXY"]
    connection = duckdb.connect(
        ":memory:",
        config=config,
    )
    connection.execute("SET enable_progress_bar = false")
    try:
        builtin_extensions = {
            name
            for (name,) in connection.execute(
                "SELECT unnest(aliases || [extension_name]) FROM duckdb_extensions() "
                "WHERE install_path = '(BUILT-IN)'"
            ).fetchall()
        }
        # Installation uses the captured shared cache.
        # Older DuckDB versions cannot bind SET values.
        extension_directory = request["extension_directory"].replace("'", "''")
        connection.execute(f"SET extension_directory = '{extension_directory}'")
        for extension in extensions:
            if extension not in builtin_extensions:
                connection.install_extension(extension)
    finally:
        connection.close()
