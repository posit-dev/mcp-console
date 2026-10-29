from __future__ import annotations

import argparse
import hashlib
import itertools
import json
import math
import os
import posixpath
import re
import select
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from collections import deque
from email.parser import BytesParser
from http.client import IncompleteRead
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STABLE_TAG = re.compile(r"v[0-9]+\.[0-9]+\.[0-9]+")
PACKAGE_VERSION = re.compile(r'^version\s*=\s*"([^"]+)"\s*$')
TARGET_ARCHITECTURES = {
    "aarch64-apple-darwin": "arm64",
    "x86_64-apple-darwin": "x86_64",
    "aarch64-unknown-linux-gnu": "aarch64",
    "x86_64-unknown-linux-gnu": "x86_64",
}
LINUX_RELEASE_GLIBC = (2, 35)
LINUX_RELEASE_CPP = {"GLIBCXX": (3, 4, 30), "CXXABI": (1, 3, 13)}
# Numeric GLIBCXX_3.4.N / CXXABI_1.3.N policy ceilings, not host library versions.
# https://github.com/pypa/auditwheel/blob/7cec8ec5b1436336bc03e560b785cc63d9c4190f/src/auditwheel/policy/manylinux-policy.json
MANYLINUX_CPP = {
    5: (8, 1),
    12: (13, 3),
    17: (19, 7),
    24: (22, 10),
    26: (22, 10),
    27: (24, 11),
    28: (24, 11),
    31: (28, 12),
    34: (29, 13),
    35: (30, 13),
    36: (30, 13),
    37: (30, 13),
    38: (30, 13),
    39: (33, 15),
    40: (33, 15),
    41: (33, 15),
    42: (34, 15),
}
# Named versions start at these pinned policy/architecture boundaries.
MANYLINUX_NAMED = {
    "GLIBC": {
        "ABI_DT_RELR": (36, {"x86_64", "aarch64"}),
        "ABI_DT_X86_64_PLT": (42, {"x86_64"}),
        "ABI_GNU2_TLS": (42, {"x86_64"}),
    },
    "CXXABI": {
        "TM_1": (17, {"x86_64", "aarch64"}),
        "FLOAT128": (24, {"x86_64"}),
    },
}
# GCC versions are sparse sets, with these additions at each policy boundary.
# Use the same pinned auditwheel policy source as the C++ ceilings above.
MANYLINUX_GCC_COMMON = {
    "3.0",
    "3.3",
    "3.3.1",
    "3.4",
    "3.4.2",
    "3.4.4",
    "4.0.0",
    "4.2.0",
}
MANYLINUX_GCC = {
    "x86_64": {
        12: {"4.3.0"},
        17: {"4.7.0", "4.8.0"},
        27: {"7.0.0"},
        35: {"12.0.0"},
        39: {"13.0.0", "14.0.0"},
    },
    "aarch64": {
        17: {"4.3.0", "4.5.0", "4.7.0"},
        26: {"7.0.0"},
        34: {"11.0"},
        39: {"13.0.0", "14.0", "14.0.0"},
    },
}
# Runtime prerequisites, not a distro archive or build-package allowlist.
LINUX_SYSTEM_LIBRARIES = {
    "libc.so.6",
    "libm.so.6",
    "libdl.so.2",
    "libpthread.so.0",
    "librt.so.1",
    "libgcc_s.so.1",
    "libstdc++.so.6",
    "libcap.so.2",
    "ld-linux-x86-64.so.2",
    "ld-linux-aarch64.so.1",
}
LINUX_MACHINES = {
    "x86_64": ("Advanced Micro Devices X86-64", "/lib64/ld-linux-x86-64.so.2"),
    "aarch64": ("AArch64", "/lib/ld-linux-aarch64.so.1"),
}


class ReleaseError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseError(message)


