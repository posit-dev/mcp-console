#!/usr/bin/env python3
"""Measure public MCP startup and warm assignment calls with installed runtimes.

Run with Python 3.11+ after building Console and installing released dependencies:

    MCP_CONSOLE_TEST_JUPYTER_PYTHON=/path/to/python \
    MCP_CONSOLE_TEST_ARK=/path/to/ark \
    MCP_CONSOLE_TEST_RETICULATE_LIBRARY=/path/to/R/library \
    python3 benchmarks/jupyter.py --binary target/release/mcp-console > results.json

Setup and version probes run outside timed regions. Each sample launches a new
Console server, negotiates MCP, completes x = 1, then completes thirty identical
warm cells. Variants use direct execution with normal Console recording.
Installed-runtime caches remain warm across launches; this is not a cold-cache
or package-installation benchmark. RSS is a simultaneous process-tree snapshot,
not peak memory or unique physical memory: shared pages can be counted repeatedly.
"""

import argparse
import hashlib
import json
import os
import platform
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from support.client import McpClient
from support.execution import DIRECT
from support.r import r_test_environment
from support.resolvers import bare_runtime_environment


@dataclass(frozen=True)
class Variant:
    name: str
    field: str
    kernel: str | None = None
    transport: str | None = None


VARIANTS = (
    Variant("builtin-python", "python"),
    Variant("ipykernel-ipc", "code", "ipykernel", "ipc"),
    Variant("builtin-r", "r"),
    Variant("ark-tcp", "code", "ark", "tcp"),
)


def installed_path(name: str, *, directory: bool = False) -> Path:
    path = Path(os.environ[name])
    assert path.is_absolute(), (name, path)
    assert path.is_dir() if directory else path.is_file(), (name, path)
    return path


@contextmanager
def launch_settings(
    variant: Variant,
    python: Path,
    ark: Path,
    library: Path,
    inherited: dict[str, str],
) -> Iterator[tuple[tuple[str, ...], dict[str, str], Path]]:
    with tempfile.TemporaryDirectory(prefix="console-jupyter-benchmark-") as directory:
        root = Path(directory)
        workspace = root / "workspace"
        workspace.mkdir()
        environment = bare_runtime_environment(inherited, library)
        environment["RETICULATE_PYTHON"] = str(python)
        arguments = ["--no-config"]
        if variant.kernel is None:
            arguments.extend(
                [
                    "-c",
                    f"languages=[{variant.field}]",
                    "-c",
                    "python=" + json.dumps(str(python)),
                    "-c",
                    "r.resolution=disabled",
                    "-c",
                    "r.packages=[]",
                ]
            )
        else:
            name = "benchmark-" + variant.kernel
            spec = root / "jupyter" / "kernels" / name
            spec.mkdir(parents=True)
            if variant.kernel == "ipykernel":
                argv = [
                    str(python),
                    "-Xfrozen_modules=off",
                    "-m",
                    "ipykernel_launcher",
                    "-f",
                    "{connection_file}",
                ]
                language = "python"
            else:
                argv = [
                    str(ark),
                    "--connection_file",
                    "{connection_file}",
                    "--session-mode",
                    "notebook",
                    "--",
                    "--vanilla",
                ]
                language = "R"
                environment["RUST_LOG"] = "error"
            (spec / "kernel.json").write_text(
                json.dumps({"argv": argv, "display_name": name, "language": language}),
                encoding="utf-8",
            )
            environment["JUPYTER_PATH"] = str(root / "jupyter")
            environment.update(
                JUPYTER_CONFIG_DIR=str(root / "jupyter-config"),
                JUPYTER_RUNTIME_DIR=str(root / "jupyter-runtime"),
                IPYTHONDIR=str(root / "ipython"),
            )
            arguments.extend(
                [
                    "--jupyter-kernel",
                    name,
                    "--jupyter-python",
                    str(python),
                    "--jupyter-transport",
                    variant.transport,
                ]
            )
        yield DIRECT.serve(*arguments), environment, workspace


def process_rss(root_pid: int) -> dict:
    ps = shutil.which("ps")
    if ps is None:
        return {"available": False, "reason": "ps is unavailable"}
    observed = subprocess.run(
        [ps, "-axo", "pid=,ppid=,rss=,comm="],
        check=False,
        capture_output=True,
        text=True,
    )
    if observed.returncode != 0:
        return {"available": False, "reason": observed.stderr.strip()}
    processes = {}
    for line in observed.stdout.splitlines():
        pid, parent, rss, executable = line.split(None, 3)
        processes[int(pid)] = {
            "pid": int(pid),
            "parent_pid": int(parent),
            "rss_kib": int(rss),
            "executable": executable,
        }
    assert root_pid in processes, "Console exited before its memory snapshot"
    selected = {root_pid}
    while children := {
        pid
        for pid, process in processes.items()
        if process["parent_pid"] in selected and pid not in selected
    }:
        selected.update(children)
    rows = [processes[pid] for pid in sorted(selected)]
    return {
        "available": True,
        "total_rss_kib": sum(row["rss_kib"] for row in rows),
        "process_count": len(rows),
        "processes": rows,
    }


