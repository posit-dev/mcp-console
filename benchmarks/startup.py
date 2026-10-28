#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = ["py-yaml12>=0.2.0"]
# ///
"""Compare fresh public MCP sessions and plain interpreter first-result latency.

Build binaries separately, then run:
    uv run benchmarks/startup.py --binary target/release/mcp-console \
        --baseline /path/to/previous/mcp-console --output /tmp/startup.json

Each sample uses a fresh workspace, with one temporary Console home per binary
retained through the run. HOME and dependency caches are preserved; Console home
does not select package cache locations. Untimed sessions warm each variant before round-robin timed
launches. Initialization, tools/list, evaluation, and the completed MCP response
are timed; shutdown is not. Plain interpreter timing ends at its first output.
Harness setup and version probes finish before timing. Console's own dependency
preparation remains part of its startup measurement. Use --python to select the
existing interpreter; SQL variants using it are skipped when DuckDB is absent.
Use --sandbox for the native default managed cache policy. Direct execution uses
the native host-cache default. Dependency caches are shared across both binaries.
"""

import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from boundaries.client_server.python.test_without_r import (
    environment as no_r_environment,
)
from support.assertions import wait_for_evaluation_output
from support.client import McpClient, TextReader
from support.execution import DIRECT, SANDBOXED
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment, expose_uv


@dataclass(frozen=True)
class Variant:
    field: str
    languages: tuple[str, ...] | None = None
    no_r: bool = False
    managed: bool = False
    uv: bool = False
    disable_r: bool = False


VARIANTS = {
    "r-existing-uv": Variant("r", uv=True),
    "r-existing-no-uv": Variant("r"),
    "python-existing": Variant("python", languages=("python",), no_r=True),
    "python-managed": Variant(
        "python", languages=("python",), no_r=True, managed=True, uv=True
    ),
    "python-with-sql-existing": Variant(
        "python", languages=("python", "sql"), no_r=True
    ),
    "python-with-sql-managed": Variant(
        "python", languages=("python", "sql"), no_r=True, managed=True, uv=True
    ),
    "python-sql-existing": Variant("sql", languages=("python", "sql"), no_r=True),
    "python-sql-managed": Variant(
        "sql", languages=("python", "sql"), no_r=True, managed=True, uv=True
    ),
    "managed-default": Variant("r", managed=True, uv=True),
    "r-disabled-existing": Variant("python", languages=("python",), disable_r=True),
}

CACHE_ENVIRONMENT = (
    "MCP_CONSOLE_CACHE_DIR",
    "XDG_CACHE_HOME",
    "UV_CACHE_DIR",
    "UV_PYTHON_INSTALL_DIR",
    "UV_TOOL_DIR",
    "IR_CACHE_DIR",
    "IR_LIBRARY_ROOT",
    "MPLCONFIGDIR",
    "MCP_CONSOLE_DUCKDB_EXTENSION_DIRECTORY",
    "R_USER_CACHE_DIR",
)


def probe(command: list[str], **kwargs: object) -> str:
    result = subprocess.run(
        command, capture_output=True, text=True, check=True, **kwargs
    )
    return (result.stdout + result.stderr).strip()


def plain(command: list[str], env: dict[str, str], workspace: Path) -> dict:
    started = time.perf_counter()
    with subprocess.Popen(
        command,
        env=env,
        cwd=workspace,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    ) as process:
        assert process.stdout is not None
        output = TextReader(process.stdout).readline(timeout=60)
        elapsed = time.perf_counter() - started
        rest, error = process.communicate(timeout=60)
        assert process.returncode == 0 and output.strip() in {"2", "[1] 2", "done"}, (
            output,
            rest,
            error,
        )
    return {
        "command": command,
        "first_seconds": elapsed,
        "output": output,
        "stderr": error,
    }