def command_output(
    command: list[str], env: dict[str, str] | None = None, *, strip: bool = True
) -> str:
    result = subprocess.run(
        command,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ReleaseError(f"{' '.join(command)} failed{suffix}")
    return result.stdout.strip() if strip else result.stdout


def run_command(
    command: list[str], env: dict[str, str] | None = None, *, cwd: Path | None = None
) -> None:
    result = subprocess.run(command, env=env, cwd=cwd, check=False)
    if result.returncode != 0:
        raise ReleaseError(
            f"{' '.join(command)} failed with status {result.returncode}"
        )


def package_version() -> str:
    in_package = False
    for line in Path("Cargo.toml").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            if in_package:
                break
            in_package = stripped == "[package]"
        elif in_package and (match := PACKAGE_VERSION.fullmatch(stripped)):
            return match.group(1)
    raise ReleaseError("Cargo.toml package version is missing")


def linux_processes(root_pid: int) -> list[dict[str, Any]]:
    """Capture Linux process ancestry and wait state before smoke cleanup."""
    records = {}
    for directory in Path("/proc").iterdir():
        if not directory.name.isdecimal():
            continue
        try:
            status = {
                line.partition(":")[0]: line.partition(":")[2].strip()
                for line in (directory / "status")
                .read_text(errors="replace")
                .splitlines()
            }
        except OSError:
            # Process exit can remove a proc entry while the snapshot is read.
            continue
        records[int(directory.name)] = {
            "pid": int(directory.name),
            "ppid": int(status["PPid"]),
            "name": status["Name"],
            "state": status["State"],
        }
    family = {root_pid}
    while (
        children := {pid for pid, row in records.items() if row["ppid"] in family}
        - family
    ):
        family.update(children)
    rows = [records[pid] for pid in sorted(family & records.keys())]
    for row in rows:
        directory = Path("/proc") / str(row["pid"])
        try:
            row["wait"] = (directory / "wchan").read_text().strip()
            row["cwd"] = os.readlink(directory / "cwd")
            row["executable"] = os.readlink(directory / "exe")
            # Retain preparation inputs without exposing inherited credentials
            # or submitted source code from the rest of the process environment.
            row["environment"] = {
                name: value
                for entry in (directory / "environ")
                .read_text(errors="replace")
                .split("\0")
                if (name := entry.partition("=")[0])
                in {
                    "IR_CACHE_DIR",
                    "IR_LIBRARY_ROOT",
                    "IR_PREFER_BINARIES",
                    "PKG_PLATFORMS",
                    "R_HOME",
                    "R_ENVIRON",
                    "R_ENVIRON_USER",
                    "R_PROFILE",
                    "R_PROFILE_USER",
                    "R_LIBS",
                    "R_LIBS_SITE",
                    "R_LIBS_USER",
                    "R_MAKEVARS_USER",
                    "R_USER_CACHE_DIR",
                    "RENV_PATHS_ROOT",
                    "RENV_PATHS_CACHE",
                    "RENV_PATHS_SOURCE",
                    "RENV_PATHS_BINARY",
                }
                for value in [entry.partition("=")[2]]
            }
        except OSError:
            # A disappearing descendant still retains its captured identity.
            pass
    return rows


def terminate(process: subprocess.Popen[bytes]) -> str:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=5)
    assert process.stderr is not None
    return process.stderr.read().decode(errors="replace").strip()


def receive(
    process: subprocess.Popen[bytes], buffer: bytearray, timeout_seconds: float
) -> dict[str, Any]:
    assert process.stdout is not None
    deadline = time.monotonic() + timeout_seconds
    while b"\n" not in buffer:
        remaining = deadline - time.monotonic()
        require(
            remaining > 0, f"MCP response timed out after {timeout_seconds:g} seconds"
        )
        readable, _, _ = select.select([process.stdout], [], [], remaining)
        require(
            bool(readable), f"MCP response timed out after {timeout_seconds:g} seconds"
        )
        chunk = os.read(process.stdout.fileno(), 65536)
        require(bool(chunk), "MCP server closed stdout before completing a response")
        buffer.extend(chunk)

    line, _, remainder = buffer.partition(b"\n")
    buffer[:] = remainder
    message = json.loads(line)
    require(isinstance(message, dict), "MCP response must be a JSON object")
    return message


def smoke_mcp(
    executable: Path,
    version: str,
    env: dict[str, str],
    workspace: Path,
    startup_timeout: float,
    response_timeout: float,
    r_available: bool,
) -> None:
    process = subprocess.Popen(
        [str(executable), "serve"],
        env=env,
        cwd=workspace,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    buffer = bytearray()

    def send(message: dict[str, Any]) -> None:
        assert process.stdin is not None
        process.stdin.write((json.dumps(message) + "\n").encode())
        process.stdin.flush()

    try:
        send(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {
                        "name": "wheel-smoke-test",
                        "version": "1.0.0",
                    },
                },
            }
        )
        initialization = receive(process, buffer, startup_timeout)
        require(initialization.get("id") == 1, "unexpected initialize response ID")
        require(
            initialization.get("result", {}).get("serverInfo")
            == {"name": "mcp-console", "version": version},
            "unexpected initialize serverInfo",
        )

        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        # Observe eager worker readiness before the language smoke evaluations.
        send(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "send",
                    "arguments": {"timeout_ms": int(startup_timeout * 1_000)},
                },
            }
        )
        startup = receive(process, buffer, startup_timeout)
        require(startup.get("id") == 2, "unexpected startup response ID")
        require(
            startup.get("result")
            == {
                "content": [{"type": "text", "text": "\n[idle]"}],
                "isError": False,
            },
            f"unexpected runtime startup response: {json.dumps(startup, ensure_ascii=False)}",
        )

        evaluations = [("python", "42\n")]
        if r_available:
            evaluations.append(("r", "[1] 42\n"))
        for identifier, (language, output) in enumerate(evaluations, start=3):
            send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "tools/call",
                    "params": {
                        "name": "send",
                        "arguments": {language: "6 * 7"},
                    },
                }
            )
            evaluation = receive(process, buffer, response_timeout)
            require(
                evaluation.get("id") == identifier, "unexpected evaluation response ID"
            )
            require(
                evaluation.get("result")
                == {
                    "content": [{"type": "text", "text": output}],
                    "isError": False,
                },
                f"unexpected {language} evaluation response: {json.dumps(evaluation, ensure_ascii=False)}",
            )
    except Exception as error:
        if sys.platform == "linux":
            print(
                "MCP processes at failure: " + json.dumps(linux_processes(process.pid)),
                file=sys.stderr,
            )
        standard_error = terminate(process)
        if standard_error:
            raise ReleaseError(f"{error}: {standard_error}") from error
        raise

    process.stdin.close()
    try:
        returncode = process.wait(timeout=response_timeout)
    except subprocess.TimeoutExpired as error:
        standard_error = terminate(process)
        detail = f": {standard_error}" if standard_error else ""
        raise ReleaseError(f"MCP server did not shut down{detail}") from error

    assert process.stderr is not None
    standard_error = process.stderr.read().decode(errors="replace").strip()
    require(
        returncode == 0, f"MCP server exited with status {returncode}: {standard_error}"
    )
    require(not standard_error, f"MCP server wrote to stderr: {standard_error}")


