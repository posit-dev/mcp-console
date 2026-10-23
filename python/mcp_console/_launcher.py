"""Keep the Windows command bound to its installed native Console bundle."""

from importlib.metadata import distribution
import subprocess
import sys


def main() -> int:
    native = next(
        (
            path.locate()
            for path in distribution("mcp-console").files or ()
            if path.parts[-2:] == ("libexec", "mcp-console.exe")
        ),
        None,
    )
    if native is None:
        print("The installed native MCP Console executable is missing", file=sys.stderr)
        return 1
    # Private worker invocations carry explicitly inheritable native events
    # and pipes. Keep those handles intact while forwarding the command.
    with subprocess.Popen([str(native), *sys.argv[1:]], close_fds=False) as process:
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
