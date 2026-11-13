"""Install the native bundle pinned by a thin Python wheel.

Imports do no I/O. The wheel's manifest pins each bundle's size and SHA-256,
so a verified archive is trusted like the wheel's own code: its members are
extracted without further inspection. The cache is user-owned storage, not a
security boundary, and a published bundle directory is never modified. The
native executable keeps its own checks of the companion and its artifacts.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
from http.client import HTTPException
import json
import os
from pathlib import Path
import platform
import sys
import sysconfig
import tempfile
import time
from urllib.request import urlopen
import zipfile


MANIFEST = Path(__file__).with_name("_artifacts.json")
RELEASES = "https://github.com/posit-dev/mcp-console/releases/download"
SYSTEMS = {
    "darwin": "apple-darwin",
    "linux": "unknown-linux-gnu",
    "win32": "pc-windows-msvc",
}


class InstallError(Exception):
    """The pinned native bundle cannot be selected or installed."""


def _target() -> str:
    # The interpreter's architecture selects the Windows bundle, as it selected
    # the native wheel; this also avoids platform.machine()'s WMI query on every
    # launch. A 32-bit Python on a 64-bit Unix kernel is unsupported.
    if sys.platform == "win32":
        machine = sysconfig.get_platform().removeprefix("win-")
    else:
        machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    return f"{architecture}-{SYSTEMS.get(sys.platform, sys.platform)}"


def _require_host(artifact: dict) -> None:
    # A universal wheel has no platform tag to enforce the native floors.
    if sys.platform == "darwin":
        name, actual = "macOS", platform.mac_ver()[0]
        minimum = artifact["minimum_macos"]
    elif sys.platform == "linux":
        name, minimum = "glibc", artifact["minimum_glibc"]
        library, actual = platform.libc_ver()
        if library != "glibc":
            actual = "0"
    else:
        return
    if [*map(int, actual.split("."))] < [*map(int, minimum.split("."))]:
        raise InstallError(f"This native release requires {name} {minimum} or newer.")


def _cache_root() -> Path:
    override = os.environ.get("MCP_CONSOLE_BINARY_CACHE")
    if override is not None:
        if not Path(override).is_absolute():
            raise InstallError(
                "MCP_CONSOLE_BINARY_CACHE must be an absolute directory."
            )
        return Path(override)
    if sys.platform == "win32":
        base = Path(os.environ["LOCALAPPDATA"])
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        candidate = Path(os.environ.get("XDG_CACHE_HOME", ""))
        base = candidate if candidate.is_absolute() else Path.home() / ".cache"
    return base / "mcp-console/native"


@contextmanager
def _install_lock(path: Path):
    # Never unlink a lock file: waiters must continue to use the same inode.
    # Both operating systems release the lock if the installing process dies.
    with path.open("a+b") as stream:
        if sys.platform == "win32":
            import msvcrt

            stream.seek(0, os.SEEK_END)
            if stream.tell() == 0:
                stream.write(b"\0")
                stream.flush()
            deadline = time.monotonic() + 600
            while True:
                stream.seek(0)
                try:
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError as error:
                    if time.monotonic() >= deadline:
                        raise InstallError(
                            "Timed out waiting for another native installer."
                        ) from error
                    time.sleep(0.2)
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)


def _copy_archive(source, destination: Path, artifact: dict) -> None:
    digest = hashlib.sha256()
    size = 0
    with destination.open("xb") as output:
        # Stop at the pinned size rather than store an unbounded response.
        while size <= artifact["size"] and (chunk := source.read(1024 * 1024)):
            size += len(chunk)
            digest.update(chunk)
            output.write(chunk)
    if size != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise InstallError(
            "Native archive size or SHA-256 does not match the installed Python package."
        )


def native_binary(*, archive: Path | None = None, download: bool = True) -> Path:
    manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    version, target = manifest["version"], _target()
    artifact = manifest["artifacts"].get(target)
    if artifact is None:
        raise InstallError(
            f"MCP Console {version} has no native release for {target}. "
            "Set MCP_CONSOLE_BINARY to run a locally built executable."
        )
    _require_host(artifact)
    parent = _cache_root() / version / target
    prefix = parent / artifact["sha256"]
    native = prefix / (
        "libexec/mcp-console.exe" if sys.platform == "win32" else "bin/mcp-console"
    )
    # A bundle directory is published by one rename, so it is never partial.
    if native.is_file():
        return native
    if archive is None:
        if not download:
            raise InstallError(
                "Native bundle is not installed; run mcp-console-install first."
            )
        if os.environ.get("MCP_CONSOLE_OFFLINE", "").lower() in {"1", "true", "yes"}:
            raise InstallError(
                "Native bundle is not cached and MCP_CONSOLE_OFFLINE is set; "
                "use mcp-console-install --archive PATH."
            )
    parent.mkdir(parents=True, exist_ok=True)
    with _install_lock(parent / f"{artifact['sha256']}.lock"):
        if native.is_file():
            return native
        print(
            f"Installing MCP Console {version} for {target}...",
            file=sys.stderr,
            flush=True,
        )
        # Staging and destination share a filesystem.
        with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as temporary:
            payload = Path(temporary) / "download.zip"
            if archive is not None:
                with archive.open("rb") as source:
                    _copy_archive(source, payload, artifact)
            else:
                url = f"{RELEASES}/v{version}/mcp-console-{version}-{target}.zip"
                try:
                    # The pinned digest authenticates the archive wherever
                    # redirects lead.
                    with urlopen(url, timeout=30) as source:
                        _copy_archive(source, payload, artifact)
                except (OSError, HTTPException) as error:
                    raise InstallError(f"Cannot download {url}: {error}") from error
            bundle = Path(temporary) / "bundle"
            with zipfile.ZipFile(payload) as source:
                source.extractall(bundle)
                for info in source.infolist():
                    if info.external_attr >> 16 & 0o111 and not info.is_dir():
                        (bundle / info.filename).chmod(0o755)
            bundle.rename(prefix)
    return native


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prepare the native MCP Console bundle pinned by this Python package."
    )
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--archive",
        type=Path,
        help="install the pinned ZIP from a local file (works offline)",
    )
    options.add_argument(
        "--print-path",
        action="store_true",
        help="print the installed executable without downloading",
    )
    args = parser.parse_args()
    try:
        print(native_binary(archive=args.archive, download=not args.print_path))
        return 0
    except (InstallError, OSError) as error:
        print(f"mcp-console-install: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