def expanded_tags(tag: str) -> set[str]:
    fields = tag.split("-")
    require(len(fields) == 3, f"invalid wheel tag: {tag}")
    return {
        "-".join(parts)
        for parts in itertools.product(*(field.split(".") for field in fields))
    }


def wheel_identity(wheel: Path) -> tuple[str, set[str]]:
    fields = wheel.name.removesuffix(".whl").split("-")
    require(
        len(fields) == 5 and fields[0] == "mcp_console",
        f"unexpected wheel filename: {wheel.name}",
    )
    version = fields[1]
    tags = expanded_tags("-".join(fields[2:]))
    prefix = f"mcp_console-{version}.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        metadata = BytesParser().parsebytes(archive.read(f"{prefix}/METADATA"))
        require(
            metadata["Name"] == "mcp-console" and metadata["Version"] == version,
            "wheel filename and package metadata disagree",
        )
        metadata = BytesParser().parsebytes(archive.read(f"{prefix}/WHEEL"))
        declared = set().union(
            *(expanded_tags(tag) for tag in metadata.get_all("Tag", []))
        )
        require(tags == declared, "wheel filename and WHEEL tags disagree")
        require(
            metadata["Root-Is-Purelib"] == "false",
            "wheel must not declare pure Python content",
        )
    return version, tags


