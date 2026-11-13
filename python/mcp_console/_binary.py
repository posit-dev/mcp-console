"""Install the native bundle pinned by a published Python wheel.

Imports do no I/O. The cache is trusted user-owned storage, not a second
sandbox. Archive hashes bind downloads to the wheel; the native executable
retains its own checks of the companion and its supporting artifacts.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import sys
import tempfile
import time
from urllib.error import URLError
from urllib.request import urlopen
import zipfile


MANIFEST = Path(__file__).with_name("_artifacts.json")
RELEASES = "https://github.com/posit-dev/mcp-console/releases/download"


class InstallError(Exception):
    """A native installation cannot be selected or completed."""


def _target() -> str:
    machine = platform.machine().lower()
    architecture = {"amd64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    systems = {
        "darwin": "apple-darwin",
        "linux": "unknown-linux-gnu",
        "win32": "pc-windows-msvc",
    }
    if (
        sys.platform not in systems
        or architecture not in {"x86_64", "aarch64"}
        or struct.calcsize("P") != 8
        or (sys.platform == "win32" and architecture != "x86_64")
    ):
        raise InstallError(
            f"No native MCP Console release for {sys.platform}/{machine}."
        )
    if sys.platform == "linux":
        # A universal wheel has no manylinux tag to enforce the native floor.
        try:
            libc = os.confstr("CS_GNU_LIBC_VERSION") or ""
        except (OSError, ValueError):
            libc = ""
        match = re.fullmatch(r"glibc (\d+)\.(\d+)", libc)
        if match is None or tuple(map(int, match.groups())) < (2, 35):
            raise InstallError(
                "MCP Console requires Linux with glibc 2.35 or newer; musl is unsupported."
            )
    return f"{architecture}-{systems[sys.platform]}"


def _selection() -> tuple[dict, str, dict]:
    try:
        manifest = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InstallError(
            "This installation has no native release manifest. Install a published "
            "mcp-console wheel, or set MCP_CONSOLE_BINARY to a locally built executable."
        ) from error
    except (OSError, ValueError) as error:
        raise InstallError(f"Cannot read native release manifest: {error}") from error
    target = _target()
    try:
        version = manifest["version"]
        artifact = manifest["artifacts"][target]
        expected_executable = (
            "libexec/mcp-console.exe" if sys.platform == "win32" else "bin/mcp-console"
        )
        valid = (
            manifest["schema"] == 1
            and re.fullmatch(r"\d+\.\d+\.\d+(?:[.+-][A-Za-z0-9.]+)?", version)
            and manifest["release"] == f"v{version}"
            and artifact["filename"] == f"mcp-console-{version}-{target}.zip"
            and re.fullmatch(r"[0-9a-f]{64}", artifact["sha256"])
            and type(artifact["size"]) is int
            and 0 < artifact["size"] <= 1024**3
            and artifact["executable"] == expected_executable
        )
        if not valid:
            raise ValueError("invalid release fields")
        if sys.platform == "darwin" and "minimum_macos" in artifact:
            minimum = tuple(map(int, artifact["minimum_macos"].split(".")))
            actual = tuple(map(int, platform.mac_ver()[0].split(".")))
            if actual < minimum:
                raise InstallError(
                    f"This native release requires macOS {artifact['minimum_macos']} or newer."
                )
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise InstallError(
            f"Invalid native release manifest for {target}: {error}"
        ) from error
    return manifest, target, artifact


def _cache_root() -> Path:
    override = os.environ.get("MCP_CONSOLE_BINARY_CACHE")
    if override is not None:
        root = Path(override).expanduser()
        if not override or not root.is_absolute():
            raise InstallError(
                "MCP_CONSOLE_BINARY_CACHE must be an absolute directory."
            )
        return root
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData/Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Caches"
    else:
        candidate = Path(os.environ.get("XDG_CACHE_HOME", ""))
        base = candidate if candidate.is_absolute() else Path.home() / ".cache"
    return base / "mcp-console/native"


def _required_files(target: str, artifact: dict) -> list[str]:
    suffix = ".exe" if "windows" in target else ""
    paths = [
        artifact["executable"],
        f"libexec/mcp-console-sandbox{suffix}",
        "share/licenses/mcp-console/LICENSE",
        "share/licenses/mcp-console/NOTICE",
    ]
    if "linux" in target:
        paths += [
            "libexec/bwrap",
            "share/licenses/mcp-console/bubblewrap-COPYING",
            "share/licenses/mcp-console/bubblewrap-NOTICE",
            "share/licenses/mcp-console/bubblewrap-SOURCE.json",
        ]
    return paths


def _complete(prefix: Path, target: str, artifact: dict) -> bool:
    marker = prefix / ".complete"
    if not marker.is_file() or marker.read_text(encoding="ascii") != artifact["sha256"]:
        return False
    return all(
        (prefix / name).is_file()
        and (
            not name.startswith(("bin/", "libexec/"))
            or os.access(prefix / name, os.X_OK)
        )
        for name in _required_files(target, artifact)
    )


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
    count = 0
    deadline = time.monotonic() + 600
    with destination.open("xb") as output:
        while chunk := source.read(1024 * 1024):
            count += len(chunk)
            if count > artifact["size"] or time.monotonic() > deadline:
                raise InstallError(
                    "Native download exceeded its expected size or time limit."
                )
            digest.update(chunk)
            output.write(chunk)
    if count != artifact["size"] or digest.hexdigest() != artifact["sha256"]:
        raise InstallError(
            "Native archive size or SHA-256 does not match the installed Python package."
        )


def _extract(archive: Path, destination: Path) -> None:
    with zipfile.ZipFile(archive) as source:
        members = source.infolist()
        if (
            len(members) > 10000
            or sum(info.file_size for info in members) > 2 * 1024**3
        ):
            raise InstallError("Native archive exceeds the extraction limits.")
        seen = set()
        for info in members:
            name = info.filename.rstrip("/") if info.is_dir() else info.filename
            path = PurePosixPath(name)
            mode = info.external_attr >> 16
            if (
                not name
                or path.is_absolute()
                or path.as_posix() != name
                or any(
                    part in {".", ".."} or part.endswith((".", " "))
                    for part in path.parts
                )
                or any(character in name for character in ("\\", ":", "\0"))
                or stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}
                or name.casefold() in seen
                or name == ".complete"
            ):
                raise InstallError(
                    f"Unsafe member in native archive: {info.filename!r}"
                )
            seen.add(name.casefold())
            output = destination.joinpath(*path.parts)
            if info.is_dir():
                output.mkdir(parents=True, exist_ok=True)
            else:
                output.parent.mkdir(parents=True, exist_ok=True)
                with source.open(info) as content, output.open("xb") as installed:
                    shutil.copyfileobj(content, installed)
                output.chmod(0o755 if mode & 0o111 else 0o644)


def native_binary(*, archive: Path | None = None, download: bool = True) -> Path:
    override = os.environ.get("MCP_CONSOLE_BINARY")
    if override is not None:
        binary = Path(override).expanduser()
        if archive is not None:
            raise InstallError(
                "Unset MCP_CONSOLE_BINARY before installing a local archive."
            )
        if (
            not override
            or not binary.is_absolute()
            or not binary.is_file()
            or not os.access(binary, os.X_OK)
        ):
            raise InstallError(
                "MCP_CONSOLE_BINARY must name an absolute native executable."
            )
        if binary.resolve() == Path(sys.argv[0]).resolve():
            raise InstallError(
                "MCP_CONSOLE_BINARY points to the Python launcher itself."
            )
        return binary
    manifest, target, artifact = _selection()
    parent = _cache_root() / manifest["version"] / target
    prefix = parent / artifact["sha256"]
    if _complete(prefix, target, artifact):
        return prefix / artifact["executable"]
    if not download and archive is None:
        raise InstallError(
            "Native bundle is not installed; run mcp-console-install first."
        )
    offline = os.environ.get("MCP_CONSOLE_OFFLINE", "").lower() in {"1", "true", "yes"}
    if offline and archive is None:
        raise InstallError(
            "Native bundle is not cached and MCP_CONSOLE_OFFLINE is set; use mcp-console-install --archive PATH."
        )
    parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with _install_lock(parent / f"{artifact['sha256']}.lock"):
        if _complete(prefix, target, artifact):
            return prefix / artifact["executable"]
        if prefix.exists():
            raise InstallError(
                f"Native cache is incomplete. Remove {prefix} and run mcp-console-install again."
            )
        print(
            f"Installing MCP Console {manifest['version']} for {target}...",
            file=sys.stderr,
            flush=True,
        )
        with tempfile.TemporaryDirectory(prefix=".install-", dir=parent) as temporary:
            work = Path(temporary)
            payload = work / "download.zip"
            if archive is not None:
                with archive.open("rb") as source:
                    _copy_archive(source, payload, artifact)
            else:
                url = f"{RELEASES}/{manifest['release']}/{artifact['filename']}"
                with urlopen(url, timeout=30) as source:
                    if not source.geturl().startswith("https://"):
                        raise InstallError(
                            "Native download redirected away from HTTPS."
                        )
                    _copy_archive(source, payload, artifact)
            bundle = work / "bundle"
            bundle.mkdir()
            _extract(payload, bundle)
            (bundle / ".complete").write_text(artifact["sha256"], encoding="ascii")
            if not _complete(bundle, target, artifact):
                raise InstallError(
                    "Native archive is missing required bundle files or executable permissions."
                )
            # Staging and destination share a filesystem. The lock serializes
            # installers; readers can see only the complete published directory.
            bundle.rename(prefix)
    return prefix / artifact["executable"]


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
    except (InstallError, OSError, URLError, ValueError, zipfile.BadZipFile) as error:
        print(f"mcp-console-install: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
