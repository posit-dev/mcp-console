"""Install validated extension names with the selected managed Python DuckDB."""

from contextlib import suppress
import json
import os
from pathlib import Path
import re
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory

request = json.load(sys.stdin)
if request.get("check_available"):
    import importlib.util

    print(json.dumps(importlib.util.find_spec("duckdb") is not None))
    sys.exit(0)

import importlib.metadata

extensions = request["extensions"]
assert isinstance(extensions, list) and extensions
assert all(
    isinstance(name, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
    for name in extensions
)


def file_identity(path: Path) -> list[str | int]:
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def preparation_identity(directory: Path) -> str:
    distribution = importlib.metadata.distribution("duckdb")
    package_files = sorted(distribution.files or [])
    if not package_files:
        raise FileNotFoundError("DuckDB package file inventory is unavailable")
    identity = {
        "recipe": request["recipe"],
        "extensions": sorted(extensions),
        "runtime": [sys.version, sys.prefix, file_identity(Path(sys.executable))],
        "package": [
            distribution.version,
            [
                file_identity(Path(distribution.locate_file(path)))
                for path in package_files
                if Path(distribution.locate_file(path)).is_file()
            ],
        ],
        "cache": str(directory.resolve()),
        "artifacts": [
            file_identity(path)
            for path in sorted(directory.rglob("*.duckdb_extension*"))
            if path.is_file()
        ],
    }
    return json.dumps(identity)


directory = Path(request["extension_directory"])
receipt = directory / ".mcp-console-python-duckdb.json"
# Receipts are cache hints, not executable selections or filesystem grants.
# Compare current identities directly; no path is taken from cached data.
try:
    identity = preparation_identity(directory)
    if receipt.is_file() and receipt.read_bytes() == identity.encode():
        sys.exit(0)
except (OSError, importlib.metadata.PackageNotFoundError):
    pass

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

# A failed install never publishes successful preparation. Cache receipt I/O
# remains optional; DuckDB's own native artifacts and validation are authoritative.
temporary = None
try:
    directory.mkdir(parents=True, exist_ok=True)
    identity = preparation_identity(directory)
    with NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(identity)
    os.replace(temporary, receipt)
except (OSError, importlib.metadata.PackageNotFoundError):
    pass
finally:
    if temporary is not None:
        with suppress(OSError):
            temporary.unlink(missing_ok=True)