def inspect_linux_abi(
    wheel: Path, version: str, tags: set[str], *, target: str | None, release: bool
) -> dict[str, Any]:
    architectures = set()
    floors = []
    for tag in tags:
        platform = tag.rsplit("-", 1)[1]
        match = re.fullmatch(r"manylinux_2_([0-9]+)_(x86_64|aarch64)", platform)
        legacy = re.fullmatch(r"manylinux(1|2010|2014)_(x86_64|aarch64)", platform)
        native = re.fullmatch(r"linux_(x86_64|aarch64)", platform)
        require(bool(match or legacy or native), f"unsupported Linux wheel tag: {tag}")
        if match:
            floors.append((2, int(match[1])))
            architectures.add(match[2])
        elif legacy:
            floors.append((2, {"1": 5, "2010": 12, "2014": 17}[legacy[1]]))
            architectures.add(legacy[2])
        else:
            assert native is not None
            architectures.add(native[1])
    require(len(architectures) == 1, "wheel tags mix Linux architectures")
    architecture = architectures.pop()
    if target:
        require(
            target.endswith("-linux-gnu")
            and TARGET_ARCHITECTURES[target] == architecture,
            f"wheel does not match {target}: {wheel.name}",
        )
    if release:
        require(
            len(floors) == len(tags) and max(floors) <= LINUX_RELEASE_GLIBC,
            "wheel tag exceeds the glibc 2.35 release floor",
        )
    ceilings: dict[str, list[tuple[int, ...]]] = {
        "GLIBC": floors,
        "GLIBCXX": [],
        "CXXABI": [],
    }
    gcc_allowed: set[str] | None = None
    for _, minor in floors:
        require(
            minor in MANYLINUX_CPP and (architecture == "x86_64" or minor >= 17),
            f"unsupported manylinux C++ policy: manylinux_2_{minor}_{architecture}",
        )
        glibcxx, cxxabi = MANYLINUX_CPP[minor]
        # Auditwheel's 2.26 policy has different C++ ceilings on ARM64.
        if architecture == "aarch64" and minor == 26:
            glibcxx, cxxabi = 24, 11
        ceilings["GLIBCXX"].append((3, 4, glibcxx))
        ceilings["CXXABI"].append((1, 3, cxxabi))
        gcc_policy = MANYLINUX_GCC_COMMON | {
            number
            for since, numbers in MANYLINUX_GCC[architecture].items()
            if since <= minor
            for number in numbers
        }
        gcc_allowed = gcc_policy if gcc_allowed is None else gcc_allowed & gcc_policy
    if release:
        for family, ceiling in LINUX_RELEASE_CPP.items():
            ceilings[family].append(ceiling)
    limits = {family: min(values, default=None) for family, values in ceilings.items()}
    named_allowed = {
        family: {
            number
            for number, (since, supported) in versions.items()
            if architecture in supported and all(minor >= since for _, minor in floors)
        }
        for family, versions in MANYLINUX_NAMED.items()
    }
    machine, loader = LINUX_MACHINES[architecture]
    evidence = {}
    # Model a Unix installation prefix; Python's version does not change its depth.
    site_packages = "lib/pythonX.Y/site-packages"
    schemes = {
        "scripts": "bin",
        "data": ".",
        "purelib": site_packages,
        "platlib": site_packages,
    }
    data_prefix = f"mcp_console-{version}.data/"
    installed_elves: dict[str, str] = {}
    rpaths: dict[str, tuple[str, ...]] = {}
    needed_order: dict[str, list[str]] = {}
    with zipfile.ZipFile(wheel) as archive, tempfile.TemporaryDirectory() as directory:
        for member in archive.namelist():
            with archive.open(member) as stream:
                if stream.read(4) != b"\x7fELF":
                    continue
            if member.startswith(data_prefix):
                scheme, relative = member.removeprefix(data_prefix).split("/", 1)
                require(scheme in schemes, f"{member}: unsupported ELF install scheme")
                installed = posixpath.normpath(
                    posixpath.join(schemes[scheme], relative)
                )
            else:
                installed = posixpath.join(site_packages, member)
            installed_elves[installed] = member
            elf = Path(directory) / "artifact"
            elf.write_bytes(archive.read(member))
            output = command_output(
                ["readelf", "-h", "-l", "-d", "-V", "-W", str(elf)],
                env=os.environ | {"LC_ALL": "C"},
            )

            def field(name: str) -> str | None:
                match = re.search(rf"^\s*{name}:\s*(.+)$", output, re.MULTILINE)
                return match[1].strip() if match else None

            require(
                field("Class") == "ELF64"
                and field("Data") == "2's complement, little endian",
                f"{member}: expected little-endian ELF64",
            )
            require(
                field("Machine") == machine,
                f"{member}: ELF machine does not match {architecture}",
            )
            interp = re.search(r"\[Requesting program interpreter: ([^]]+)\]", output)
            require(
                interp is None or interp[1] == loader,
                f"{member}: unexpected ELF interpreter",
            )
            paths = re.findall(r"\((RPATH|RUNPATH)\).*\[([^]]*)\]", output)
            search_paths: dict[str, list[str]] = {}
            for kind, value in paths:
                search_paths[kind] = []
                for path in value.split(":"):
                    path = path.replace("${ORIGIN}", "$ORIGIN")
                    require(
                        path == "$ORIGIN" or path.startswith("$ORIGIN/"),
                        f"{member}: {kind} must be relative to $ORIGIN: {value}",
                    )
                    resolved = posixpath.normpath(
                        posixpath.join(
                            posixpath.dirname(installed),
                            path.removeprefix("$ORIGIN").lstrip("/"),
                        )
                    )
                    require(
                        resolved != ".." and not resolved.startswith("../"),
                        f"{member}: {kind} escapes the installed wheel: {value}",
                    )
                    search_paths[kind].append(resolved)
            # RUNPATH suppresses this object's RPATH, and is never inherited.
            rpaths[member] = (
                tuple(search_paths.get("RPATH", []))
                if "RUNPATH" not in search_paths
                else ()
            )
            versions = re.findall(
                r"Name: ((?:GLIBC|GLIBCXX|CXXABI|GCC)_[^\s]+)",
                output.partition("Version needs section")[2],
            )
            requirements: dict[str, list[str]] = {
                "GLIBC": [],
                "GLIBCXX": [],
                "CXXABI": [],
                "GCC": [],
            }
            for symbol in versions:
                family, number = symbol.split("_", 1)
                numeric = re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", number) is not None
                require(
                    numeric or number in named_allowed.get(family, ()),
                    f"{member}: unsupported symbol requirement {symbol}",
                )
                if family == "GCC":
                    require(
                        gcc_allowed is None or number in gcc_allowed,
                        f"{member}: {symbol} is not permitted by the declared wheel policy",
                    )
                elif numeric:
                    required = tuple(map(int, number.split(".")))
                    ceiling = limits[family]
                    require(
                        ceiling is None or required <= ceiling,
                        f"{member}: {symbol} exceeds the declared wheel/runtime floor",
                    )
                requirements[family].append(number)
            needed_order[member] = re.findall(r"\(NEEDED\).*\[([^]]+)\]", output)
            evidence[member] = {
                "class": field("Class"),
                "data": field("Data"),
                "machine": field("Machine"),
                "interpreter": interp[1] if interp else None,
                "needed": sorted(needed_order[member]),
                "rpath_runpath": dict(paths),
                "search": search_paths.get("RUNPATH", search_paths.get("RPATH", [])),
                "glibc": sorted(set(requirements["GLIBC"])),
                "glibcxx": sorted(set(requirements["GLIBCXX"])),
                "cxxabi": sorted(set(requirements["CXXABI"])),
                "gcc": sorted(set(requirements["GCC"])),
            }
        commands = (
            f"mcp_console-{version}.data/scripts/mcp-console",
            *(
                f"mcp_console-{version}.data/data/libexec/{name}"
                for name in ("mcp-console-sandbox", "bwrap")
            ),
        )
        for name in commands:
            require(name in evidence, f"{name}: required wheel executable is not ELF")
        checked: set[str] = set()

        def check_dependencies(entry: str) -> None:
            loaded_names: set[str] = set()
            loaded_members = {entry}
            pending: deque[tuple[str, tuple[str, ...]]] = deque([(entry, ())])
            # glibc maps dependencies breadth-first, reusing already loaded objects.
            # Each executable starts with its own loader state and search paths.
            while pending:
                member, inherited = pending.popleft()
                checked.add(member)
                elf = evidence[member]
                # Each ancestor's paths use that ancestor's installed $ORIGIN.
                ancestors = tuple(dict.fromkeys((*rpaths[member], *inherited)))
                search = (
                    elf["search"] if "RUNPATH" in elf["rpath_runpath"] else ancestors
                )
                for needed in needed_order[member]:
                    # Slash-containing filenames bypass glibc's library search.
                    require(
                        "/" not in needed,
                        f"{member}: unsupported DT_NEEDED filename {needed}; "
                        "dependencies must be library names without /",
                    )
                    if needed in LINUX_SYSTEM_LIBRARIES or needed in loaded_names:
                        continue
                    dependency = next(
                        (
                            installed_elves[candidate]
                            for path in search
                            if (
                                candidate := posixpath.normpath(
                                    posixpath.join(path, needed)
                                )
                            )
                            in installed_elves
                        ),
                        None,
                    )
                    require(
                        dependency is not None,
                        f"{member}: undeclared dependency {needed}",
                    )
                    loaded_names.add(needed)
                    if dependency not in loaded_members:
                        loaded_members.add(dependency)
                        pending.append((dependency, ancestors))

        needed_names = {name for elf in evidence.values() for name in elf["needed"]}
        # Audit independent entry points separately; children inherit only their chain.
        for member, elf in evidence.items():
            if (
                member in commands
                or elf["interpreter"] is not None
                or posixpath.basename(member) not in needed_names
            ):
                check_dependencies(member)
        # Also audit unreferenced bundles, including closed dependency cycles.
        for member in evidence:
            if member not in checked:
                check_dependencies(member)
    return evidence


