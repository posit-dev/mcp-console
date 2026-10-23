"""Test-only startup behavior for selected-executable inspection."""

import os
import socket
import sys
import sysconfig
from pathlib import Path

root = Path(__file__).parent
mode = (root / "inspection-mode").read_text(encoding="utf-8").strip()

if mode.startswith("inspection-"):
    # Inspection invokes isolated Python with -c and one result-path argument.
    # Synchronize on that invocation, independently of temporary file names.
    inspecting = sys.flags.isolated and len(sys.argv) == 2 and sys.argv[0] == "-c"
    mode = mode.removeprefix("inspection-") if inspecting else "quiet"
    if inspecting:
        with (root / "inspection-count").open("a") as count:
            count.write("1\n")

if mode == "quiet":
    pass
elif mode == "output":
    print("startup output before inspection result")
elif mode == "old-version":
    sys.version_info = (3, 9, 0, "final", 0)
elif mode in ("missing", "unusable", "other-library"):
    original_get_config_var = sysconfig.get_config_var

    def fixture_config_var(name: str):
        if name == "LDLIBRARY":
            return (
                "missing-embedding-library.so"
                if mode == "missing"
                else "fake-library.so"
            )
        if mode in ("unusable", "other-library") and name in (
            "LIBDIR",
            "PYTHONFRAMEWORKPREFIX",
        ):
            return str(root)
        return original_get_config_var(name)

    sysconfig.get_config_var = fixture_config_var
elif mode == "checkpoint":
    (root / "inspection-pid").write_text(str(os.getpid()), encoding="utf-8")
    (root / "inspection-result").write_text(sys.argv[1], encoding="utf-8")
    with (root / "inspection-ready").open("wb", buffering=0) as ready:
        ready.write(b"1")
    with (root / "inspection-release").open("rb", buffering=0) as release:
        release.read(1)
elif mode == "wait":
    address = (root / "inspection-socket").read_text(encoding="utf-8")
    connection = socket.socket(socket.AF_UNIX)
    connection.connect(address)
    connection.sendall(f"{os.getpid()}\n".encode())
    connection.recv(1)
else:
    raise ValueError(f"unknown inspection mode: {mode}")