def sample(
    binary: Path,
    name: str,
    variant: Variant,
    options: argparse.Namespace,
    inherited: dict[str, str],
    library: Path | None,
    console_home: Path,
) -> dict:
    with tempfile.TemporaryDirectory(prefix="console-startup-") as directory:
        root = Path(directory)
        workspace, tools = root / "workspace", root / "tools"
        workspace.mkdir()
        tools.mkdir()
        env = inherited.copy()
        for key in ("RETICULATE_PYTHON", "RETICULATE_UV"):
            env.pop(key, None)
        if variant.no_r:
            env = no_r_environment(tools, inherited=env)
        elif not variant.managed and not variant.disable_r:
            assert library is not None
            env = bare_runtime_environment(env, library)
        if variant.uv:
            expose_uv(tools)
            env["PATH"] = os.pathsep.join((str(tools), env["PATH"]))
        env["MCP_CONSOLE_HOME"] = str(console_home)
        (workspace / ".agents/console").mkdir(parents=True)
        args = ["--no-config"]
        if variant.languages is not None:
            args += ["-c", "languages=" + json.dumps(variant.languages)]
        if name != "managed-default":
            args += ["-c", "r.resolution=disabled", "-c", "r.packages=[]"]
        if not variant.managed:
            args += ["-c", "python=" + json.dumps(str(options.python))]
        if variant.disable_r:
            args += ["-c", "r.enabled=false"]
        arguments = (SANDBOXED if options.sandbox else DIRECT).serve(*args)
        source = "SELECT 1 + 1 AS answer" if variant.field == "sql" else "1 + 1"
        expected = "[1] 2\n" if variant.field == "r" else "2\n"
        if options.silent and variant.field != "sql":
            source, expected = "x = 1 + 1", "[done]"

        def evaluate(client: McpClient, source: str, expected: str | None) -> str:
            output = wait_for_evaluation_output(
                client,
                expected,
                "benchmark evaluation",
                completion_timeout_seconds=options.timeout,
                timeout_ms=int(options.timeout * 1000),
                **{variant.field: source},
            )
            assert "[waiting for stdin]" not in output and "[running;" not in output, (
                output
            )
            if variant.field == "sql":
                assert (
                    "answer" in output
                    and re.search(r"\b2\b", output)
                    and "Error" not in output
                ), output
            return output

        started = time.perf_counter()
        with McpClient(
            binary,
            arguments,
            env,
            current_directory=workspace,
            record_in_project=False,
            use_home_configuration=True,
            use_r_startup_files=True,
            response_timeout=options.timeout,
        ) as client:
            client.initialize_and_list_tools()
            handshake = time.perf_counter() - started
            output = evaluate(
                client, source, None if variant.field == "sql" else expected
            )
            first = time.perf_counter() - started
            warm = []
            for _ in range(options.warm_calls):
                start = time.perf_counter()
                evaluate(client, source, None if variant.field == "sql" else expected)
                warm.append(time.perf_counter() - start)
            silent = None
            if variant.field != "sql":
                start = time.perf_counter()
                silent_output = evaluate(client, "x = 1 + 1", "[done]")
                silent = {
                    "seconds": time.perf_counter() - start,
                    "output": silent_output,
                }
            metadata_field = "r" if variant.field == "r" else "python"
            r_cache_names = ", ".join(json.dumps(key) for key in CACHE_ENVIRONMENT)
            metadata_source = (
                "dput(list(version = R.version.string, home = R.home(), libraries = .libPaths(), "
                f"cache = Sys.getenv(c({r_cache_names}), unset = NA_character_)))"
                if metadata_field == "r"
                else "import json, os, sys; print(json.dumps(dict(executable=sys.executable, version=sys.version, prefix=sys.prefix, base_prefix=sys.base_prefix, "
                f"cache={{key: os.environ.get(key) for key in {CACHE_ENVIRONMENT!r}}})))"
            )
            runtime = wait_for_evaluation_output(
                client,
                None,
                "runtime metadata",
                completion_timeout_seconds=options.timeout,
                **{metadata_field: metadata_source},
            )
            assert (
                "[waiting for stdin]" not in runtime and "[running;" not in runtime
            ), runtime
            _, error = client.finish_with_standard_error()
        return {
            "command": [str(binary), *arguments],
            "source": source,
            "output": output,
            "first_language": variant.field,
            "languages": list(variant.languages or ("r", "python", "sql")),
            "first_seconds": first,
            "handshake_seconds": handshake,
            "warm_seconds": warm,
            "warm_median_seconds": statistics.median(warm),
            "silent_warm": silent,
            "stderr": error,
            "runtime_metadata": runtime,
            "console_home": str(console_home),
            "environment": {
                key: env.get(key)
                for key in (
                    "PATH",
                    "R_HOME",
                    "R_LIBS",
                    "R_LIBS_USER",
                    "R_LIBS_SITE",
                    "RETICULATE_PYTHON",
                    "HOME",
                    *CACHE_ENVIRONMENT,
                )
            },
        }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "target/release/mcp-console"
    )
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--candidate-revision")
    parser.add_argument("--baseline-revision")
    parser.add_argument("--python", type=Path, default=Path(sys.executable))
    parser.add_argument("--r-library", type=Path)
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument("--warm-calls", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--sandbox", action="store_true")
    parser.add_argument(
        "--silent",
        action="store_true",
        help="Use a silent first assignment for R/Python variants",
    )
    parser.add_argument("--variant", action="append", choices=VARIANTS)
    parser.add_argument("--output", type=Path, required=True)
    options = parser.parse_args()
    assert (
        options.runs > 0
        and options.warm_calls > 0
        and options.warmup >= 0
        and options.timeout > 0
    )
    options.python = options.python.absolute()  # Preserve virtualenv symlinks.
    inherited = os.environ.copy()
    for name in ("R_ENVIRON", "R_ENVIRON_USER", "R_PROFILE", "R_PROFILE_USER"):
        inherited[name] = os.devnull
    has_r = bool(inherited.get("R_HOME") or shutil.which("R"))
    rscript, library = None, options.r_library
    if has_r:
        inherited, rscript = r_test_environment()
        if library is None:
            library = Path(
                probe(
                    [str(rscript), "--vanilla", "-e", "cat(.libPaths()[[1L]])"],
                    env=inherited,
                )
            )
    binaries = {"candidate": options.binary.resolve()}
    if options.baseline:
        binaries["baseline"] = options.baseline.resolve()
    has_duckdb = (
        probe(
            [
                str(options.python),
                "-c",
                'import importlib.util; print(importlib.util.find_spec("duckdb") is not None)',
            ]
        )
        == "True"
    )
    result = {
        "schema_version": 1,
        "method": {
            "runs": options.runs,
            "untimed_warmups": options.warmup,
            "warm_calls": options.warm_calls,
            "sandbox": options.sandbox,
            "silent": options.silent,
            "sample_order": "Round robin by variant and binary; reverse binary order on alternate rounds.",
            "console_endpoint": "Process launch through completed public send, including initialize and tools/list.",
            "plain_endpoint": "Process launch through first stdout line; shutdown excluded.",
            "cache_mode": "managed (native sandbox default)"
            if options.sandbox
            else "host (--no-sandbox default)",
            "cache_state": "Default dependency cache locations preserved and shared across binaries; untimed warmup per variant and binary. Console homes are retained for this run only and do not select dependency caches.",
            "r_disabled_variant": "Candidate only; other no-R variants use identical filtered environments for both binaries.",
        },
        "host": {
            "platform": platform.platform(),
            "machine": platform.machine(),
            "cpus": os.cpu_count(),
        },
        "checkout": {
            "revision": probe(["git", "rev-parse", "HEAD"], cwd=ROOT),
            "status": probe(["git", "status", "--short"], cwd=ROOT),
        },
        "binaries": {
            label: {
                "path": str(path),
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "version": probe([str(path), "--version"]),
                "revision": options.baseline_revision
                if label == "baseline"
                else options.candidate_revision,
            }
            for label, path in binaries.items()
        },
        "runtimes": {
            "python": str(options.python),
            "python_version": probe([str(options.python), "--version"]),
            "rscript": str(rscript) if rscript else None,
            "r_version": probe([str(rscript), "--version"]) if rscript else None,
            "r_library": str(library) if library else None,
            "existing_python_has_duckdb": has_duckdb,
            "uv": shutil.which("uv"),
        },
        "plain": {"python-visible": [], "python-silent": []},
        "variants": {},
    }

    def save() -> None:
        options.output.write_text(json.dumps(result, indent=2) + "\n")

    active = {}
    for name in options.variant or VARIANTS:
        variant = VARIANTS[name]
        reason = (
            "R is unavailable"
            if variant.field == "r" and not has_r
            else "uv is unavailable"
            if variant.uv and not shutil.which("uv")
            else None
        )
        if variant.field == "sql" and not variant.managed and not has_duckdb:
            reason = "selected existing Python has no DuckDB"
        if reason:
            result["variants"][name] = {"skipped": reason}
        else:
            active[name] = variant
            result["variants"][name] = {
                label: []
                for label in binaries
                if not (variant.disable_r and label == "baseline")
            }
    if rscript:
        result["plain"].update({"r-visible": [], "r-silent": []})
    with tempfile.TemporaryDirectory(prefix="console-startup-homes-") as directory:
        console_homes = {label: Path(directory) / label for label in binaries}
        for home in console_homes.values():
            home.mkdir()
        save()
        for round_number in range(-options.warmup, options.runs):
            with tempfile.TemporaryDirectory(prefix="plain-startup-") as directory:
                commands = {
                    "python-visible": [str(options.python), "-u", "-c", "print(1 + 1)"],
                    "python-silent": [
                        str(options.python),
                        "-u",
                        "-c",
                        "x = 1 + 1; print('done')",
                    ],
                }
                if rscript:
                    commands.update(
                        {
                            "r-visible": [str(rscript), "--vanilla", "-e", "1 + 1"],
                            "r-silent": [
                                str(rscript),
                                "--vanilla",
                                "-e",
                                'x = 1 + 1; cat("done\\n")',
                            ],
                        }
                    )
                for name, command in commands.items():
                    env = (
                        bare_runtime_environment(inherited, library)
                        if name.startswith("r-")
                        else inherited
                    )
                    row = plain(command, env, Path(directory))
                    if round_number >= 0:
                        result["plain"][name].append(row)
            for name, variant in active.items():
                for label in list(binaries)[:: 1 if round_number % 2 == 0 else -1]:
                    if variant.disable_r and label == "baseline":
                        continue
                    row = sample(
                        binaries[label],
                        name,
                        variant,
                        options,
                        inherited,
                        library,
                        console_homes[label],
                    )
                    if round_number >= 0:
                        result["variants"][name][label].append(row)
                    print(
                        f"{round_number}: {name} {label} {row['first_seconds']:.4f}s",
                        file=sys.stderr,
                        flush=True,
                    )
                    save()


if __name__ == "__main__":
    main()