def inspect_wheel_commands(
    wheel: Path,
    *,
    linux: bool,
    version: str,
    tags: set[str],
    target: str | None = None,
    release: bool = False,
    sandbox_pin: Path = Path("sandbox-runner.json"),
) -> dict[str, Any]:
    data = f"mcp_console-{version}.data/data"
    evidence = {}
    with zipfile.ZipFile(wheel) as archive:
        members = archive.namelist()
        for name in ("mcp-console-sandbox", *(["bwrap"] if linux else [])):
            runner = f"{data}/libexec/{name}"
            require(
                [member for member in members if Path(member).name == name] == [runner],
                f"wheel must contain exactly one private sandbox runner {name} under libexec",
            )
            require(
                archive.getinfo(runner).external_attr >> 16 & 0o111 != 0,
                f"private sandbox runner {name} is not executable",
            )
        for name in (
            "LICENSE",
            "NOTICE",
            *(
                ["bubblewrap-COPYING", "bubblewrap-NOTICE", "bubblewrap-SOURCE.json"]
                if linux
                else []
            ),
        ):
            require(
                f"{data}/share/licenses/mcp-console/{name}" in members,
                f"private sandbox runner is missing {name}",
            )
            require(
                bool(archive.read(f"{data}/share/licenses/mcp-console/{name}").strip()),
                f"private sandbox runner has empty {name}",
            )

        if linux:
            evidence = inspect_linux_abi(
                wheel, version, tags, target=target, release=release
            )
            prefix = f"{data}/share/licenses/mcp-console"
            provenance = json.loads(archive.read(f"{prefix}/bubblewrap-SOURCE.json"))
            pin = json.loads(sandbox_pin.read_text())
            source_archive = (
                f"https://github.com/{pin['repository']}/archive/{pin['commit']}.tar.gz"
            )
            require(
                source_archive.encode() in archive.read(f"{prefix}/bubblewrap-NOTICE"),
                "bubblewrap-NOTICE does not identify the pinned source archive",
            )
            helper = archive.read(f"{data}/libexec/bwrap")
            for key, expected in {
                "source_repository": pin["repository"],
                "source_revision": pin["commit"],
                "source_directory": "codex-rs/vendor/bubblewrap",
                "build_script": "codex-rs/bwrap/build.rs",
                "wrapper_directory": "codex-rs/bwrap",
                "sha256": hashlib.sha256(helper).hexdigest(),
            }.items():
                require(
                    provenance.get(key) == expected,
                    f"Bubblewrap provenance has inconsistent {key}",
                )
            needed = evidence[f"{data}/libexec/bwrap"]["needed"]
            linkage = (
                "dynamic"
                if any(name.startswith("libcap.so.") for name in needed)
                else "static"
            )
            require(
                provenance.get("elf_needed") == needed,
                "Bubblewrap provenance has inconsistent ELF dependencies",
            )
            require(
                provenance.get("libcap_linkage") == linkage,
                "Bubblewrap provenance has inconsistent libcap linkage",
            )
            libcap_notice = f"{prefix}/libcap-NOTICE"
            require(
                (libcap_notice in members) == (linkage == "static"),
                "Bubblewrap provenance requires libcap-NOTICE only for redistributed static libcap",
            )
            if linkage == "static":
                require(
                    bool(archive.read(libcap_notice).strip()),
                    "Bubblewrap provenance has an empty libcap-NOTICE",
                )
    return {
        "wheel": wheel.name,
        "sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "tags": sorted(tags),
        "elf": evidence,
    }


