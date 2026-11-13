"""Keep the public command bound to this Python package's native bundle."""

from importlib.metadata import distribution
import os
from pathlib import Path
import subprocess
import sys

from ._binary import InstallError, MANIFEST, native_binary


def _pinned() -> Path:
    override = os.environ.get("MCP_CONSOLE_BINARY")
    if override is None:
        return native_binary()
    # Naming this command would start it again without end.
    if Path(override).resolve() == Path(sys.argv[0]).resolve():
        raise InstallError("MCP_CONSOLE_BINARY names the Python launcher itself.")
    return Path(override)


def main() -> int:
    if MANIFEST.is_file():
        try:
            native = _pinned()
        except (InstallError, OSError) as error:
            print(f"mcp-console: {error}", file=sys.stderr)
            return 1
        except KeyboardInterrupt:
            return 130
    else:
        # A native Windows wheel installs its bundle with the distribution.
        native = next(
            (
                path.locate()
                for path in distribution("mcp-console").files or ()
                if path.parts[-2:] == ("libexec", "mcp-console.exe")
            ),
            None,
        )
        if native is None:
            print(
                "The installed native MCP Console executable is missing",
                file=sys.stderr,
            )
            return 1
    if sys.platform != "win32":
        # Retain PID, signals, inherited descriptors, and the native exit code.
        try:
            os.execv(native, [str(native), *sys.argv[1:]])
        except OSError as error:
            print(f"mcp-console: cannot execute {native}: {error}", file=sys.stderr)
            return 1
    # Private worker invocations carry explicitly inheritable native events
    # and pipes. Keep those handles intact while forwarding the command.
    try:
        process = subprocess.Popen([str(native), *sys.argv[1:]], close_fds=False)
    except OSError as error:
        print(f"mcp-console: cannot execute {native}: {error}", file=sys.stderr)
        return 1
    with process:
        while True:
            try:
                status = process.wait()
                # Python's exit status accepts a signed Windows C int, while
                # native exception/exit statuses retain all 32 bits.
                return status if status < 2**31 else status - 2**32
            except KeyboardInterrupt:
                # Console control events already reach the native process.
                # Retain ownership until its shutdown has finished.
                continue


if __name__ == "__main__":
    raise SystemExit(main())
