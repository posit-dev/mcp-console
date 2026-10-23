"""Validate an installed release wheel in a source-free native floor runtime."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--target", required=True)
    parser.add_argument("--with-r", action="store_true")
    args = parser.parse_args()
    fixture = Path(__file__).resolve().parent
    evidence = Path("/evidence")
    evidence.mkdir(exist_ok=True)

    glibc = subprocess.check_output(["getconf", "GNU_LIBC_VERSION"], text=True).strip()
    print(glibc, Path("/etc/os-release").read_text(), flush=True)
    assert glibc == "glibc 2.35"
    assert platform.machine() == args.target.split("-", 1)[0]
    assert all(
        shutil.which(tool) is None
        for tool in ("gcc", "g++", "cc", "cargo", "rustc", "make")
    )
    assert (shutil.which("R") is not None) == args.with_r
    assert not Path("Cargo.toml").exists()
    assert not Path("target").exists()
    subprocess.run(["uname", "-a"], check=True)
    packages = subprocess.check_output(["dpkg-query", "-W"], text=True)
    print(packages, flush=True)
    assert not any(
        line.split()[0].split(":")[0].endswith("-dev") for line in packages.splitlines()
    )
    subprocess.run(["uv", "--version"], check=True)
    subprocess.run([sys.executable, "--version"], check=True)
    if args.with_r:
        subprocess.run(["R", "--version"], check=True)

    command = [sys.executable, str(fixture / "release.py")]
    contract = [
        "--release",
        "--target",
        args.target,
        "--sandbox-pin",
        str(fixture / "sandbox-runner.json"),
    ]
    subprocess.run(
        [
            *command,
            "inspect-wheel",
            str(args.wheel),
            *contract,
            "--report",
            str(evidence / "abi.json"),
        ],
        check=True,
    )
    subprocess.run(
        [
            *command,
            "smoke-wheel",
            str(args.wheel),
            "--installed-only",
            *contract,
            *([] if args.with_r else ["--without-r"]),
        ],
        check=True,
    )

    installed = (Path(os.environ["UV_TOOL_BIN_DIR"]) / "mcp-console").resolve()
    prefix = installed.parent.parent
    # Inspect each dynamic executable's full tree, including bundled libraries.
    # Loading a child independently loses the executable's inherited DT_RPATH.
    for artifact in sorted(prefix.rglob("*")):
        if not artifact.is_file() or artifact.is_symlink():
            continue
        with artifact.open("rb") as stream:
            if stream.read(4) != b"\x7fELF":
                continue
        headers = subprocess.check_output(
            ["readelf", "-l", "-W", str(artifact)],
            text=True,
            env=os.environ | {"LC_ALL": "C"},
        )
        if "[Requesting program interpreter:" not in headers:
            continue
        result = subprocess.run(
            ["ldd", str(artifact)], capture_output=True, text=True, check=True
        )
        print(artifact.relative_to(prefix), result.stdout, flush=True)
        assert "not found" not in result.stdout
        assert not any(
            path in result.stdout for path in ("/work/", "/build/", "/usr/local/cargo/")
        )
    subprocess.run(
        [sys.executable, str(fixture / "sandbox_installation.py"), str(installed)],
        check=True,
    )
    print(
        json.dumps(
            {"target": args.target, "r_present": args.with_r, "result": "passed"}
        ),
        flush=True,
    )


if __name__ == "__main__":
    main()