def inspect_wheel(args: argparse.Namespace) -> None:
    wheel = Path(args.wheel).resolve()
    version, tags = wheel_identity(wheel)
    evidence = inspect_wheel_commands(
        wheel,
        linux=True,
        version=version,
        tags=tags,
        target=args.target,
        release=args.release,
        sandbox_pin=Path(args.sandbox_pin),
    )
    report = json.dumps(evidence, indent=2) + "\n"
    if args.report:
        Path(args.report).write_text(report, encoding="utf-8")
    else:
        print(report, end="")


def smoke_wheel(args: argparse.Namespace) -> None:
    wheel = Path(args.wheel).resolve()
    require(wheel.is_file(), f"wheel does not exist: {wheel}")
    cargo_bin = Path(args.cargo_bin).resolve() if args.cargo_bin else None
    require(
        args.installed_only == (cargo_bin is None),
        "provide a Cargo binary or select --installed-only",
    )
    version, tags = wheel_identity(wheel)
    if cargo_bin is not None:
        require(cargo_bin.is_file(), f"Cargo binary does not exist: {cargo_bin}")
        require(
            os.access(cargo_bin, os.X_OK),
            f"Cargo binary is not executable: {cargo_bin}",
        )
        require(version == package_version(), "wheel and Cargo versions differ")
    platform = wheel.name.rsplit("-", 1)[-1]
    require(
        platform.startswith(("macosx_", "manylinux", "linux_")),
        f"unsupported wheel platform: {wheel.name}",
    )
    linux = not platform.startswith("macosx_")
    require(not wheel.name.endswith("-none-any.whl"), "wheel must be platform-specific")
    inspect_wheel_commands(
        wheel,
        linux=linux,
        version=version,
        tags=tags,
        target=args.target,
        release=args.release,
        sandbox_pin=Path(args.sandbox_pin),
    )

    if args.target is not None:
        architecture = TARGET_ARCHITECTURES[args.target]
        require(
            wheel.name.endswith(f"_{architecture}.whl")
            and linux == ("linux" in args.target),
            f"wheel does not match {args.target}: {wheel.name}",
        )

    actual_version = command_output(
        ["uv", "tool", "run", "--from", str(wheel), "mcp-console", "--version"]
    )
    # Wheel metadata keeps +dev stable; a development executable can also
    # identify its source commit without changing the installable version.
    version_pattern = re.escape(f"mcp-console {version}")
    if version.endswith("+dev"):
        version_pattern += r"(?:\.g[0-9a-f]{12})?"
    require(
        re.fullmatch(version_pattern, actual_version) is not None,
        "installed and wheel metadata versions differ",
    )
    expected_version = (
        command_output([str(cargo_bin), "--version"])
        if cargo_bin is not None
        else actual_version
    )
    reference = "Cargo" if cargo_bin is not None else "wheel metadata"
    require(
        actual_version == expected_version, f"installed and {reference} versions differ"
    )

    wheel_help = command_output(
        ["uv", "tool", "run", "--from", str(wheel), "mcp-console", "--help"],
        strip=False,
    )
    expected_help = (
        command_output([str(cargo_bin), "--help"], strip=False)
        if cargo_bin is not None
        else wheel_help
    )
    if cargo_bin is not None:
        require(wheel_help == expected_help, "installed and Cargo help output differ")

    run_command(["uv", "tool", "install", str(wheel)])

    tool_bin = Path(os.environ["UV_TOOL_BIN_DIR"])
    installed = tool_bin / "mcp-console"
    require(installed.is_file(), f"installed executable does not exist: {installed}")
    require(
        os.access(installed, os.X_OK),
        f"installed executable is not executable: {installed}",
    )
    require(
        command_output([str(installed), "--version"]) == expected_version,
        f"`uv` tool and {reference} versions differ",
    )
    require(
        command_output([str(installed), "--help"], strip=False) == expected_help,
        "`uv` tool and wheel help output differ",
    )
    public_runner = tool_bin / "mcp-console-sandbox"
    require(
        not public_runner.exists(),
        f"private sandbox runner was installed as a public command: {public_runner}",
    )
    with tempfile.TemporaryDirectory(prefix="mcp-console-empty-path-") as directory:
        sandbox_env = os.environ.copy()
        sandbox_env["PATH"] = directory
        sandbox_env["MCP_CONSOLE_HOME"] = str(Path(directory) / "console")
        for executable in (
            [cargo_bin, installed] if cargo_bin is not None else [installed]
        ):
            run_command(
                [str(executable), "sandbox", "--", "/usr/bin/true"],
                env=sandbox_env,
                cwd=Path(directory),
            )

    internal_ir = installed.resolve().with_name("ir")
    require(not internal_ir.exists(), f"wheel contains sibling `ir`: {internal_ir}")

    r_home = None if args.without_r else os.environ.get("R_HOME")
    if r_home is not None:
        require(
            any((Path(r_home) / "bin" / name).is_file() for name in ("R", "Rscript")),
            "R_HOME must select an existing R installation",
        )
    elif not args.without_r and shutil.which("R"):
        r_home = command_output(["R", "RHOME"])
    uv = shutil.which("uv")
    require(uv is not None, "host `uv` is not on `PATH`")
    with tempfile.TemporaryDirectory(prefix="mcp-console-uv-path-") as directory:
        uv_bin = Path(directory)
        (uv_bin / "uv").symlink_to(Path(uv).resolve())
        if args.without_r:
            (uv_bin / "python3").symlink_to(Path(sys.executable).resolve())
            if bwrap := shutil.which("bwrap"):
                (uv_bin / "bwrap").symlink_to(Path(bwrap).resolve())
        unavailable_uvx = uv_bin / "uvx"
        unavailable_uvx.write_text("#!/bin/sh\nexit 97\n", encoding="utf-8")
        unavailable_uvx.chmod(0o755)
        path = (
            str(uv_bin)
            if args.without_r
            else os.pathsep.join(
                [str(uv_bin)]
                + [
                    entry
                    for entry in os.environ.get("PATH", "").split(os.pathsep)
                    if not (Path(entry) / "ir").is_file()
                ]
            )
        )

        env = os.environ.copy()
        env.pop("RETICULATE_UV", None)
        if args.without_r:
            for name in ("R_HOME", "R_LIBS", "R_LIBS_USER", "RETICULATE_PYTHON"):
                env.pop(name, None)
        if r_home is not None:
            env["R_HOME"] = r_home
        env["PATH"] = path
        env["MCP_CONSOLE_HOME"] = str(uv_bin / "console")
        smoke_mcp(
            installed,
            actual_version.removeprefix("mcp-console "),
            env,
            uv_bin,
            args.startup_timeout_seconds,
            args.response_timeout_seconds,
            r_available=r_home is not None,
        )


