"""Compile and load native checkpoints on macOS and Linux."""

import os
import subprocess
import sys
from pathlib import Path

LOADER_VARIABLE = "DYLD_INSERT_LIBRARIES" if sys.platform == "darwin" else "LD_PRELOAD"

SHARED_LIBRARY_FLAG = "-dynamiclib" if sys.platform == "darwin" else "-shared"


def build_interposer(directory: Path, name: str) -> Path:
    source = Path(__file__).resolve().parents[1] / "fixtures" / "native" / f"{name}.c"
    return compile_interposer(source, directory / name)


def compile_interposer(source: Path, output: Path) -> Path:
    library = output.with_suffix(".dylib" if sys.platform == "darwin" else ".so")
    architectures = []
    if sys.platform == "darwin" and os.uname().machine == "arm64":
        # The loader environment can also reach arm64e host helpers.
        architectures = ["-arch", "arm64", "-arch", "arm64e"]
    subprocess.run(
        [
            "cc",
            SHARED_LIBRARY_FLAG,
            *architectures,
            "-fPIC",
            "-std=c11",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-o",
            str(library),
            str(source),
        ],
        check=True,
    )
    return library
