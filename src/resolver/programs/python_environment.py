"""Report uv's selected interpreter and prepare its optional plotting cache."""

from contextlib import suppress
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import tempfile


def file_identity(path: Path) -> list[str | int]:
    stat = path.stat()
    return [str(path.resolve()), stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def font_files(directory: Path) -> dict[str, list[str | int]]:
    return {
        path.name: file_identity(path)
        for path in sorted(directory.glob("fontlist-v*.json"))
        if path.is_file()
    }


def warm_matplotlib() -> None:
    if not os.environ.get("MPLCONFIGDIR"):
        import matplotlib.font_manager

        return
    directory = Path(os.environ["MPLCONFIGDIR"])
    distribution = importlib.metadata.distribution("matplotlib")
    # A receipt only skips optional cache warming. It never selects an
    # interpreter, package directory, or filesystem grant. Managed package
    # replacement and edited font-manager code invalidate its identity.
    identity = [
        sys.argv[-2],
        sys.version,
        sys.prefix,
        file_identity(Path(sys.executable)),
        distribution.version,
        file_identity(Path(distribution.locate_file("matplotlib/__init__.py"))),
        file_identity(Path(distribution.locate_file("matplotlib/font_manager.py"))),
    ]
    key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
    receipt = directory / f".mcp-console-fonts-{key}.json"
    files = font_files(directory)
    if (
        files
        and receipt.is_file()
        and receipt.read_bytes() == json.dumps(files).encode()
    ):
        return

    import matplotlib.font_manager

    # Matplotlib can use temporary storage when the requested cache is not
    # writable. That remains best effort and must not become reusable success.
    if Path(matplotlib.get_cachedir()).resolve() != directory.resolve():
        return
    files = font_files(directory)
    if files:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=directory, delete=False
            ) as stream:
                temporary = Path(stream.name)
                json.dump(files, stream)
            os.replace(temporary, receipt)
        finally:
            if temporary is not None:
                with suppress(OSError):
                    temporary.unlink(missing_ok=True)


with open(sys.argv[-1], "w", encoding="utf-8") as stream:
    stream.write(sys.executable)

# Unavailable Matplotlib and unwritable font caches have never made managed
# Python resolution fail. Only a completed warmup can publish a receipt.
try:
    warm_matplotlib()
except Exception:
    pass