def validate_publish(_: argparse.Namespace) -> None:
    event_name = os.environ["GITHUB_EVENT_NAME"]
    ref_type = os.environ["GITHUB_REF_TYPE"]
    tag = os.environ["GITHUB_REF_NAME"]
    repository = os.environ["GITHUB_REPOSITORY"]

    require(
        event_name == "push",
        f"release publication requires a push event, got {event_name}",
    )
    require(ref_type == "tag", f"release publication requires a tag, got {ref_type}")
    require(STABLE_TAG.fullmatch(tag) is not None, f"invalid release tag: {tag}")

    version = package_version()
    require(
        tag == f"v{version}", f"tag {tag} does not match Cargo.toml version v{version}"
    )

    release_commit = command_output(["git", "rev-parse", f"refs/tags/{tag}^{{commit}}"])
    run_command(["git", "fetch", "--no-tags", "origin", "main"])
    main_commit = command_output(["git", "rev-parse", "FETCH_HEAD"])
    ancestry = subprocess.run(
        ["git", "merge-base", "--is-ancestor", release_commit, main_commit],
        check=False,
    )
    require(ancestry.returncode == 0, f"release commit {release_commit} is not on main")

    response = command_output(
        [
            "gh",
            "api",
            "-H",
            "Accept: application/vnd.github+json",
            "--method",
            "GET",
            f"/repos/{repository}/actions/workflows/ci.yaml/runs",
            "-f",
            f"head_sha={release_commit}",
            "-f",
            "event=push",
            "-f",
            "branch=main",
            "-f",
            "status=completed",
            "-f",
            "per_page=100",
        ]
    )
    runs = json.loads(response)["workflow_runs"]
    matching_runs = [
        run
        for run in runs
        if run.get("head_sha") == release_commit
        and run.get("event") == "push"
        and run.get("head_branch") == "main"
    ]
    require(
        bool(matching_runs) and matching_runs[0].get("conclusion") == "success",
        f"release commit {release_commit} lacks successful CI from a push to main",
    )


def verify_wheel_set(args: argparse.Namespace) -> None:
    directory = Path(args.directory)
    wheels = sorted(directory.glob("*.whl"))
    arm64 = list(directory.glob("mcp_console-*-macosx_*_arm64.whl"))
    x86_64 = list(directory.glob("mcp_console-*-macosx_*_x86_64.whl"))
    linux_arm64 = list(directory.glob("mcp_console-*-manylinux_*_aarch64.whl"))
    linux_x86_64 = list(directory.glob("mcp_console-*-manylinux_*_x86_64.whl"))
    windows_x86_64 = list(directory.glob("mcp_console-*-win_amd64.whl"))
    universal = list(directory.glob("*-none-any.whl"))
    sdists = list(directory.glob("*.tar.gz"))

    require(len(wheels) == 5, f"expected exactly five wheels, found {len(wheels)}")
    require(len(arm64) == 1, "expected exactly one Apple Silicon wheel")
    require(len(x86_64) == 1, "expected exactly one Intel macOS wheel")
    require(len(linux_arm64) == 1, "expected exactly one ARM64 Linux wheel")
    require(len(linux_x86_64) == 1, "expected exactly one x86-64 Linux wheel")
    require(len(windows_x86_64) == 1, "expected exactly one x86-64 Windows wheel")
    require(not universal, "a platform-independent wheel must not be published")
    require(not sdists, "a source distribution must not be published")

    print("Publishing:")
    for wheel in wheels:
        print(f"  {wheel}")


