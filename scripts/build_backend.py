"""Prepare the private companion before Maturin builds a wheel."""

import argparse
import base64
import configparser
import copy
import csv
import hashlib
import io
import json
import os
import runpy
import subprocess
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
import zipfile

import maturin

get_requires_for_build_editable = maturin.get_requires_for_build_editable
get_requires_for_build_sdist = maturin.get_requires_for_build_sdist
get_requires_for_build_wheel = maturin.get_requires_for_build_wheel


def prepare_metadata_for_build_wheel(
    metadata_directory: str, config_settings: dict[str, Any] | None = None
) -> str:
    name = maturin.prepare_metadata_for_build_wheel(metadata_directory, config_settings)
    if sys.platform == "win32":
        entry_points = Path(metadata_directory) / name / "entry_points.txt"
        existing = entry_points.read_bytes() if entry_points.exists() else b""
        entry_points.write_bytes(_windows_entry_points(existing))
    return name


prepare_metadata_for_build_editable = prepare_metadata_for_build_wheel


def build_sdist(
    sdist_directory: str, config_settings: dict[str, Any] | None = None
) -> str:
    with _checkout_owner(Path(__file__).resolve().parent.parent):
        return maturin.build_sdist(sdist_directory, config_settings)


def build_wheel(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    with _staged_companion():
        name = maturin.build_wheel(wheel_directory, config_settings, metadata_directory)
        if sys.platform == "win32":
            _windows_wheel(Path(wheel_directory) / name)
        return name


def build_editable(
    wheel_directory: str,
    config_settings: dict[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    with _staged_companion():
        name = maturin.build_editable(
            wheel_directory, config_settings, metadata_directory
        )
        if sys.platform == "win32":
            _windows_wheel(Path(wheel_directory) / name)
        return name


def _windows_entry_points(existing: bytes) -> bytes:
    entries = configparser.ConfigParser(interpolation=None)
    entries.optionxform = str
    entries.read_string(existing.decode("utf-8"))
    if not entries.has_section("console_scripts"):
        entries.add_section("console_scripts")
    entries["console_scripts"]["mcp-console"] = "mcp_console._launcher:main"
    output = io.StringIO()
    entries.write(output)
    return output.getvalue().encode("utf-8")


def _windows_wheel(wheel: Path) -> None:
    # uv copies Windows commands rather than linking them. An installer-made
    # Python entry point retains the tool environment; the native executable
    # stays in that environment with its digest-bound companion bundle.
    temporary = wheel.with_suffix(".whl.tmp")
    try:
        with (
            zipfile.ZipFile(wheel) as source,
            zipfile.ZipFile(temporary, "w") as output,
        ):
            (record,) = (
                name for name in source.namelist() if name.endswith(".dist-info/RECORD")
            )
            dist_info = record.rsplit("/", 1)[0]
            data = dist_info.removesuffix(".dist-info") + ".data"
            native = f"{data}/scripts/mcp-console.exe"
            destination = f"{data}/data/libexec/mcp-console.exe"
            entry_points = f"{dist_info}/entry_points.txt"
            if native not in source.namelist() or destination in source.namelist():
                raise ValueError("Windows wheel must contain one native Console script")
            records = []

            def write(info: zipfile.ZipInfo | str, content: bytes) -> None:
                output.writestr(info, content)
                name = info.filename if isinstance(info, zipfile.ZipInfo) else info
                digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest())
                records.append(
                    (name, "sha256=" + digest.rstrip(b"=").decode(), len(content))
                )

            for info in source.infolist():
                if info.filename in {record, entry_points}:
                    continue
                content = source.read(info)
                relocated = copy.copy(info)
                if info.filename == native:
                    relocated.filename = destination
                write(relocated, content)
            existing = (
                source.read(entry_points) if entry_points in source.namelist() else b""
            )
            write(entry_points, _windows_entry_points(existing))
            records.append((record, "", ""))
            manifest = io.StringIO(newline="")
            csv.writer(manifest, lineterminator="\n").writerows(records)
            output.writestr(record, manifest.getvalue().encode("utf-8"))
        temporary.replace(wheel)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _checkout_owner(root: Path) -> Iterator[None]:
    from checkout_workflow import checkout_owner

    with checkout_owner(root, wait=sys.platform == "win32"):
        yield


@contextmanager
def _staged_companion() -> Iterator[None]:
    root = Path(__file__).resolve().parent.parent
    with _checkout_owner(root):
        if sys.platform == "win32":
            # Stage in-process under the shared checkout owner; the pinned
            # companion source has its own ownership lock.
            staging = runpy.run_path(str(root / "scripts/stage-sandbox-runner"))
            args = argparse.Namespace(
                checkout=Path(source)
                if (source := os.environ.get("MCP_CONSOLE_SANDBOX_SOURCE"))
                else None,
                target=None,
            )
            staging["stage_locked"](
                args, json.loads((root / "sandbox-runner.json").read_text())
            )
        else:
            subprocess.run(
                [sys.executable, str(root / "scripts/stage-sandbox-runner")], check=True
            )
        yield
