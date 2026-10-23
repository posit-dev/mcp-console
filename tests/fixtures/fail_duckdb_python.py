"""A sitecustomize fixture affecting only the extension materializer."""

import sys
from typing import Any, TextIO

if any("mcp-console-duckdb-builtins-" in argument for argument in sys.orig_argv):
    import json

    load = json.load

    def extension_request(stream: TextIO, *args: Any, **kwargs: Any) -> dict[str, Any]:
        request = load(stream, *args, **kwargs)
        if "sqlite" in request.get("extensions", []):
            raise SystemExit("fixture DuckDB extension sqlite is unavailable")
        return request

    json.load = extension_request