def wait_pypi(args: argparse.Namespace) -> None:
    wheels = sorted(Path(args.directory).glob("*.whl"))
    require(len(wheels) == 5, f"expected exactly five wheels, found {len(wheels)}")
    for value in (args.timeout_seconds, args.poll_interval_seconds):
        require(
            math.isfinite(value) and value > 0,
            "wait budgets must be finite and positive",
        )
    expected = {
        wheel.name: hashlib.sha256(wheel.read_bytes()).hexdigest() for wheel in wheels
    }
    version = package_version()
    base = args.pypi_url.rstrip("/")
    endpoints = (
        (
            f"{base}/pypi/mcp-console/{version}/json",
            "urls",
            "digests",
            "application/json",
        ),
        (
            f"{base}/simple/mcp-console/",
            "files",
            "hashes",
            "application/vnd.pypi.simple.v1+json",
        ),
    )
    deadline = time.monotonic() + args.timeout_seconds
    timeout_message = (
        f"PyPI wheel set was not visible within {args.timeout_seconds:g} seconds"
    )
    while True:
        for url, collection, hash_field, accept in endpoints:
            remaining = deadline - time.monotonic()
            require(remaining > 0, timeout_message)
            try:
                with urlopen(
                    Request(url, headers={"Accept": accept}), timeout=min(30, remaining)
                ) as response:
                    payload = json.load(response)
            except HTTPError as error:
                if error.code not in (404, 429) and not 500 <= error.code < 600:
                    raise
                error.close()
                break
            except (URLError, ConnectionError, TimeoutError, IncompleteRead):
                break
            visible = {item["filename"]: item for item in payload[collection]}
            if collection == "urls":
                require(
                    visible.keys() <= expected.keys(),
                    "PyPI version contains unexpected files",
                )
            for filename in expected.keys() & visible.keys():
                item = visible[filename]
                require(
                    item.get("yanked", False) is False,
                    f"PyPI wheel is yanked: {filename}",
                )
                require(
                    item[hash_field]["sha256"] == expected[filename],
                    f"PyPI hash differs: {filename}",
                )
            if not expected.keys() <= visible.keys():
                break
        else:
            report = (
                json.dumps({"version": version, "wheels": expected}, indent=2) + "\n"
            )
            if args.report:
                Path(args.report).write_text(report, encoding="utf-8")
            else:
                print(report, end="")
            return
        remaining = deadline - time.monotonic()
        require(remaining > 0, timeout_message)
        time.sleep(min(args.poll_interval_seconds, remaining))


def parser() -> argparse.ArgumentParser:
    argument_parser = argparse.ArgumentParser()
    commands = argument_parser.add_subparsers(required=True)

    smoke = commands.add_parser("smoke-wheel")
    smoke.add_argument("wheel")
    smoke.add_argument("cargo_bin", nargs="?")
    smoke.add_argument("--installed-only", action="store_true")
    smoke.add_argument("--sandbox-pin", default="sandbox-runner.json")
    smoke.add_argument("--release", action="store_true")
    smoke.add_argument("--target", choices=sorted(TARGET_ARCHITECTURES))
    smoke.add_argument("--without-r", action="store_true")
    smoke.add_argument("--startup-timeout-seconds", type=float, default=1200.0)
    smoke.add_argument("--response-timeout-seconds", type=float, default=30.0)
    smoke.set_defaults(function=smoke_wheel)

    inspect = commands.add_parser("inspect-wheel")
    inspect.add_argument("wheel")
    inspect.add_argument("--target", choices=sorted(TARGET_ARCHITECTURES))
    inspect.add_argument("--release", action="store_true")
    inspect.add_argument("--sandbox-pin", default="sandbox-runner.json")
    inspect.add_argument("--report")
    inspect.set_defaults(function=inspect_wheel)

    validate = commands.add_parser("validate-publish")
    validate.set_defaults(function=validate_publish)

    verify = commands.add_parser("verify-wheel-set")
    verify.add_argument("directory")
    verify.set_defaults(function=verify_wheel_set)

    wait = commands.add_parser("wait-pypi")
    wait.add_argument("directory")
    wait.add_argument("--pypi-url", default="https://pypi.org")
    wait.add_argument("--timeout-seconds", type=float, default=120)
    wait.add_argument("--poll-interval-seconds", type=float, default=2)
    wait.add_argument("--report")
    wait.set_defaults(function=wait_pypi)

    return argument_parser


def main() -> int:
    args = parser().parse_args()
    try:
        for name in ("startup_timeout_seconds", "response_timeout_seconds"):
            require(getattr(args, name, 1) > 0, "timeouts must be greater than zero")
        args.function(args)
    except (FileNotFoundError, KeyError, json.JSONDecodeError, ReleaseError) as error:
        print(f"release: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
