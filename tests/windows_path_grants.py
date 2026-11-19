"""Compare equivalent writable-root spellings on the Windows 11 ARM64 host."""

import ctypes
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

binary = Path("install/bin/mcp-console.exe").resolve()
short_path = ctypes.windll.kernel32.GetShortPathNameW
short_path.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint32]
short_path.restype = ctypes.c_uint32
long_temp = (Path(os.environ["USERPROFILE"]) / "AppData/Local/Temp").resolve(
    strict=True
)
buffer = ctypes.create_unicode_buffer(32768)
assert 0 < short_path(str(long_temp), buffer, len(buffer)) < len(buffer)
short_temp = Path(buffer.value)
assert str(long_temp).casefold() != str(short_temp).casefold()
print(
    "Platform:",
    os.environ["PROCESSOR_ARCHITECTURE"],
    "archive:",
    binary.name,
    "x86_64",
    flush=True,
)
with tempfile.TemporaryDirectory(dir=long_temp) as root:
    name = Path(root).name
    long = long_temp / name
    short = short_temp / name
    neutral = Path(os.environ["RUNNER_TEMP"]) / name
    neutral.mkdir()
    for index, (label, grant, target) in enumerate(
        [
            ("long/long", long, long),
            ("long/short", long, short),
            ("short/long", short, long),
            ("short/short", short, short),
            ("neutral", neutral, neutral),
        ]
    ):
        dest = target / f"write-{index}.txt"
        config = {
            "filesystem": {
                "kind": "restricted",
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    },
                    {"path": {"type": "path", "path": str(grant)}, "access": "write"},
                ],
            },
            "network": "enabled",
        }
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--runner-config-env",
                "POLICY",
                "--",
                sys.executable,
                "-I",
                "-c",
                "import pathlib,sys; pathlib.Path(sys.argv[1]).write_text('allowed')",
                dest,
            ],
            cwd=long,
            env=os.environ | {"POLICY": json.dumps(config)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(
            label,
            "policy",
            grant,
            "target",
            target,
            "wrote",
            dest.exists(),
            "status",
            result.returncode,
            "stderr",
            result.stderr,
            flush=True,
        )
    for label, parent in [
        ("long temp", long),
        ("short temp", short),
        ("neutral temp", neutral),
    ]:
        result = subprocess.run(
            [
                binary,
                "sandbox",
                "--no-config",
                "--",
                sys.executable,
                "-I",
                "-c",
                "import os,pathlib; p=pathlib.Path(os.environ['TEMP']) / 'write.txt'; p.write_text('allowed'); print(p.read_text())",
            ],
            cwd=long,
            env=os.environ | {"TEMP": str(parent), "TMP": str(parent)},
            capture_output=True,
            text=True,
            timeout=60,
        )
        print(
            label,
            "status",
            result.returncode,
            "stdout",
            result.stdout,
            "stderr",
            result.stderr,
            flush=True,
        )