def sample(
    binary: Path,
    variant: Variant,
    python: Path,
    ark: Path,
    library: Path,
    inherited: dict[str, str],
    warm_cells: int,
) -> dict:
    with launch_settings(variant, python, ark, library, inherited) as settings:
        arguments, environment, workspace = settings
        started = time.perf_counter()
        with McpClient(
            binary, arguments, environment, current_directory=workspace
        ) as client:
            client.initialize_and_list_tools()
            client.expect("[done]", **{variant.field: "x = 1"})
            first_result_seconds = time.perf_counter() - started
            warm = []
            for _ in range(warm_cells):
                started = time.perf_counter()
                client.expect("[done]", **{variant.field: "x = 1"})
                warm.append(time.perf_counter() - started)
            rss = process_rss(client.process.pid)
            client.finish()
        return {
            "first_result_seconds": first_result_seconds,
            "warm_cell_seconds": warm,
            "warm_median_seconds": statistics.median(warm),
            "after_warm_rss": rss,
        }


def version(command: list[str], environment: dict[str, str]) -> str:
    result = subprocess.run(
        command, env=environment, check=True, capture_output=True, text=True, timeout=30
    )
    return result.stdout.strip()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binary", type=Path, default=ROOT / "target/release/mcp-console"
    )
    parser.add_argument("--launches", type=int, default=5)
    parser.add_argument("--warm-cells", type=int, default=30)
    parser.add_argument(
        "--variant", choices=[v.name for v in VARIANTS], action="append"
    )
    options = parser.parse_args()
    assert options.launches > 0 and options.warm_cells > 0
    variants = tuple(
        v for v in VARIANTS if not options.variant or v.name in options.variant
    )
    binary = options.binary.resolve()
    assert binary.is_file(), binary
    python = installed_path("MCP_CONSOLE_TEST_JUPYTER_PYTHON")
    ark = installed_path("MCP_CONSOLE_TEST_ARK")
    library = installed_path("MCP_CONSOLE_TEST_RETICULATE_LIBRARY", directory=True)
    inherited, rscript = r_test_environment()
    versions_environment = bare_runtime_environment(inherited, library)
    dependencies = json.loads(
        version(
            [
                str(python),
                "-c",
                "import importlib.metadata as m, json, sys; print(json.dumps({'python': sys.version, **{n: m.version(n) for n in ('jupyter_client', 'ipykernel', 'pyzmq')}}))",
            ],
            versions_environment,
        )
    )
    dependencies["ark"] = version([str(ark), "--version"], versions_environment)
    dependencies["r_and_reticulate"] = version(
        [
            str(rscript),
            "--vanilla",
            "-e",
            'cat(R.version.string, "\\nreticulate ", as.character(packageVersion("reticulate")), sep = "")',
        ],
        versions_environment,
    )
    results = {
        "method": {
            "launches_per_variant": options.launches,
            "warm_cells_per_launch": options.warm_cells,
            "source": "x = 1",
            "startup_interval": "Before MCP process launch through the first completed assignment, including MCP initialization and tool discovery.",
            "warm_interval": "One complete public send call after the first assignment.",
            "dependency_preparation": "None: existing Python executable and published R library selected; ir/uv absent from child PATH; R resolution disabled.",
            "execution": "Direct, with normal Console recording in a new temporary workspace per launch.",
            "cache_state": "Installed-runtime caches remain warm; each launch uses fresh Jupyter and IPython configuration directories.",
            "memory": "ps RSS after warm cells for Console and all descendants; includes shared-page double counting, excludes benchmark client, is not peak RSS.",
            "sample_order": "Round robin in listed variant order, one new launch per variant per round.",
        },
        "host": {"platform": platform.platform(), "machine": platform.machine()},
        "binary": {
            "path": str(binary),
            "bytes": binary.stat().st_size,
            "sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
        },
        "dependencies": dependencies,
        "variants": {
            variant.name: {"status": "ok", "samples": []} for variant in variants
        },
    }
    for _ in range(options.launches):
        for variant in variants:
            entry = results["variants"][variant.name]
            if entry["status"] == "error":
                continue
            try:
                entry["samples"].append(
                    sample(
                        binary,
                        variant,
                        python,
                        ark,
                        library,
                        inherited,
                        options.warm_cells,
                    )
                )
            except Exception as error:
                entry["status"] = "error"
                entry["error"] = f"{type(error).__name__}: {error}"
    for entry in results["variants"].values():
        if entry["samples"]:
            entry["first_result_median_seconds"] = statistics.median(
                item["first_result_seconds"] for item in entry["samples"]
            )
            entry["warm_cell_median_seconds"] = statistics.median(
                duration
                for item in entry["samples"]
                for duration in item["warm_cell_seconds"]
            )
    print(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
