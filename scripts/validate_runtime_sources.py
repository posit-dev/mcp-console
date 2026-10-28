#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import ast
import os
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
from checkout_workflow import r_executable

SOURCE_ROOT = ROOT / "src"
EXPECTED_SOURCES = {
    "src/jupyter.py",
    "src/python/bridge.R",
    "src/python/initialize.R",
    "src/python/environment.py",
    "src/python/inspection.py",
    "src/python/probe.py",
    "src/python/runtime.py",
    "src/python/services.py",
    "src/r_environment/bridge.R",
    "src/r_graphics/bridge.R",
    "src/resolver/programs/duckdb_extensions.R",
    "src/resolver/programs/duckdb_extensions.py",
    "src/resolver/programs/r_library.R",
    "src/resolver/programs/uv_binary.R",
    "src/resolver/programs/windows_read_access.R",
    "src/sql/bridge.R",
    "src/sql/dbapi.py",
    "src/worker/embedded_r/documentation.R",
    "src/worker/embedded_r/parse.R",
}
INCLUDE_PATTERN = re.compile(
    r'include_str!\(\s*"([^"\n]+\.(?:R|py))"\s*\)', re.MULTILINE
)
PLACEHOLDER_PATTERNS = (
    re.compile(r"\{\{[^{}\n]+\}\}"),
    re.compile(r"@@[A-Z][A-Z0-9_]+@@"),
    re.compile(r"__MCP_CONSOLE_[A-Z0-9_]+__"),
)


def repository_path(path: Path) -> str:
    return path.relative_to(ROOT).as_posix()


def production_sources() -> dict[str, Path]:
    return {
        repository_path(path): path
        for path in SOURCE_ROOT.rglob("*")
        if path.suffix in {".R", ".py"}
    }


def included_sources() -> set[str]:
    included = set()
    for rust_source in SOURCE_ROOT.rglob("*.rs"):
        source = rust_source.read_text(encoding="utf-8")
        for relative_path in INCLUDE_PATTERN.findall(source):
            included.add(
                repository_path((rust_source.parent / relative_path).resolve())
            )
    return included


def validate_placeholders(path: str, source: str) -> list[str]:
    errors = []
    for pattern in PLACEHOLDER_PATTERNS:
        if match := pattern.search(source):
            errors.append(
                f"{path}: unresolved generation placeholder {match.group()!r}"
            )
    return errors


def validate_python(path: str, source: str) -> list[str]:
    try:
        ast.parse(source, filename=path, feature_version=(3, 10))
    except SyntaxError as error:
        return [f"{path}: {error.__class__.__name__}: {error}"]
    return []


def validate_r(path: str, source_path: Path, rscript: str) -> list[str]:
    result = subprocess.run(
        [
            rscript,
            "--vanilla",
            "-e",
            "invisible(parse(file = commandArgs(trailingOnly = TRUE)[[1L]], keep.source = TRUE))",
            str(source_path),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        env=os.environ | {"LC_ALL": "C"},
    )
    if result.returncode == 0:
        return []
    detail = result.stderr.strip() or result.stdout.strip() or "R parser failed"
    return [f"{path}: {detail}"]


def main() -> int:
    sources = production_sources()
    discovered = set(sources)
    included = included_sources()
    errors = []
    rscript = r_executable("Rscript")
    if rscript is None:
        print("R source syntax checks skipped: Rscript is unavailable", file=sys.stderr)

    for path in sorted(EXPECTED_SOURCES - discovered):
        errors.append(f"{path}: expected production source is missing")
    for path in sorted(discovered - EXPECTED_SOURCES):
        errors.append(f"{path}: production source is not in the validation manifest")
    for path in sorted(discovered - included):
        errors.append(f"{path}: production source is not included by Rust")
    for path in sorted(included - discovered):
        errors.append(
            f"{path}: Rust includes a source that validation did not discover"
        )

    for path, source_path in sorted(sources.items()):
        source = source_path.read_text(encoding="utf-8")
        errors.extend(validate_placeholders(path, source))
        if source_path.suffix == ".py":
            errors.extend(validate_python(path, source))
        elif rscript is not None:
            errors.extend(validate_r(path, source_path, rscript))

    if errors:
        print("\n".join(errors), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
