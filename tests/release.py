#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///

from __future__ import annotations

import hashlib
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from threading import Thread
import unittest
import zipfile
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "release.py"
STAGE_SCRIPT = ROOT / "scripts" / "stage-sandbox-runner"


def write_executable(path: Path, source: str) -> None:
    path.write_text(code(source), encoding="utf-8")
    path.chmod(0o755)


def write_python_executable(path: Path, source: str) -> None:
    # The shell/Python header preserves the selected interpreter with spaces,
    # without PATH lookup or consuming the fixture's MCP stdin.
    launcher = code(
        f"""
        #!/bin/sh
        '''exec' {shlex.quote(sys.executable)} "$0" "$@"
        ' '''
        """
    )
    write_executable(path, launcher + code(source))


def bubblewrap_notice(
    pin: dict[str, object], toolchain: str = "fixture-sandbox"
) -> str:
    return (
        "Bubblewrap companion\n\n"
        f"Source archive: https://github.com/{pin['repository']}/archive/{pin['commit']}.tar.gz\n"
        "C source: codex-rs/vendor/bubblewrap\n"
        "Build integration: codex-rs/bwrap\n"
        "License text: bubblewrap-COPYING\n"
        f"Rust toolchain: {toolchain} (codex-rs/rust-toolchain.toml)\n\n"
        "/* Copyright fixture author; SPDX-License-Identifier: LGPL-2.0-or-later */\n"
    )


def helper_metadata(pin: dict[str, object], helper: bytes) -> dict[str, object]:
    return {
        "source_repository": pin["repository"],
        "source_revision": pin["commit"],
        "source_directory": "codex-rs/vendor/bubblewrap",
        "build_script": "codex-rs/bwrap/build.rs",
        "wrapper_directory": "codex-rs/bwrap",
        "sha256": hashlib.sha256(helper).hexdigest(),
        "elf_needed": ["libc.so.6", "libcap.so.2"],
        "libcap_linkage": "dynamic",
    }


def elf_fixture(**changes: object) -> bytes:
    fields = {
        "machine": "Advanced Micro Devices X86-64",
        "interpreter": "/lib64/ld-linux-x86-64.so.2",
        "needed": ["libc.so.6"],
        "versions": ["GLIBC_2.34"],
        "rpath": None,
        "runpath": None,
    }
    fields.update(changes)
    return b"\x7fELF" + json.dumps(fields).encode()


def rewrite_wheel(wheel: Path, replacements: dict[str, bytes]) -> None:
    with zipfile.ZipFile(wheel) as archive:
        entries = [(info, archive.read(info)) for info in archive.infolist()]
    with zipfile.ZipFile(wheel, "w") as archive:
        for info, contents in entries:
            archive.writestr(info, replacements.get(info.filename, contents))


def write_readelf_fixture(commands: Path) -> None:
    write_executable(
        commands / "readelf",
        # fmt: python
        """
        #!/usr/bin/env python3
        import json
        import os
        import sys
        from pathlib import Path

        if sys.argv[1:3] == ["-s", "-W"]:
            assert Path(sys.argv[3]).read_bytes() == b"bwrap bytes with debug symbols"
            if not os.environ.get("FAKE_NO_LIBCAP"):
                print("12: 0000000000100 42 FUNC GLOBAL DEFAULT 15 cap_get_proc")
            raise SystemExit(0)
        blob = Path(sys.argv[-1]).read_bytes()
        if blob.startswith(b"\\x7fELF"):
            fields = json.loads(blob[4:])
            if sys.argv[1:-1] == ["-h", "-l", "-d", "-V", "-W"]:
                print("  Class: ELF64")
                print("  Data: 2's complement, little endian")
                print("  Machine:", fields["machine"])
                if fields["interpreter"]:
                    print("[Requesting program interpreter: " + fields["interpreter"] + "]")
            else:
                assert sys.argv[1:3] == ["-d", "-W"]
            for name in fields["needed"]:
                if name != "libcap.so.2" or not os.environ.get("FAKE_STATIC_LIBCAP"):
                    print(" (NEEDED) Shared library: [" + name + "]")
            for kind in ("rpath", "runpath"):
                if fields.get(kind):
                    print(" (" + kind.upper() + ") Library path: [" + fields[kind] + "]")
            print("Version needs section '.gnu.version_r':")
            for name in fields["versions"]:
                print("  Name: " + name + " Flags: none Version: 2")
        else:
            assert sys.argv[1:3] == ["-d", "-W"]
            assert blob in (b"fixture\\n", b"bwrap bytes")
            print(" (NEEDED) Shared library: [libc.so.6]")
            if not os.environ.get("FAKE_STATIC_LIBCAP"):
                print(" (NEEDED) Shared library: [libcap.so.2]")
    """,
    )


class ReleaseFixture(unittest.TestCase):
    def run_script(
        self,
        *arguments: str,
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(SCRIPT), *arguments],
            cwd=cwd,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )


@unittest.skipUnless(os.name == "posix", "Unix executable and packaging fixtures")
class ReleaseScriptTests(ReleaseFixture):
    def validation_environment(self, directory: Path) -> dict[str, str]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        write_executable(
            commands / "git",
            # fmt: python
            """
            #!/usr/bin/env python3
            import os
            import sys

            arguments = sys.argv[1:]
            if arguments == ["rev-parse", "refs/tags/v0.0.2^{commit}"]:
                print(os.environ["FAKE_RELEASE_COMMIT"])
            elif arguments == ["fetch", "--no-tags", "origin", "main"]:
                pass
            elif arguments == ["rev-parse", "FETCH_HEAD"]:
                print(os.environ["FAKE_MAIN_COMMIT"])
            elif arguments[:2] == ["merge-base", "--is-ancestor"]:
                raise SystemExit(int(os.environ.get("FAKE_ANCESTRY_STATUS", "0")))
            else:
                print(f"unexpected git arguments: {arguments}", file=sys.stderr)
                raise SystemExit(2)
            """,
        )
        write_executable(
            commands / "gh",
            # fmt: python
            """
            #!/usr/bin/env python3
            import json
            import os
            import sys
            from pathlib import Path

            Path(os.environ["FAKE_GH_ARGUMENTS"]).write_text(
                json.dumps(sys.argv[1:]), encoding="utf-8"
            )
            print(os.environ["FAKE_GH_RESPONSE"])
            """,
        )

        environment = os.environ.copy()
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "GITHUB_EVENT_NAME": "push",
                "GITHUB_REF_TYPE": "tag",
                "GITHUB_REF_NAME": "v0.0.2",
                "GITHUB_REPOSITORY": "t-kalinowski/mcp-console",
                "FAKE_RELEASE_COMMIT": "release-commit",
                "FAKE_MAIN_COMMIT": "main-commit",
                "FAKE_GH_ARGUMENTS": str(directory / "gh-arguments.json"),
            }
        )
        return environment

    def test_validate_publish_requires_successful_main_push_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "push",
                            "head_branch": "main",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertEqual(result.returncode, 0, result.stderr)
            gh_arguments = json.loads((directory / "gh-arguments.json").read_text())
            self.assertIn(
                "/repos/t-kalinowski/mcp-console/actions/workflows/ci.yaml/runs",
                gh_arguments,
            )
            self.assertIn("head_sha=release-commit", gh_arguments)
            self.assertIn("event=push", gh_arguments)
            self.assertIn("branch=main", gh_arguments)

    def test_validate_publish_rejects_successful_pull_request_ci(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["FAKE_GH_RESPONSE"] = json.dumps(
                {
                    "workflow_runs": [
                        {
                            "head_sha": "release-commit",
                            "event": "pull_request",
                            "head_branch": "feature",
                            "conclusion": "success",
                        }
                    ]
                }
            )

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("lacks successful CI from a push to main", result.stderr)

    def test_validate_publish_rejects_manual_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment = self.validation_environment(directory)
            environment["GITHUB_EVENT_NAME"] = "workflow_dispatch"
            environment["FAKE_GH_RESPONSE"] = '{"workflow_runs": []}'

            result = self.run_script("validate-publish", cwd=directory, env=environment)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("requires a push event", result.stderr)

    def test_linux_floor_checks_libraries_through_executable_entry_points(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            prefix = directory / "installed"
            (prefix / "bin").mkdir(parents=True)
            (prefix / "libexec").mkdir()
            for name, fields in {
                "bin/mcp-console": {
                    "needed": ["liba.so"],
                    "rpath": "$ORIGIN/../libexec",
                },
                "libexec/liba.so": {"interpreter": None, "needed": ["libb.so"]},
                "libexec/libb.so": {"interpreter": None},
                "libexec/bwrap": {},
                "libexec/mcp-console-sandbox": {},
                "libexec/extra-command": {},
            }.items():
                (prefix / name).write_bytes(elf_fixture(**fields))
            launcher = directory / "floor.py"
            write_executable(
                launcher,
                # fmt: python
                """
                import json
                import os
                import platform
                import runpy
                import shutil
                import subprocess
                import sys
                from pathlib import Path
                from unittest.mock import patch

                # Supply the floor host and unrelated smoke observations; the
                # CLI still discovers ELF files and chooses loader entry points.
                original_read_text = Path.read_text
                original_mkdir = Path.mkdir
                def read_text(path: Path, *args, **kwargs) -> str:
                    if path == Path("/etc/os-release"):
                        return "ID=ubuntu\\nVERSION_ID=22.04\\n"
                    return original_read_text(path, *args, **kwargs)
                def mkdir(path: Path, *args, **kwargs) -> None:
                    if path != Path("/evidence"):
                        original_mkdir(path, *args, **kwargs)
                def check_output(command: list[str], **kwargs) -> str:
                    if command == ["getconf", "GNU_LIBC_VERSION"]:
                        return "glibc 2.35\\n"
                    if command[0] == "readelf":
                        assert command[1:-1] == ["-l", "-W"]
                        fields = json.loads(Path(command[-1]).read_bytes()[4:])
                        if fields["interpreter"]:
                            return "[Requesting program interpreter: " + fields["interpreter"] + "]"
                        return ""
                    assert command == ["dpkg-query", "-W"]
                    return "libc6 2.35\\n"
                def run(command: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
                    output = ""
                    if command[0] == "ldd":
                        artifact = Path(command[-1])
                        with Path(os.environ["LOADER_CALLS"]).open("a") as log:
                            log.write(artifact.name + "\\n")
                        # liba needs the executable's inherited RPATH to find libb.
                        if artifact.name == "liba.so":
                            output = "libb.so => not found"
                        else:
                            output = "libc.so.6 => /lib/libc.so.6"
                        if artifact.name == "extra-command":
                            output = os.environ["EXTRA_LOADER_OUTPUT"]
                    else:
                        assert command[0] in (sys.executable, "uname", "uv")
                    return subprocess.CompletedProcess(command, 0, output, "")
                sys.argv = [sys.argv[1], "fixture.whl", "--target", "x86_64-unknown-linux-gnu"]
                with patch.object(platform, "machine", return_value="x86_64"), \\
                     patch.object(shutil, "which", return_value=None), \\
                     patch.object(Path, "read_text", read_text), \\
                     patch.object(Path, "mkdir", mkdir), \\
                     patch.object(subprocess, "check_output", check_output), \\
                     patch.object(subprocess, "run", run):
                    runpy.run_path(sys.argv[0], run_name="__main__")
                """,
            )
            for output, accepted in (
                ("libc.so.6 => /lib/libc.so.6", True),
                ("libmissing.so => not found", False),
                ("libb.so => /build/libb.so", False),
            ):
                with self.subTest(output=output):
                    calls = directory / "loader-calls"
                    calls.unlink(missing_ok=True)
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(launcher),
                            str(ROOT / "tests/linux_wheel_runtime.py"),
                        ],
                        cwd=directory,
                        env=os.environ
                        | {
                            "UV_TOOL_BIN_DIR": str(prefix / "bin"),
                            "LOADER_CALLS": str(calls),
                            "EXTRA_LOADER_OUTPUT": output,
                        },
                        capture_output=True,
                        text=True,
                        check=False,
                    )
                    if accepted:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(
                            set(calls.read_text().splitlines()),
                            {
                                "mcp-console",
                                "mcp-console-sandbox",
                                "bwrap",
                                "extra-command",
                            },
                        )
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout)

    def test_linux_floor_refreshes_expired_r_preparation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            commands = directory / "commands"
            commands.mkdir()
            # Model Docker's persistent RUN cache and IR's 24-hour expiry at
            # the driver boundary; real cold R preparation takes many minutes.
            write_executable(
                commands / "docker",
                # fmt: python
                """
                #!/usr/bin/env python3
                import json
                import os
                import re
                import sys
                from pathlib import Path

                arguments = sys.argv[1:]
                state_path = Path(os.environ["FLOOR_PREPARATION_STATE"])
                now = int(os.environ["FLOOR_NOW"])
                if arguments[0] == "info":
                    print(os.environ["FLOOR_ARCHITECTURE"])
                elif arguments[0] == "pull":
                    pass  # Pulling an unchanged base does not expire RUN caches.
                elif arguments[0] == "build":
                    mode = arguments[arguments.index("--target") + 1]
                    if mode == "with-r":
                        recipe = (Path(arguments[-1]) / "Dockerfile").read_text()
                        declared = re.findall(r"(?m)^ARG (\\w+)", recipe)
                        key = [
                            arguments[index + 1]
                            for index, argument in enumerate(arguments)
                            if argument == "--build-arg"
                            and arguments[index + 1].split("=", 1)[0] in declared
                        ]
                        state = json.loads(state_path.read_text()) if state_path.exists() else {}
                        if state.get("key") != key or "--no-cache" in arguments:
                            state_path.write_text(json.dumps({"key": key, "prepared": now}))
                elif arguments[:2] == ["image", "inspect"]:
                    print("[]")
                elif arguments[0] == "run":
                    if "--with-r" in arguments:
                        state = json.loads(state_path.read_text())
                        if now - state["prepared"] >= 86400:
                            print(
                                "expired R resolution needs a source build; no compiler in runtime",
                                file=sys.stderr,
                            )
                            raise SystemExit(1)
                else:
                    raise SystemExit(f"unexpected Docker command: {arguments}")
                """,
            )
            wheel = directory / "wheel.whl"
            wheel.touch()
            for architecture in ("x86_64", "aarch64"):
                environment = os.environ | {
                    "PATH": f"{commands}{os.pathsep}{os.environ['PATH']}",
                    "FLOOR_ARCHITECTURE": architecture,
                    "FLOOR_PREPARATION_STATE": str(directory / f"{architecture}.json"),
                }
                # The wheel and base images are unchanged on the second run;
                # only the cached latest-package resolution has expired.
                for now in (0, 86401):
                    with self.subTest(architecture=architecture, now=now):
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(ROOT / "scripts/check-linux-wheel"),
                                str(wheel),
                                "--target",
                                f"{architecture}-unknown-linux-gnu",
                                "--evidence",
                                str(directory / architecture),
                            ],
                            env=environment | {"FLOOR_NOW": str(now)},
                            capture_output=True,
                            text=True,
                            check=False,
                        )
                        self.assertEqual(result.returncode, 0, result.stderr)

    def smoke_environment(self, directory: Path) -> tuple[dict[str, str], Path, Path]:
        (directory / "Cargo.toml").write_text(
            '[package]\nversion = "0.0.2"\n', encoding="utf-8"
        )
        commands = directory / "commands"
        commands.mkdir()
        tool_directory = directory / "tool" / "mcp-console" / "bin"
        tool_directory.mkdir(parents=True)
        tool_bin = directory / "bin"
        tool_bin.mkdir()

        # fmt: python
        executable_source = """
            import hashlib
            import json
            import os
            import signal
            import shutil
            import sys
            from pathlib import Path

            if record := os.environ.get("FAKE_MCP_ARGUMENTS"):
                with open(record, "a") as stream:
                    stream.write(json.dumps(sys.argv[1:]) + "\\n")

            if record := os.environ.get("FAKE_MCP_LOCATIONS"):
                if sys.argv[1] in ("serve", "sandbox"):
                    with open(record, "a") as stream:
                        stream.write(
                            json.dumps(
                                {
                                    "command": sys.argv[1],
                                    "interpreter": sys.executable,
                                    "cwd": str(Path.cwd()),
                                    "home": os.environ.get("HOME"),
                                    "console": os.environ.get("MCP_CONSOLE_HOME"),
                                    "r_home": os.environ.get("R_HOME"),
                                    "r": shutil.which("R"),
                                    "rscript": shutil.which("Rscript"),
                                    "python": (
                                        str(Path(python).resolve())
                                        if (python := shutil.which("python3"))
                                        else None
                                    ),
                                }
                            )
                            + "\\n"
                        )

            if sys.argv[1:] == ["--version"]:
                print("mcp-console 0.0.2")
            elif sys.argv[1:] == ["--help"]:
                print("mcp-console help")
            elif sys.argv[1:3] == ["sandbox", "--"]:
                if os.environ.get("FAKE_SANDBOX_FAILURE"):
                    print("private sandbox runner failed", file=sys.stderr)
                    raise SystemExit(1)
                Path(os.environ["FAKE_SANDBOX_PATH_RECORD"]).write_text(
                    os.environ.get("PATH", ""), encoding="utf-8"
                )
            elif sys.argv[1:] in (["serve"], ["serve", "--no-sandbox"]):
                initialize = json.loads(sys.stdin.readline())
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": initialize["id"],
                            "result": {
                                "protocolVersion": "2025-11-25",
                                "capabilities": {"tools": {}},
                                "serverInfo": {
                                    "name": "mcp-console",
                                    "version": "0.0.2",
                                },
                            },
                        }
                    ),
                    flush=True,
                )
                json.loads(sys.stdin.readline())
                startup = json.loads(sys.stdin.readline())
                assert startup["params"]["name"] == "send"
                assert set(startup["params"]["arguments"]) == {"timeout_ms"}
                assert startup["params"]["arguments"]["timeout_ms"] > 0
                if os.environ.get("FAKE_MCP_STARTUP_HANG"):
                    signal.pause()
                if failed := os.environ.get("FAKE_MCP_STARTUP_RESULT"):
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": startup["id"],
                                "result": json.loads(failed),
                            }
                        ),
                        flush=True,
                    )
                    signal.pause()
                print(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": startup["id"],
                            "result": {
                                "content": [
                                    {
                                        "type": "text",
                                        "text": "\\n[idle]",
                                    }
                                ],
                                "isError": False,
                            },
                        }
                    ),
                    flush=True,
                )
                evaluations = [("python", "42\\n")]
                if not os.environ.get("FAKE_NO_R"):
                    evaluations.append(("r", "[1] 42\\n"))
                for language, output in evaluations:
                    evaluation = json.loads(sys.stdin.readline())
                    assert evaluation["params"] == {
                        "name": "send",
                        "arguments": {language: "6 * 7"},
                    }
                    if os.environ.get("FAKE_MCP_EVALUATION_HANG"):
                        signal.pause()
                    print(
                        json.dumps(
                            {
                                "jsonrpc": "2.0",
                                "id": evaluation["id"],
                                "result": {
                                    "content": [{"type": "text", "text": output}],
                                    "isError": False,
                                },
                            }
                        ),
                        flush=True,
                    )
            else:
                raise SystemExit(2)
        """
        cargo_directory = directory / "cargo-target"
        (cargo_directory / "release").mkdir(parents=True)
        cargo_bin = cargo_directory / "release" / "mcp-console"
        write_python_executable(cargo_bin, executable_source)
        installed = tool_directory / "mcp-console"
        write_python_executable(installed, executable_source)
        (tool_bin / "mcp-console").symlink_to(installed)

        write_executable(
            commands / "uv",
            r"""
            #!/bin/sh
            if test "$1 $2 $3 $5" = "tool run --from mcp-console"; then
              case "$6" in
                --version) echo 'mcp-console 0.0.2' ;;
                --help)
                  printf 'mcp-console help\n'
                  if test "${FAKE_UV_HELP_EXTRA_NEWLINE:-}" = 1; then
                    printf '\n'
                  fi
                  ;;
                *) exit 2 ;;
              esac
            elif test "$1 $2" = "tool install"; then
              exit 0
            else
              exit 2
            fi
            """,
        )
        write_executable(
            commands / "R",
            """
            #!/bin/sh
            test "$1" = RHOME
            echo /fake/R
            """,
        )

        write_readelf_fixture(commands)
        shutil.copyfile(ROOT / "sandbox-runner.json", directory / "sandbox-runner.json")
        wheel = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
        self.write_wheel(wheel)
        environment = os.environ.copy()
        environment.pop("R_HOME", None)
        environment.update(
            {
                "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                "UV_TOOL_DIR": str(directory / "tool"),
                "UV_TOOL_BIN_DIR": str(tool_bin),
                "FAKE_SANDBOX_PATH_RECORD": str(directory / "sandbox-path.txt"),
            }
        )
        return environment, wheel, cargo_bin

    def write_wheel(
        self,
        wheel: Path,
        *,
        omit: str | None = None,
        executable: bool = True,
        glibc: str = "2.34",
    ) -> None:
        with zipfile.ZipFile(wheel, "w") as archive:
            linux = "linux" in wheel.name
            elf_fields = {"versions": [f"GLIBC_{glibc}"]}
            if linux and "aarch64" in wheel.name:
                elf_fields.update(
                    machine="AArch64", interpreter="/lib/ld-linux-aarch64.so.1"
                )
            archive.writestr(
                "mcp_console-0.0.2.data/scripts/mcp-console",
                elf_fixture(**elf_fields) if linux else b"fixture\n",
            )
            archive.writestr(
                "mcp_console-0.0.2.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: mcp-console\nVersion: 0.0.2\n",
            )
            tag = wheel.name.removesuffix(".whl").split("-", 2)[2]
            archive.writestr(
                "mcp_console-0.0.2.dist-info/WHEEL",
                f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {tag}\n",
            )
            names = ("mcp-console-sandbox", "LICENSE", "NOTICE")
            if "linux" in wheel.name:
                names += (
                    "bwrap",
                    "bubblewrap-COPYING",
                    "bubblewrap-NOTICE",
                    "bubblewrap-SOURCE.json",
                )
            for name in names:
                if name == omit:
                    continue
                directory = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                info = zipfile.ZipInfo(
                    f"mcp_console-0.0.2.data/data/{directory}/{name}"
                )
                mode = 0o755 if directory == "libexec" and executable else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                contents: str | bytes = "fixture\n"
                if linux and name in ("mcp-console-sandbox", "bwrap"):
                    contents = elf_fixture(
                        **elf_fields,
                        needed=["libc.so.6", "libcap.so.2"]
                        if name == "bwrap"
                        else ["libc.so.6"],
                    )
                if name == "bubblewrap-NOTICE":
                    contents = bubblewrap_notice(
                        json.loads((ROOT / "sandbox-runner.json").read_text())
                    )
                if name == "bubblewrap-SOURCE.json":
                    pin = json.loads((ROOT / "sandbox-runner.json").read_text())
                    contents = json.dumps(
                        helper_metadata(
                            pin,
                            elf_fixture(
                                **elf_fields, needed=["libc.so.6", "libcap.so.2"]
                            ),
                        )
                    )
                archive.writestr(info, contents)

    def test_inspect_wheel_reports_every_shipped_elf_and_matching_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            self.write_wheel(wheel)
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr("mcp_console/extra.so", elf_fixture(interpreter=None))
            report = directory / "abi.json"
            result = self.run_script(
                "inspect-wheel",
                str(wheel),
                "--release",
                "--target",
                "x86_64-unknown-linux-gnu",
                "--report",
                str(report),
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            evidence = json.loads(report.read_text())
            self.assertEqual(evidence["tags"], ["py3-none-manylinux_2_35_x86_64"])
            self.assertEqual(len(evidence["elf"]), 4)
            self.assertEqual(evidence["elf"]["mcp_console/extra.so"]["glibc"], ["2.34"])
            self.assertIn(
                "libcap.so.2",
                evidence["elf"]["mcp_console-0.0.2.data/data/libexec/bwrap"]["needed"],
            )

    def test_inspect_wheel_resolves_bundled_libraries_after_installation(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            for member, runpath, library_directory in (
                (
                    "mcp_console-0.0.2.data/scripts/mcp-console",
                    "$ORIGIN/../libexec",
                    "libexec",
                ),
                (
                    "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox",
                    "${ORIGIN}",
                    "libexec",
                ),
                (
                    "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox",
                    "$ORIGIN/..",
                    ".",
                ),
                ("mcp_console/extra.so", "$ORIGIN/../../../../libexec", "libexec"),
                (
                    "mcp_console-0.0.2.data/platlib/mcp_console/extra.so",
                    "$ORIGIN/../../../../libexec",
                    "libexec",
                ),
                (
                    "mcp_console-0.0.2.data/purelib/mcp_console/extra.so",
                    "$ORIGIN/../../../../libexec",
                    "libexec",
                ),
            ):
                with self.subTest(member=member, runpath=runpath):
                    self.write_wheel(wheel)
                    contents = elf_fixture(needed=["libfixture.so"], runpath=runpath)
                    if member.endswith("extra.so"):
                        with zipfile.ZipFile(wheel, "a") as archive:
                            archive.writestr(member, contents)
                    else:
                        rewrite_wheel(wheel, {member: contents})
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            (
                                Path("mcp_console-0.0.2.data/data")
                                / library_directory
                                / "libfixture.so"
                            ).as_posix(),
                            elf_fixture(interpreter=None),
                        )
                    report = directory / "abi.json"
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        "--report",
                        str(report),
                        cwd=directory,
                        env=environment,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    evidence = json.loads(report.read_text())["elf"]
                    self.assertEqual(evidence[member]["search"], [library_directory])

    def test_inspect_wheel_rejects_relative_dependency_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            executable = "mcp_console-0.0.2.data/scripts/mcp-console"
            for kind in ("rpath", "runpath"):
                for needed in (
                    "libfixture.so",
                    "./libfixture.so",
                    "bin/subdir/libfixture.so",
                ):
                    with self.subTest(kind=kind, needed=needed):
                        self.write_wheel(wheel)
                        rewrite_wheel(
                            wheel,
                            {
                                executable: elf_fixture(
                                    needed=[needed], **{kind: "$ORIGIN/.."}
                                )
                            },
                        )
                        # A matching file under the search path must not make a
                        # cwd-relative DT_NEEDED filename pass the audit.
                        with zipfile.ZipFile(wheel, "a") as archive:
                            archive.writestr(
                                "mcp_console-0.0.2.data/data/"
                                + ("bin/subdir/" if needed.startswith("bin/") else "")
                                + "libfixture.so",
                                elf_fixture(interpreter=None),
                            )
                        result = self.run_script(
                            "inspect-wheel",
                            str(wheel),
                            "--release",
                            cwd=directory,
                            env=environment,
                        )
                        if needed == "libfixture.so":
                            self.assertEqual(result.returncode, 0, result.stderr)
                        else:
                            self.assertNotEqual(result.returncode, 0, result.stdout)
                            self.assertIn(executable, result.stderr)
                            self.assertIn("DT_NEEDED filename", result.stderr)
                            self.assertIn(needed, result.stderr)

    def test_inspect_wheel_inherits_rpath_only_along_dependency_chains(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            executable = "mcp_console-0.0.2.data/scripts/mcp-console"
            libexec = "mcp_console-0.0.2.data/data/libexec"
            for name, root_paths, child_paths, accepted in (
                ("inherited RPATH", {"rpath": "$ORIGIN/../libexec"}, {}, True),
                ("cyclic RPATH chain", {"rpath": "$ORIGIN/../libexec"}, {}, True),
                ("direct-only RUNPATH", {"runpath": "$ORIGIN/../libexec"}, {}, False),
                (
                    "child supplies RUNPATH",
                    {"runpath": "$ORIGIN/../libexec"},
                    {"runpath": "${ORIGIN}"},
                    True,
                ),
                (
                    "ancestor RPATH survives an intermediate RUNPATH",
                    {"rpath": "$ORIGIN/../libexec"},
                    {"runpath": "$ORIGIN"},
                    True,
                ),
                (
                    "RUNPATH overrides local RPATH",
                    {"rpath": "$ORIGIN/../libexec", "runpath": "$ORIGIN/../libexec"},
                    {},
                    False,
                ),
                (
                    "child RUNPATH overrides inherited RPATH for direct needs",
                    {"rpath": "$ORIGIN/../libexec"},
                    {"runpath": "$ORIGIN/missing"},
                    False,
                ),
                (
                    "child RPATH keeps its own origin",
                    {"runpath": "$ORIGIN/../libexec"},
                    {"rpath": "${ORIGIN}/nested"},
                    True,
                ),
            ):
                with self.subTest(name=name):
                    self.write_wheel(wheel)
                    rewrite_wheel(
                        wheel,
                        {executable: elf_fixture(needed=["liba.so"], **root_paths)},
                    )
                    library_directory = (
                        f"{libexec}/nested" if "rpath" in child_paths else libexec
                    )
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            f"{libexec}/liba.so",
                            elf_fixture(
                                interpreter=None, needed=["libb.so"], **child_paths
                            ),
                        )
                        archive.writestr(
                            f"{library_directory}/libb.so",
                            elf_fixture(
                                interpreter=None,
                                needed=["libcfixture.so"],
                                runpath="$ORIGIN"
                                if name == "child supplies RUNPATH"
                                else None,
                            ),
                        )
                        archive.writestr(
                            f"{library_directory}/libcfixture.so",
                            elf_fixture(
                                interpreter=None,
                                needed=["liba.so"]
                                if name == "cyclic RPATH chain"
                                else ["libc.so.6"],
                            ),
                        )
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        cwd=directory,
                        env=environment,
                    )
                    if accepted:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn("undeclared dependency", result.stderr)

            # The runner is a separate process: it cannot borrow the main executable's RPATH.
            self.write_wheel(wheel)
            rewrite_wheel(
                wheel,
                {
                    executable: elf_fixture(
                        needed=["liba.so"], rpath="$ORIGIN/../libexec"
                    ),
                    f"{libexec}/mcp-console-sandbox": elf_fixture(needed=["liba.so"]),
                },
            )
            with zipfile.ZipFile(wheel, "a") as archive:
                archive.writestr(f"{libexec}/liba.so", elf_fixture(interpreter=None))
            result = self.run_script(
                "inspect-wheel",
                str(wheel),
                "--release",
                cwd=directory,
                env=environment,
            )
            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn(
                "mcp-console-sandbox: undeclared dependency liba.so", result.stderr
            )

    def test_inspect_wheel_reuses_loaded_dependencies_per_entry_point(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            executable = "mcp_console-0.0.2.data/scripts/mcp-console"
            libexec = "mcp_console-0.0.2.data/data/libexec"
            for name, needed, libraries, separate_executable, accepted in (
                (
                    "direct RUNPATH siblings",
                    ["liba.so", "libb.so"],
                    {"liba.so": {"needed": ["libb.so"]}, "libb.so": {}},
                    False,
                    True,
                ),
                (
                    "breadth-first transitive siblings",
                    ["liba.so", "libb.so"],
                    {
                        "liba.so": {"needed": ["libcfixture.so"], "runpath": "$ORIGIN"},
                        "libb.so": {"needed": ["libd.so"], "runpath": "$ORIGIN"},
                        "libcfixture.so": {"needed": ["libd.so"]},
                        "libd.so": {},
                    },
                    False,
                    True,
                ),
                (
                    "declared dependency order",
                    ["libb.so", "liba.so"],
                    {
                        "libb.so": {"needed": ["libcfixture.so"], "runpath": "$ORIGIN"},
                        "liba.so": {
                            "needed": ["libcfixture.so"],
                            "runpath": "$ORIGIN/missing",
                        },
                        "libcfixture.so": {},
                    },
                    False,
                    True,
                ),
                (
                    "unloaded transitive dependency",
                    ["liba.so"],
                    {"liba.so": {"needed": ["libb.so"]}, "libb.so": {}},
                    False,
                    False,
                ),
                (
                    "independent executable cannot reuse loaded siblings",
                    ["liba.so", "libb.so"],
                    {"liba.so": {"needed": ["libb.so"]}, "libb.so": {}},
                    True,
                    False,
                ),
            ):
                with self.subTest(name=name):
                    self.write_wheel(wheel)
                    replacements = {
                        executable: elf_fixture(
                            needed=needed, runpath="$ORIGIN/../libexec"
                        )
                    }
                    if separate_executable:
                        replacements[f"{libexec}/mcp-console-sandbox"] = elf_fixture(
                            needed=["libb.so"]
                        )
                    rewrite_wheel(wheel, replacements)
                    with zipfile.ZipFile(wheel, "a") as archive:
                        for library, fields in libraries.items():
                            archive.writestr(
                                f"{libexec}/{library}",
                                elf_fixture(interpreter=None, **fields),
                            )
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        cwd=directory,
                        env=environment,
                    )
                    if accepted:
                        self.assertEqual(result.returncode, 0, result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        owner = (
                            "mcp-console-sandbox" if separate_executable else "liba.so"
                        )
                        self.assertIn(
                            f"{owner}: undeclared dependency libb.so", result.stderr
                        )

    def test_inspect_wheel_rejects_loader_paths_outside_installed_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            for member, runpath in (
                ("mcp_console-0.0.2.data/scripts/mcp-console", "$ORIGIN/../../outside"),
                (
                    "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox",
                    "$ORIGIN/../../../outside",
                ),
            ):
                with self.subTest(member=member):
                    self.write_wheel(wheel)
                    rewrite_wheel(wheel, {member: elf_fixture(runpath=runpath)})
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        cwd=directory,
                        env=environment,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn("RUNPATH escapes the installed wheel", result.stderr)
                    self.assertIn(member, result.stderr)

    def test_inspect_wheel_rejects_incompatible_private_and_extra_elf(self) -> None:
        defects = (
            ({"versions": ["GLIBC_2.36"]}, "GLIBC_2.36"),
            ({"versions": ["GLIBCXX_3.4.31"]}, "GLIBCXX_3.4.31"),
            ({"versions": ["CXXABI_1.3.14"]}, "CXXABI_1.3.14"),
            ({"machine": "AArch64"}, "machine"),
            ({"interpreter": "/opt/builder/ld-linux.so"}, "interpreter"),
            ({"runpath": "/opt/builder/lib"}, "RUNPATH"),
            ({"needed": ["libssl.so.3"]}, "undeclared dependency"),
        )
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            wheel = directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
            for member in (
                "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox",
                "mcp_console/extra.so",
            ):
                for fields, diagnostic in defects:
                    with self.subTest(member=member, defect=fields):
                        self.write_wheel(wheel)
                        if member.endswith("extra.so"):
                            with zipfile.ZipFile(wheel, "a") as archive:
                                archive.writestr(member, elf_fixture(**fields))
                        else:
                            rewrite_wheel(wheel, {member: elf_fixture(**fields)})
                        result = self.run_script(
                            "inspect-wheel",
                            str(wheel),
                            "--release",
                            cwd=directory,
                            env=environment,
                        )
                        self.assertNotEqual(result.returncode, 0, result.stdout)
                        self.assertIn(diagnostic, result.stderr)
                        self.assertIn(member, result.stderr)

    def test_inspect_wheel_rejects_false_or_inconsistent_tags(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, _, _ = self.smoke_environment(directory)
            for platform, metadata_tag, diagnostic in (
                ("manylinux_2_28_x86_64", None, "GLIBC_2.34"),
                ("manylinux_2_39_x86_64", None, "release floor"),
                ("manylinux_2_33_x86_64", None, "unsupported manylinux C++ policy"),
                (
                    "manylinux_2_35_x86_64",
                    "py3-none-manylinux_2_35_aarch64",
                    "WHEEL tags",
                ),
                ("manylinux_2_35_x86_64.manylinux_2_28_x86_64", None, "GLIBC_2.34"),
            ):
                with self.subTest(platform=platform, metadata=metadata_tag):
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    self.write_wheel(wheel)
                    if metadata_tag:
                        rewrite_wheel(
                            wheel,
                            {
                                "mcp_console-0.0.2.dist-info/WHEEL": f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: {metadata_tag}\n".encode()
                            },
                        )
                    result = self.run_script(
                        "inspect-wheel",
                        str(wheel),
                        "--release",
                        cwd=directory,
                        env=environment,
                    )
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertIn(diagnostic, result.stderr)

    def test_inspect_wheel_checks_cpp_symbols_against_every_advertised_policy(
        self,
    ) -> None:
        for architecture in ("x86_64", "aarch64"):
            policies = [
                ("manylinux2014", 19, 7),
                (
                    "manylinux_2_26",
                    22 if architecture == "x86_64" else 24,
                    10 if architecture == "x86_64" else 11,
                ),
                ("manylinux_2_31", 28, 12),
                ("manylinux_2_34", 29, 13),
                ("manylinux_2_35", 30, 13),
                ("manylinux_2_35.manylinux_2_34", 29, 13),
                ("manylinux_2_39", 33, 15),
            ]
            if architecture == "x86_64":
                policies[:0] = [("manylinux1", 8, 1), ("manylinux2010", 13, 3)]
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, _, _ = self.smoke_environment(directory)
                for policy, glibcxx, cxxabi in policies:
                    platform = ".".join(
                        f"{tag}_{architecture}" for tag in policy.split(".")
                    )
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    for symbols, rejected in (
                        ([f"GLIBCXX_3.4.{glibcxx}", f"CXXABI_1.3.{cxxabi}"], None),
                        ([f"GLIBCXX_3.4.{glibcxx + 1}"], f"GLIBCXX_3.4.{glibcxx + 1}"),
                        ([f"CXXABI_1.3.{cxxabi + 1}"], f"CXXABI_1.3.{cxxabi + 1}"),
                    ):
                        self.write_wheel(
                            wheel, glibc="2.5" if architecture == "x86_64" else "2.17"
                        )
                        member = "mcp_console/extra.so"
                        with zipfile.ZipFile(wheel, "a") as archive:
                            archive.writestr(
                                member,
                                elf_fixture(
                                    machine="AArch64"
                                    if architecture == "aarch64"
                                    else "Advanced Micro Devices X86-64",
                                    interpreter=None,
                                    needed=["libstdc++.so.6"],
                                    versions=symbols,
                                ),
                            )
                        for release in (False, True):
                            with self.subTest(
                                platform=platform, symbols=symbols, release=release
                            ):
                                result = self.run_script(
                                    "inspect-wheel",
                                    str(wheel),
                                    *(["--release"] if release else []),
                                    cwd=directory,
                                    env=environment,
                                )
                                if release and policy == "manylinux_2_39":
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn("release floor", result.stderr)
                                elif rejected:
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn(rejected, result.stderr)
                                    self.assertIn(member, result.stderr)
                                else:
                                    self.assertEqual(
                                        result.returncode, 0, result.stderr
                                    )

    def test_wheel_commands_check_named_glibc_against_every_advertised_policy(
        self,
    ) -> None:
        for architecture in ("x86_64", "aarch64"):
            arm = architecture == "aarch64"
            cases = [
                ("manylinux_2_35", "ABI_DT_RELR", False),
                ("manylinux_2_36", "ABI_DT_RELR", True),
                ("manylinux_2_39", "ABI_DT_RELR", True),
                ("manylinux_2_39.manylinux_2_36", "ABI_DT_RELR", True),
                ("manylinux_2_39.manylinux_2_35", "ABI_DT_RELR", False),
                ("manylinux_2_39.manylinux2014", "ABI_DT_RELR", False),
                ("manylinux_2_39", "ABI_DT_X86_64_PLT", False),
                ("manylinux_2_42", "ABI_DT_X86_64_PLT", not arm),
                ("manylinux_2_39", "ABI_GNU2_TLS", False),
                ("manylinux_2_42", "ABI_GNU2_TLS", not arm),
                ("manylinux_2_42.manylinux_2_39", "ABI_GNU2_TLS", False),
                ("manylinux_2_42", "PRIVATE", False),
                ("manylinux_2_42", "UNKNOWN", False),
                ("linux", "ABI_DT_RELR", True),
            ]
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, _, _ = self.smoke_environment(directory)
                environment["FAKE_NO_R"] = "1"
                for policy, number, permitted in cases:
                    platform = ".".join(
                        f"{tag}_{architecture}" for tag in policy.split(".")
                    )
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    self.write_wheel(wheel, glibc="2.17")
                    member = "mcp_console/extra.so"
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            member,
                            elf_fixture(
                                machine="AArch64"
                                if arm
                                else "Advanced Micro Devices X86-64",
                                interpreter=None,
                                versions=["GLIBC_2.17", f"GLIBC_{number}"],
                            ),
                        )
                    for release in (False, True):
                        with self.subTest(
                            platform=platform, number=number, release=release
                        ):
                            report = directory / "abi.json"
                            result = self.run_script(
                                "inspect-wheel",
                                str(wheel),
                                *(["--release"] if release else []),
                                "--report",
                                str(report),
                                cwd=directory,
                                env=environment,
                            )
                            if release and policy != "manylinux_2_35":
                                self.assertNotEqual(result.returncode, 0, result.stdout)
                                self.assertIn("release floor", result.stderr)
                            elif not permitted:
                                self.assertNotEqual(result.returncode, 0, result.stdout)
                                self.assertIn(f"GLIBC_{number}", result.stderr)
                                self.assertIn(member, result.stderr)
                            else:
                                self.assertEqual(result.returncode, 0, result.stderr)
                                evidence = json.loads(report.read_text())
                                self.assertEqual(
                                    evidence["elf"][member]["glibc"], ["2.17", number]
                                )
                    if policy == "manylinux_2_39" and number == "ABI_DT_RELR":
                        with self.subTest(platform=platform, command="smoke-wheel"):
                            result = self.run_script(
                                "smoke-wheel",
                                str(wheel),
                                "--installed-only",
                                "--sandbox-pin",
                                str(directory / "sandbox-runner.json"),
                                "--without-r",
                                cwd=directory,
                                env=environment,
                            )
                            self.assertEqual(result.returncode, 0, result.stderr)

    def test_inspect_wheel_checks_named_cxxabi_against_every_advertised_policy(
        self,
    ) -> None:
        for architecture in ("x86_64", "aarch64"):
            arm = architecture == "aarch64"
            named = {"TM_1"} if arm else {"TM_1", "FLOAT128"}
            policies = [
                ("manylinux2014", {"TM_1"}),
                ("manylinux_2_24", named),
                ("manylinux_2_34", named),
                ("manylinux_2_35", named),
                ("manylinux_2_35.manylinux2014", {"TM_1"}),
            ]
            if not arm:
                policies[:0] = [("manylinux2010", set())]
                policies.append(("manylinux_2_35.manylinux2010", set()))
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, _, _ = self.smoke_environment(directory)
                for policy, permitted in policies:
                    platform = ".".join(
                        f"{tag}_{architecture}" for tag in policy.split(".")
                    )
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    names = ["TM_1", "FLOAT128"]
                    if policy == "manylinux_2_35":
                        names.append("UNKNOWN")
                    for number in names:
                        self.write_wheel(wheel, glibc="2.17" if arm else "2.5")
                        member = (
                            "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox"
                        )
                        rewrite_wheel(
                            wheel,
                            {
                                member: elf_fixture(
                                    machine="AArch64"
                                    if arm
                                    else "Advanced Micro Devices X86-64",
                                    interpreter="/lib/ld-linux-aarch64.so.1"
                                    if arm
                                    else "/lib64/ld-linux-x86-64.so.2",
                                    needed=["libstdc++.so.6"],
                                    versions=["CXXABI_1.3.1", f"CXXABI_{number}"],
                                )
                            },
                        )
                        for release in (False, True):
                            with self.subTest(
                                platform=platform, number=number, release=release
                            ):
                                report = directory / "abi.json"
                                result = self.run_script(
                                    "inspect-wheel",
                                    str(wheel),
                                    *(["--release"] if release else []),
                                    "--report",
                                    str(report),
                                    cwd=directory,
                                    env=environment,
                                )
                                if number not in permitted:
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn(f"CXXABI_{number}", result.stderr)
                                    self.assertIn(member, result.stderr)
                                else:
                                    self.assertEqual(
                                        result.returncode, 0, result.stderr
                                    )
                                    evidence = json.loads(report.read_text())
                                    self.assertEqual(
                                        evidence["elf"][member]["cxxabi"],
                                        ["1.3.1", number],
                                    )

    def test_inspect_wheel_checks_gcc_symbols_against_every_advertised_policy(
        self,
    ) -> None:
        for architecture in ("x86_64", "aarch64"):
            arm = architecture == "aarch64"
            policies = [
                ("manylinux2014", "4.7.0" if arm else "4.8.0", "7.0.0"),
                (
                    "manylinux_2_26",
                    "7.0.0" if arm else "4.8.0",
                    "11.0" if arm else "7.0.0",
                ),
                ("manylinux_2_28", "7.0.0", "11.0"),
                ("manylinux_2_34", "11.0" if arm else "7.0.0", "12.0.0"),
                (
                    "manylinux_2_35",
                    "11.0" if arm else "12.0.0",
                    "12.0.0" if arm else "11.0",
                ),
                ("manylinux_2_35.manylinux_2_28", "7.0.0", "11.0" if arm else "12.0.0"),
                ("manylinux_2_39", "14.0.0", "15.0.0"),
            ]
            if arm:
                # GCC's sparse policy sets cannot be checked with numeric maxima.
                policies.append(("manylinux_2_28", "4.5.0", "4.8.0"))
            else:
                policies[:0] = [
                    ("manylinux1", "4.2.0", "4.3.0"),
                    ("manylinux2010", "4.3.0", "4.7.0"),
                ]
            with tempfile.TemporaryDirectory() as temporary:
                directory = Path(temporary)
                environment, _, _ = self.smoke_environment(directory)
                for policy, permitted, rejected in policies:
                    platform = ".".join(
                        f"{tag}_{architecture}" for tag in policy.split(".")
                    )
                    wheel = directory / f"mcp_console-0.0.2-py3-none-{platform}.whl"
                    member = "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox"
                    for number in (permitted, rejected):
                        self.write_wheel(wheel, glibc="2.17" if arm else "2.5")
                        rewrite_wheel(
                            wheel,
                            {
                                member: elf_fixture(
                                    machine="AArch64"
                                    if arm
                                    else "Advanced Micro Devices X86-64",
                                    interpreter="/lib/ld-linux-aarch64.so.1"
                                    if arm
                                    else "/lib64/ld-linux-x86-64.so.2",
                                    needed=["libc.so.6", "libgcc_s.so.1"],
                                    versions=[f"GCC_{number}", "GCC_3.0"],
                                )
                            },
                        )
                        for release in (False, True):
                            if release and policy == "manylinux_2_39":
                                continue
                            with self.subTest(
                                platform=platform, number=number, release=release
                            ):
                                report = directory / "abi.json"
                                result = self.run_script(
                                    "inspect-wheel",
                                    str(wheel),
                                    *(["--release"] if release else []),
                                    "--report",
                                    str(report),
                                    cwd=directory,
                                    env=environment,
                                )
                                if number == rejected:
                                    self.assertNotEqual(
                                        result.returncode, 0, result.stdout
                                    )
                                    self.assertIn(f"GCC_{number}", result.stderr)
                                    self.assertIn(member, result.stderr)
                                else:
                                    self.assertEqual(
                                        result.returncode, 0, result.stderr
                                    )
                                    evidence = json.loads(report.read_text())
                                    self.assertEqual(
                                        evidence["elf"][member].get("gcc"),
                                        sorted(["3.0", number]),
                                    )

    def test_smoke_wheel_requires_a_private_companion_bundle(self) -> None:
        for defect in (
            "missing runner",
            "missing notice",
            "not executable",
            "public wheel",
            "public uv",
        ):
            with (
                self.subTest(defect=defect),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                environment, wheel, cargo_bin = self.smoke_environment(directory)
                if defect in {"missing runner", "missing notice", "not executable"}:
                    self.write_wheel(
                        wheel,
                        omit={
                            "missing runner": "mcp-console-sandbox",
                            "missing notice": "NOTICE",
                        }.get(defect),
                        executable=defect != "not executable",
                    )
                elif defect == "public wheel":
                    with zipfile.ZipFile(wheel, "a") as archive:
                        archive.writestr(
                            "mcp_console-0.0.2.data/scripts/mcp-console-sandbox",
                            "fixture\n",
                        )
                else:
                    (
                        Path(environment["UV_TOOL_BIN_DIR"]) / "mcp-console-sandbox"
                    ).touch()
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    str(cargo_bin),
                    cwd=directory,
                    env=environment,
                )
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertIn("private sandbox runner", result.stderr)

    def test_smoke_wheel_isolates_console_files(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            record = directory / "locations.jsonl"
            ambient = directory / "ambient-console"
            ambient.mkdir()
            (directory / ".agents/console").mkdir(parents=True)
            environment |= {
                "FAKE_MCP_LOCATIONS": str(record),
                "MCP_CONSOLE_HOME": str(ambient),
            }
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            launches = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertEqual(
                [item["command"] for item in launches], ["sandbox", "sandbox", "serve"]
            )
            for launch in launches:
                self.assertEqual(launch["interpreter"], sys.executable)
                self.assertEqual(launch["home"], environment.get("HOME"))
                self.assertNotEqual(launch["cwd"], str(directory.resolve()))
                self.assertNotEqual(launch["console"], str(ambient))
                self.assertTrue(Path(launch["console"]).is_absolute())
                self.assertFalse(Path(launch["cwd"]).exists())
                self.assertFalse(Path(launch["console"]).exists())

    def test_smoke_wheel_requires_runnable_cargo_binary(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_SANDBOX_FAILURE"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0, result.stdout)
            self.assertIn("private sandbox runner failed", result.stderr)

    def test_smoke_installed_wheel_needs_no_checkout_or_build_output(self) -> None:
        for without_r in (False, True):
            with (
                self.subTest(without_r=without_r),
                tempfile.TemporaryDirectory() as temporary,
            ):
                directory = Path(temporary)
                environment, _, cargo_bin = self.smoke_environment(directory)
                wheel = (
                    directory / "mcp_console-0.0.2-py3-none-manylinux_2_35_x86_64.whl"
                )
                self.write_wheel(wheel)
                rewrite_wheel(
                    wheel,
                    {
                        "mcp_console-0.0.2.data/data/libexec/mcp-console-sandbox": elf_fixture(
                            needed=["libstdc++.so.6"],
                            versions=["CXXABI_FLOAT128", "CXXABI_TM_1"],
                        )
                    },
                )
                cargo_bin.unlink()
                (directory / "Cargo.toml").unlink()
                empty = directory / "empty-workspace"
                empty.mkdir()
                record = directory / "arguments.jsonl"
                environment["FAKE_MCP_ARGUMENTS"] = str(record)
                if without_r:
                    environment["FAKE_NO_R"] = "1"
                result = self.run_script(
                    "smoke-wheel",
                    str(wheel),
                    "--installed-only",
                    "--sandbox-pin",
                    str(directory / "sandbox-runner.json"),
                    *(["--without-r"] if without_r else []),
                    cwd=empty,
                    env=environment,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = [json.loads(line) for line in record.read_text().splitlines()]
                self.assertEqual(calls.count(["sandbox", "--", "/usr/bin/true"]), 1)
                self.assertIn(["serve"], calls)
                self.assertEqual(list(empty.iterdir()), [])

    def test_release_fixtures_use_python_with_spaces(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            environment = Path(temporary) / "python with spaces"
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(environment)],
                check=True,
                capture_output=True,
                text=True,
            )
            result = subprocess.run(
                [
                    str(environment / "bin/python"),
                    str(Path(__file__).resolve()),
                    "ReleaseScriptTests.test_smoke_wheel_evaluates_peers_and_bounds_response_waits",
                    "ReleaseScriptTests.test_smoke_wheel_isolates_console_files",
                    "RuntimeSourceValidationTests.test_r_home_selects_the_syntax_checker_without_r_on_path",
                ],
                cwd=ROOT,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_smoke_wheel_evaluates_peers_and_bounds_response_waits(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            sandbox_path = Path(environment["FAKE_SANDBOX_PATH_RECORD"]).read_text()
            self.assertEqual(len(sandbox_path.split(os.pathsep)), 1)
            self.assertNotEqual(sandbox_path, environment["PATH"])
            environment["FAKE_UV_HELP_EXTRA_NEWLINE"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("installed and Cargo help output differ", result.stderr)
            del environment["FAKE_UV_HELP_EXTRA_NEWLINE"]

            environment["FAKE_MCP_EVALUATION_HANG"] = "1"
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 0.01 seconds", result.stderr)

    def test_smoke_wheel_evaluates_r_selected_only_by_home(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            commands = directory / "commands"
            r_home = directory / "selected-R"
            (r_home / "bin").mkdir(parents=True)
            (commands / "R").rename(r_home / "bin" / "R")
            environment.update(PATH=str(commands), R_HOME=str(r_home))
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_wheel_accepts_a_host_without_r(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            commands = directory / "commands"
            (commands / "R").unlink()
            environment.update(PATH=str(commands), FAKE_NO_R="1")
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_smoke_wheel_without_r_hides_host_selection(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            record = directory / "launches.jsonl"
            environment.update(
                R_HOME="/inherited/R",
                FAKE_NO_R="1",
                FAKE_MCP_LOCATIONS=str(record),
            )
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--without-r",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            launch = json.loads(record.read_text().splitlines()[-1])
            self.assertEqual(launch["command"], "serve")
            self.assertIsNone(launch["r_home"])
            self.assertIsNone(launch["r"])
            self.assertIsNone(launch["rscript"])
            self.assertEqual(launch["python"], str(Path(sys.executable).resolve()))

    def test_smoke_wheel_reports_startup_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            payload = {
                "content": [{"type": "text", "text": "startup failed: café"}],
                "isError": True,
            }
            environment["FAKE_MCP_STARTUP_RESULT"] = json.dumps(payload)
            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "1",
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 1)
            response = {"jsonrpc": "2.0", "id": 2, "result": payload}
            self.assertIn(
                "unexpected runtime startup response: "
                + json.dumps(response, ensure_ascii=False),
                result.stderr,
            )

    def test_smoke_wheel_bounds_runtime_startup_separately(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            environment["FAKE_MCP_STARTUP_HANG"] = "1"

            result = self.run_script(
                "smoke-wheel",
                str(wheel),
                str(cargo_bin),
                "--target",
                "aarch64-apple-darwin",
                "--startup-timeout-seconds",
                "1",
                "--response-timeout-seconds",
                "0.01",
                cwd=directory,
                env=environment,
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("MCP response timed out after 1 seconds", result.stderr)

    def test_smoke_linux_wheel_requires_sandbox_and_bundled_helper(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            environment, wheel, cargo_bin = self.smoke_environment(directory)
            linux_wheel = wheel.with_name(
                "mcp_console-0.0.2-py3-none-manylinux_2_34_x86_64.whl"
            )
            self.write_wheel(linux_wheel)
            wheel.unlink()
            record = directory / "arguments.jsonl"
            environment["FAKE_MCP_ARGUMENTS"] = str(record)
            command = (
                "smoke-wheel",
                str(linux_wheel),
                str(cargo_bin),
                "--target",
                "x86_64-unknown-linux-gnu",
            )
            result = self.run_script(
                *command,
                cwd=directory,
                env=environment,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            invocations = [json.loads(line) for line in record.read_text().splitlines()]
            self.assertIn(["serve"], invocations)
            self.assertTrue(any(call[:1] == ["sandbox"] for call in invocations))

            for missing in (
                "mcp-console-sandbox",
                "bwrap",
                "bubblewrap-COPYING",
                "bubblewrap-NOTICE",
                "bubblewrap-SOURCE.json",
            ):
                with self.subTest(missing=missing):
                    self.write_wheel(linux_wheel, omit=missing)
                    result = self.run_script(*command, cwd=directory, env=environment)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("private sandbox runner", result.stderr)

            for field, value in (
                ("source_revision", "b" * 40),
                ("source_repository", "wrong/source"),
                ("source_directory", "old/bubblewrap"),
                ("build_script", "old/build.rs"),
                ("wrapper_directory", "old/wrapper"),
                ("sha256", "0" * 64),
                ("elf_needed", ["libc.so.6"]),
                ("libcap_linkage", "static"),
            ):
                with self.subTest(field=field):
                    self.write_wheel(linux_wheel)
                    with zipfile.ZipFile(linux_wheel) as archive:
                        entries = [
                            (info, archive.read(info)) for info in archive.infolist()
                        ]
                    with zipfile.ZipFile(linux_wheel, "w") as archive:
                        for info, contents in entries:
                            if info.filename.endswith("bubblewrap-SOURCE.json"):
                                metadata = json.loads(contents)
                                metadata[field] = value
                                contents = json.dumps(metadata).encode()
                            archive.writestr(info, contents)
                    result = self.run_script(*command, cwd=directory, env=environment)
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("provenance", result.stderr)

            for notice_name, replacement in (
                ("bubblewrap-NOTICE", b"notice for obsolete source"),
                ("bubblewrap-NOTICE", b""),
                ("bubblewrap-COPYING", b""),
            ):
                self.write_wheel(linux_wheel)
                with zipfile.ZipFile(linux_wheel) as archive:
                    entries = [
                        (info, archive.read(info)) for info in archive.infolist()
                    ]
                with zipfile.ZipFile(linux_wheel, "w") as archive:
                    for info, contents in entries:
                        if info.filename.endswith("/" + notice_name):
                            contents = replacement
                        archive.writestr(info, contents)
                result = self.run_script(*command, cwd=directory, env=environment)
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn(notice_name, result.stderr)

            for missing_notice in (True, False):
                self.write_wheel(linux_wheel)
                with zipfile.ZipFile(linux_wheel) as archive:
                    entries = [
                        (info, archive.read(info)) for info in archive.infolist()
                    ]
                with zipfile.ZipFile(linux_wheel, "w") as archive:
                    for info, contents in entries:
                        if info.filename.endswith("bubblewrap-SOURCE.json"):
                            metadata = json.loads(contents)
                            metadata.update(
                                libcap_linkage="static", elf_needed=["libc.so.6"]
                            )
                            contents = json.dumps(metadata).encode()
                        archive.writestr(info, contents)
                    if not missing_notice:
                        archive.writestr(
                            "mcp_console-0.0.2.data/data/share/licenses/mcp-console/libcap-NOTICE",
                            "fixture libcap notice",
                        )
                result = self.run_script(
                    *command,
                    cwd=directory,
                    env=environment | {"FAKE_STATIC_LIBCAP": "1"},
                )
                if missing_notice:
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn("libcap-NOTICE", result.stderr)
                else:
                    self.assertEqual(result.returncode, 0, result.stderr)
                # The same notice must not describe a dynamic dependency as bundled.
                result = self.run_script(*command, cwd=directory, env=environment)
                self.assertNotEqual(result.returncode, 0, result.stderr)

    def test_stage_runner_exports_source_pin_without_a_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "scripts").mkdir()
            script = root / "scripts" / STAGE_SCRIPT.name
            shutil.copyfile(STAGE_SCRIPT, script)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            pin = {"repository": "fixture/runner", "commit": "a" * 40}
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            output = root / "github-output"
            result = subprocess.run(
                [sys.executable, str(script), "--github-output", str(output)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(
                output.read_text(),
                "".join(f"{key}={value}\n" for key, value in pin.items()),
            )

    def test_stage_runner_builds_the_pin_and_records_artifact_integrity(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            root = directory / "project"
            scripts = root / "scripts"
            scripts.mkdir(parents=True)
            shutil.copyfile(STAGE_SCRIPT, scripts / STAGE_SCRIPT.name)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            placeholder = root / "wheel-data/data/.gitignore"
            placeholder.parent.mkdir(parents=True)
            placeholder.write_text("/*\n!/.gitignore\n")
            # The toolchain below is fake; exercise its macOS staging contract
            # on every test host without building or executing a native runner.
            launcher = scripts / "macos-stage.py"
            write_executable(
                launcher,
                # fmt: python
                """
                import runpy
                import sys

                sys.platform = "darwin"
                sys.argv.pop(0)
                runpy.run_path(sys.argv[0], run_name="__main__")
                """,
            )
            pin = {
                "repository": "t-kalinowski/codex",
                "release": "rust-v0.150.1",
                "commit": "a" * 40,
            }
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            checkout = directory / "source"
            crate = checkout / "codex-rs" / "mcp-console-sandbox"
            crate.mkdir(parents=True)
            (crate / "Cargo.toml").touch()
            toolchain_file = checkout / "codex-rs/rust-toolchain.toml"
            toolchain_file.write_text('[toolchain]\nchannel = "fixture-sandbox"\n')
            (checkout / "LICENSE").write_text("license\n")
            (checkout / "NOTICE").write_text("notice\n")
            vendor = checkout / "codex-rs/vendor/bubblewrap"
            vendor.mkdir(parents=True)
            (vendor / "COPYING").write_text("bwrap license\n")
            (vendor / "bubblewrap.c").write_text(
                "/* Copyright fixture author; SPDX-License-Identifier: LGPL-2.0-or-later */\n"
            )
            commands = directory / "commands"
            commands.mkdir()
            write_executable(
                commands / "git",
                # fmt: python
                """
                #!/usr/bin/env python3
                import os
                import sys

                if sys.argv[1:] == ["rev-parse", "HEAD"]:
                    print(os.environ["FAKE_SOURCE_REVISION"])
                elif sys.argv[1:] == ["status", "--porcelain", "--untracked-files=all"]:
                    print(os.environ.get("FAKE_SOURCE_DIRTY", ""), end="")
                else:
                    raise SystemExit(2)
                """,
            )
            write_executable(
                commands / "rustc",
                # fmt: python
                """
                #!/usr/bin/env python3
                import sys

                assert sys.argv[1:] == ["+fixture-sandbox", "--print", "host-tuple"]
                print("aarch64-apple-darwin")
                """,
            )
            write_executable(
                commands / "cargo",
                # fmt: python
                """
                #!/usr/bin/env python3
                import json
                import os
                import sys
                from pathlib import Path

                with Path(os.environ["FAKE_CARGO_ARGUMENTS"]).open("a") as record:
                    record.write(
                        json.dumps(
                            {
                                "arguments": sys.argv[1:],
                                "helper_sha256": os.environ.get("CODEX_BWRAP_SHA256"),
                            }
                        )
                        + "\\n"
                    )
                target = (
                    sys.argv[sys.argv.index("--target") + 1]
                    if "--target" in sys.argv
                    else os.environ["CARGO_BUILD_TARGET"]
                )
                output = Path(os.environ["CARGO_TARGET_DIR"]) / target / "release"
                output.mkdir(parents=True, exist_ok=True)
                (output / "mcp-console-sandbox").write_bytes(b"runner bytes with debug symbols")
                (output / "bwrap").write_bytes(b"bwrap bytes with debug symbols")
                """,
            )
            write_executable(
                commands / "rustup",
                # fmt: python
                """
                #!/usr/bin/env python3
                import os
                import sys

                assert sys.argv[1:4] == ["run", "--install", "fixture-sandbox"]
                os.execvp(sys.argv[4], [sys.argv[4], "+fixture-sandbox", *sys.argv[5:]])
                """,
            )
            write_readelf_fixture(commands)
            for name in ("xcrun", "strip"):
                write_executable(
                    commands / name,
                    # fmt: python
                    """
                    #!/usr/bin/env python3
                    import sys
                    from pathlib import Path

                    executable = Path(sys.argv[-1])
                    original = executable.read_bytes()
                    assert original.endswith(b" with debug symbols")
                    executable.write_bytes(original.removesuffix(b" with debug symbols"))
                    """,
                )
            environment = os.environ.copy()
            environment.update(
                {
                    "PATH": f"{commands}{os.pathsep}{environment['PATH']}",
                    "FAKE_SOURCE_REVISION": pin["commit"],
                    "FAKE_CARGO_ARGUMENTS": str(directory / "cargo.json"),
                    "CARGO_BUILD_TARGET": "x86_64-apple-darwin",
                    "RUSTUP_TOOLCHAIN": "fixture-console",
                    "LIBCAP_STATIC": "1",  # Requested linkage is not ELF evidence.
                }
            )
            command = [
                sys.executable,
                str(launcher),
                str(scripts / STAGE_SCRIPT.name),
                str(checkout),
            ]

            def reject_build_overrides() -> None:
                for name, value in (
                    ("CODEX_BWRAP_SOURCE_DIR", ""),
                    ("CODEX_BWRAP_SOURCE_DIR", str(vendor)),
                    ("CODEX_BWRAP_SOURCE_DIR", str(directory / "alternative")),
                    ("CODEX_SKIP_BWRAP_BUILD", "1"),
                    ("CODEX_SKIP_BWRAP_BUILD", ""),
                ):
                    with self.subTest(override=name, value=value):
                        log = directory / "cargo.json"
                        log.unlink(missing_ok=True)
                        result = subprocess.run(
                            command + ["--target", "x86_64-unknown-linux-gnu"],
                            env=environment | {name: value},
                            capture_output=True,
                            text=True,
                        )
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertIn(name, result.stderr)
                        self.assertFalse(
                            log.exists(), "must reject before Cargo can reuse a helper"
                        )

            (directory / "alternative").mkdir()
            reject_build_overrides()  # Clean staging, no helper exists yet.
            for arguments, target in (
                ([], "aarch64-apple-darwin"),
                (["--target", "x86_64-unknown-linux-gnu"], "x86_64-unknown-linux-gnu"),
                (
                    ["--target", "aarch64-unknown-linux-gnu"],
                    "aarch64-unknown-linux-gnu",
                ),
                (["--target", "x86_64-apple-darwin"], "x86_64-apple-darwin"),
            ):
                with self.subTest(target=target):
                    (directory / "cargo.json").unlink(missing_ok=True)
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(placeholder.read_text(), "/*\n!/.gitignore\n")
                    packages = [("mcp-console-sandbox", "mcp-console-sandbox")]
                    if "linux" in target:
                        packages.insert(0, ("codex-bwrap", "bwrap"))
                    self.assertEqual(
                        [
                            json.loads(line)
                            for line in (directory / "cargo.json")
                            .read_text()
                            .splitlines()
                        ],
                        [
                            {
                                "arguments": [
                                    "+fixture-sandbox",
                                    "build",
                                    "--locked",
                                    "--release",
                                    "-p",
                                    package,
                                    "--bin",
                                    binary,
                                    "--target",
                                    target,
                                ],
                                "helper_sha256": hashlib.sha256(
                                    b"bwrap bytes"
                                ).hexdigest()
                                if "linux" in target and binary == "mcp-console-sandbox"
                                else None,
                            }
                            for package, binary in packages
                        ],
                    )
                    self.assertEqual(
                        json.loads(
                            (root / "target/sandbox-runner-build.json").read_text()
                        ),
                        {
                            "source_revision": pin["commit"],
                            "target": target,
                            "artifacts": {
                                "LICENSE": hashlib.sha256(b"license\n").hexdigest(),
                                "NOTICE": hashlib.sha256(b"notice\n").hexdigest(),
                                "mcp-console-sandbox": hashlib.sha256(
                                    b"runner bytes"
                                ).hexdigest(),
                                **(
                                    {
                                        "bubblewrap-SOURCE.json": hashlib.sha256(
                                            (
                                                json.dumps(
                                                    helper_metadata(
                                                        pin, b"bwrap bytes"
                                                    ),
                                                    indent=2,
                                                )
                                                + "\n"
                                            ).encode()
                                        ).hexdigest(),
                                        "bubblewrap-NOTICE": hashlib.sha256(
                                            bubblewrap_notice(pin).encode()
                                        ).hexdigest(),
                                        "bubblewrap-COPYING": hashlib.sha256(
                                            b"bwrap license\n"
                                        ).hexdigest(),
                                        "bwrap": hashlib.sha256(
                                            b"bwrap bytes"
                                        ).hexdigest(),
                                    }
                                    if "linux" in target
                                    else {}
                                ),
                            },
                        },
                    )
                    data = root / "wheel-data/data"
                    if "linux" in target:
                        reject_build_overrides()  # Incremental staging with a finished helper.
                    unchanged = [
                        root / "target/sandbox-runner-build.json",
                        *(path for path in data.rglob("*") if path.is_file()),
                    ]
                    for path in unchanged:
                        os.utime(path, ns=(1_000_000_000, 1_000_000_000))
                    obsolete = data / "libexec/obsolete-runner"
                    obsolete.write_bytes(b"obsolete")
                    if "linux" in target:
                        notice = (
                            data / "share/licenses/mcp-console/bubblewrap-SOURCE.json"
                        )
                        notice.write_text('{"source_revision": "stale"}')
                        unchanged.remove(notice)
                    result = subprocess.run(
                        command + arguments,
                        env=environment,
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertFalse(obsolete.exists())
                    if "linux" in target:
                        self.assertEqual(
                            json.loads(notice.read_text()),
                            helper_metadata(pin, b"bwrap bytes"),
                        )
                    self.assertEqual(
                        {
                            path.relative_to(root): path.stat().st_mtime_ns
                            for path in unchanged
                        },
                        {path.relative_to(root): 1_000_000_000 for path in unchanged},
                    )
            data = root / "wheel-data" / "data"
            runner = data / "libexec" / "mcp-console-sandbox"
            self.assertEqual(runner.read_bytes(), b"runner bytes")
            self.assertTrue(os.access(runner, os.X_OK))
            self.assertEqual(
                (data / "share/licenses/mcp-console/LICENSE").read_text(),
                "license\n",
            )
            self.assertEqual(
                (
                    checkout
                    / "codex-rs/target/x86_64-apple-darwin/release/mcp-console-sandbox"
                ).read_bytes(),
                b"runner bytes with debug symbols",
            )
            (directory / "cargo.json").unlink()
            for changes in (
                {"FAKE_SOURCE_REVISION": "b" * 40},
                {"FAKE_SOURCE_DIRTY": " M Cargo.lock"},
            ):
                result = subprocess.run(
                    command, env=environment | changes, capture_output=True, text=True
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse((directory / "cargo.json").exists())
            static_notice = directory / "libcap notice"
            static_notice.write_text("fixture libcap redistribution notice\n")
            for changes, expected in (
                ({}, "MCP_CONSOLE_LIBCAP_NOTICE"),
                (
                    {
                        "MCP_CONSOLE_LIBCAP_NOTICE": str(static_notice),
                        "FAKE_NO_LIBCAP": "1",
                    },
                    "cannot establish libcap linkage",
                ),
                ({"MCP_CONSOLE_LIBCAP_NOTICE": str(static_notice)}, None),
            ):
                with self.subTest(static=changes):
                    result = subprocess.run(
                        command + ["--target", "x86_64-unknown-linux-gnu"],
                        env=environment | {"FAKE_STATIC_LIBCAP": "1"} | changes,
                        capture_output=True,
                        text=True,
                    )
                    if expected:
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertIn(expected, result.stderr)
                    else:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        notice = data / "share/licenses/mcp-console/libcap-NOTICE"
                        self.assertEqual(
                            notice.read_bytes(), static_notice.read_bytes()
                        )
                        metadata = json.loads(
                            (
                                data
                                / "share/licenses/mcp-console/bubblewrap-SOURCE.json"
                            ).read_text()
                        )
                        self.assertEqual(metadata["libcap_linkage"], "static")
                        self.assertEqual(metadata["elf_needed"], ["libc.so.6"])
            # A later dynamic build must remove the static library notice.
            result = subprocess.run(
                command + ["--target", "x86_64-unknown-linux-gnu"],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(
                (data / "share/licenses/mcp-console/libcap-NOTICE").exists()
            )
            (directory / "cargo.json").unlink()
            toolchain_file.unlink()
            result = subprocess.run(
                command, env=environment, capture_output=True, text=True
            )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("FileNotFoundError", result.stderr)
            self.assertIn("rust-toolchain.toml", result.stderr)
            self.assertFalse((directory / "cargo.json").exists())

    def test_cargo_rejects_changed_staged_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "src").mkdir()
            (root / "src/main.rs").write_text("fn main() {}\n")
            # Exercise the build script with empty native sources at their real paths.
            for native_source in (ROOT / "src").rglob("*.c"):
                destination = root / native_source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            shutil.copyfile(ROOT / "build.rs", root / "build.rs")
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            (root / "Cargo.toml").write_text(
                """[package]
name = "sandbox-artifact-build"
version = "0.0.0"
"""
                """edition = "2024"
[build-dependencies]
"""
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            shutil.copyfile(ROOT / "Cargo.lock", root / "Cargo.lock")
            target = subprocess.check_output(
                ["rustc", "--print", "host-tuple"], text=True
            ).strip()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            artifacts = {
                "mcp-console-sandbox": b"runner bytes",
                "LICENSE": b"license",
                "NOTICE": b"notice",
            }
            if sys.platform == "linux":
                artifacts["bwrap"] = b"bwrap bytes"
                artifacts["bubblewrap-COPYING"] = b"bwrap license"
                artifacts["bubblewrap-NOTICE"] = bubblewrap_notice(pin).encode()
                artifacts["bubblewrap-SOURCE.json"] = json.dumps(
                    helper_metadata(pin, b"bwrap bytes")
                ).encode()
            staged = root / "wheel-data/data"
            staged_files = {}
            for name, contents in artifacts.items():
                relative = (
                    "libexec"
                    if name in ("mcp-console-sandbox", "bwrap")
                    else "share/licenses/mcp-console"
                )
                staged_files[name] = staged / relative / name
                staged_files[name].parent.mkdir(parents=True, exist_ok=True)
                staged_files[name].write_bytes(contents)
                staged_files[name].chmod(0o755)
            (root / "target").mkdir()
            (root / "target/sandbox-runner-build.json").write_text(
                json.dumps(
                    {
                        "source_revision": pin["commit"],
                        "target": target,
                        "artifacts": {
                            name: hashlib.sha256(contents).hexdigest()
                            for name, contents in artifacts.items()
                        },
                    }
                )
            )
            arguments = [
                "cargo",
                "build",
                "--target",
                target,
                "--target-dir",
                str(root / "target"),
            ]
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            # Let Cargo observe companions created by the initial build, then
            # require the next unchanged invocation to reuse the executable.
            result = subprocess.run(arguments, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            result = subprocess.run(
                [*arguments, "--message-format=json"],
                cwd=root,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            executable = [
                event
                for line in result.stdout.splitlines()
                if (event := json.loads(line)).get("executable")
            ]
            self.assertEqual(len(executable), 1, result.stdout)
            self.assertTrue(executable[0]["fresh"], result.stderr)
            prefix = root / "target" / target
            for name, contents in artifacts.items():
                with self.subTest(artifact=name):
                    relative = (
                        "libexec"
                        if name in ("mcp-console-sandbox", "bwrap")
                        else "share/licenses/mcp-console"
                    )
                    installed = prefix / relative / name
                    self.assertEqual(installed.read_bytes(), contents)
                    self.assertTrue(os.access(installed, os.X_OK))
                    staged_files[name].write_bytes(b"replaced artifact")
                    result = subprocess.run(
                        arguments, cwd=root, capture_output=True, text=True
                    )
                    self.assertNotEqual(result.returncode, 0, result.stderr)
                    self.assertIn(f"artifact {name} changed", result.stderr)
                    self.assertEqual(installed.read_bytes(), contents)
                    staged_files[name].write_bytes(contents)


class PortableReleaseTests(ReleaseFixture):
    @contextmanager
    def publication_server(
        self, responses: dict[str, list[tuple[int, dict[str, object]]]]
    ) -> Iterator[tuple[str, list[tuple[str, dict[str, object]]]]]:
        observed: list[tuple[str, dict[str, object]]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                sequence = responses[self.path]
                status, payload = sequence[0]
                if len(sequence) > 1:
                    sequence.pop(0)
                observed.append((self.path, payload))
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
            thread = Thread(target=server.serve_forever)
            thread.start()
            try:
                yield f"http://127.0.0.1:{server.server_port}", observed
            finally:
                server.shutdown()
                thread.join()

    def publication_files(self, root: Path) -> list[dict[str, object]]:
        (root / "Cargo.toml").write_text('[package]\nversion = "0.0.2"\n')
        directory = root / "dist"
        directory.mkdir()
        files = []
        for tag in (
            "macosx_11_0_arm64",
            "macosx_10_12_x86_64",
            "manylinux_2_35_aarch64",
            "manylinux_2_35_x86_64",
        ):
            filename = f"mcp_console-0.0.2-py3-none-{tag}.whl"
            payload = filename.encode()
            (directory / filename).write_bytes(payload)
            hashes = {"sha256": hashlib.sha256(payload).hexdigest()}
            files.append(
                {
                    "filename": filename,
                    "digests": hashes,
                    "hashes": hashes,
                    "yanked": False,
                }
            )
        return files

    def test_wait_pypi_waits_for_version_json_and_simple_index(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = self.publication_files(root)
            with self.publication_server(
                {
                    "/pypi/mcp-console/0.0.2/json": [
                        (404, {"message": "not visible yet"}),
                        (200, {"urls": files[:2]}),
                        (200, {"urls": files}),
                    ],
                    "/simple/mcp-console/": [
                        (200, {"files": files[:3]}),
                        (200, {"files": files}),
                    ],
                }
            ) as (url, observed):
                result = self.run_script(
                    "wait-pypi",
                    "dist",
                    "--pypi-url",
                    url,
                    "--timeout-seconds",
                    "5",
                    "--poll-interval-seconds",
                    "0.01",
                    "--report",
                    "publication.json",
                    cwd=root,
                )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(("/simple/mcp-console/", {"files": files}), observed)
            report = json.loads((root / "publication.json").read_text())
            self.assertEqual(report["version"], "0.0.2")
            self.assertEqual(
                report["wheels"],
                {item["filename"]: item["digests"]["sha256"] for item in files},
            )

    def test_wait_pypi_times_out_when_simple_index_remains_incomplete(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            files = self.publication_files(root)
            with self.publication_server(
                {
                    "/pypi/mcp-console/0.0.2/json": [(200, {"urls": files})],
                    "/simple/mcp-console/": [(200, {"files": files[:3]})],
                }
            ) as (url, observed):
                result = self.run_script(
                    "wait-pypi",
                    "dist",
                    "--pypi-url",
                    url,
                    "--timeout-seconds",
                    "0.1",
                    "--poll-interval-seconds",
                    "0.01",
                    "--report",
                    "publication.json",
                    cwd=root,
                )
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("PyPI wheel set was not visible", result.stderr)
            self.assertFalse((root / "publication.json").exists())
            self.assertIn(("/simple/mcp-console/", {"files": files[:3]}), observed)

    def test_wait_pypi_rejects_visible_hash_mismatches_and_yanked_files(self) -> None:
        for endpoint in ("version", "simple"):
            for invalid in ("hash", "yanked"):
                with (
                    self.subTest(endpoint=endpoint, invalid=invalid),
                    tempfile.TemporaryDirectory() as temporary,
                ):
                    root = Path(temporary)
                    files = self.publication_files(root)
                    corrupted = [dict(item) for item in files]
                    if invalid == "hash":
                        corrupted[0] = dict(
                            corrupted[0],
                            digests={"sha256": "wrong"},
                            hashes={"sha256": "wrong"},
                        )
                    else:
                        corrupted[0]["yanked"] = True
                    with self.publication_server(
                        {
                            "/pypi/mcp-console/0.0.2/json": [
                                (
                                    200,
                                    {
                                        "urls": corrupted
                                        if endpoint == "version"
                                        else files
                                    },
                                )
                            ],
                            "/simple/mcp-console/": [
                                (
                                    200,
                                    {
                                        "files": corrupted
                                        if endpoint == "simple"
                                        else files
                                    },
                                )
                            ],
                        }
                    ) as (url, _):
                        result = self.run_script(
                            "wait-pypi",
                            "dist",
                            "--pypi-url",
                            url,
                            "--timeout-seconds",
                            "5",
                            "--poll-interval-seconds",
                            "0.01",
                            cwd=root,
                        )
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn(
                        "yanked" if invalid == "yanked" else "hash differs",
                        result.stderr,
                    )

    def test_source_timestamp_restore_rejects_invalid_cache_before_mutation(
        self,
    ) -> None:
        for name, kind, contents in (
            ("./changed", tarfile.REGTYPE, b"cached contents"),
            ("./missing", tarfile.REGTYPE, b"contents"),
            ("../outside", tarfile.REGTYPE, b"contents"),
            ("C:/outside", tarfile.REGTYPE, b"contents"),
            ("./changed", tarfile.SYMTYPE, b""),
        ):
            with (
                self.subTest(member=name, kind=kind),
                tempfile.TemporaryDirectory() as temporary,
            ):
                root = Path(temporary)
                source = root / "source"
                source.mkdir()
                first = source / "first"
                first.write_bytes(b"same contents")
                (source / "changed").write_bytes(b"checkout contents")
                original_time = 1_600_000_100
                os.utime(first, (original_time, original_time))
                archive = root / "source.tar"
                with tarfile.open(archive, "w") as cached:
                    for filename, member_kind, payload in (
                        ("./first", tarfile.REGTYPE, b"same contents"),
                        (name, kind, contents),
                    ):
                        member = tarfile.TarInfo(filename)
                        member.type = member_kind
                        member.size = len(payload)
                        member.mtime = 1_600_000_000
                        member.linkname = "wrong target"
                        cached.addfile(member, io.BytesIO(payload))
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts/restore-source-timestamps"),
                        str(archive),
                        str(source),
                    ],
                    capture_output=True,
                    text=True,
                )
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertIn(name.removeprefix("./"), result.stderr)
                self.assertEqual(first.stat().st_mtime, original_time)
                self.assertEqual(first.read_bytes(), b"same contents")
                self.assertEqual(
                    (source / "changed").read_bytes(), b"checkout contents"
                )
                self.assertFalse((root / "outside").exists())

    @unittest.skipUnless(os.name == "posix", "Native symlink timestamp support")
    def test_source_timestamp_restore_preserves_symlinks_without_following_them(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            outside = root / "outside"
            outside.write_bytes(b"external contents")
            original_time = 1_600_000_100
            os.utime(outside, (original_time, original_time))
            link = source / "link"
            link.symlink_to("../outside")
            archive = root / "source.tar"
            for kind in (tarfile.SYMTYPE, tarfile.REGTYPE):
                with self.subTest(kind=kind):
                    with tarfile.open(archive, "w") as cached:
                        member = tarfile.TarInfo("./link")
                        member.type = kind
                        member.linkname = "../outside"
                        member.mtime = 1_600_000_000
                        payload = b"../outside" if kind == tarfile.REGTYPE else b""
                        member.size = len(payload)
                        cached.addfile(member, io.BytesIO(payload))
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(ROOT / "scripts/restore-source-timestamps"),
                            str(archive),
                            str(source),
                        ],
                        capture_output=True,
                        text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(os.readlink(link), "../outside")
                    self.assertEqual(link.lstat().st_mtime, 1_600_000_000)
                    self.assertEqual(outside.stat().st_mtime, original_time)
                    self.assertEqual(outside.read_bytes(), b"external contents")

    @unittest.skipUnless(os.name == "nt", "Native Windows CI archive restoration")
    def test_windows_source_cache_preserves_git_symlink_checkout(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text(encoding="utf-8")
        step = workflow.split(
            "      - name: Prepare native Windows sandbox source archive\n", 1
        )[1].split("      - name:", 1)[0]
        lines = step.split("        run: |\n", 1)[1].splitlines()
        program = "\n".join(line[10:] for line in lines)
        with tempfile.TemporaryDirectory(prefix="source cache ") as temporary:
            root = Path(temporary)
            source = root / ".sandbox-runner-source"
            source.mkdir()
            shutil.copytree(ROOT / "scripts", root / "scripts")

            def git(*arguments: str, **options):
                return subprocess.run(
                    ["git", "-C", str(source), *arguments],
                    check=True,
                    capture_output=True,
                    **options,
                ).stdout

            git("init", "--quiet")
            git("config", "core.symlinks", "false")
            git("config", "core.autocrlf", "false")
            git("config", "user.name", "Fixture")
            git("config", "user.email", "fixture@example.invalid")
            (source / "COPYING").write_bytes(b"fixture license\n")
            git("add", "COPYING")
            target = (
                git("hash-object", "-w", "--stdin", input=b"COPYING").decode().strip()
            )
            git("update-index", "--add", "--cacheinfo", "120000", target, "LICENSE")
            git("checkout-index", "--all", "--force")
            git("commit", "--quiet", "-m", "fixture")
            self.assertEqual(git("status", "--porcelain"), b"")

            archive = root / "sandbox-source.tar"
            cached_time = 1_600_000_000
            with tarfile.open(archive, "w") as cached:
                copying = tarfile.TarInfo("./COPYING")
                copying.size = len(b"fixture license\n")
                copying.mtime = cached_time
                cached.addfile(copying, io.BytesIO(b"fixture license\n"))
                license = tarfile.TarInfo("./LICENSE")
                license.type = tarfile.SYMTYPE
                license.linkname = "COPYING"
                license.mtime = cached_time
                cached.addfile(license)
            for path in (source / "COPYING", source / "LICENSE"):
                os.utime(path, (cached_time + 100, cached_time + 100))

            # CI's R setup exposes GNU tar; use Git for Windows' bundled copy.
            git_root = Path(git("--exec-path").decode().strip()).parents[2]
            tar_directory = git_root / "usr/bin"
            self.assertTrue((tar_directory / "tar.exe").is_file())
            environment = dict(
                os.environ,
                RUNNER_TEMP=str(root),
                GITHUB_OUTPUT=str(root / "output"),
                PATH=os.pathsep.join(
                    (
                        str(Path(sys.executable).parent),
                        str(tar_directory),
                        os.environ["PATH"],
                    )
                ),
            )
            for state in ("symlink archive", "cache miss", "generated archive"):
                with self.subTest(cache=state):
                    if state == "cache miss":
                        archive.unlink()
                    (root / "output").unlink(missing_ok=True)
                    result = subprocess.run(
                        ["pwsh", "-NoProfile", "-NonInteractive", "-Command", program],
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    self.assertEqual(git("status", "--porcelain"), b"")
                    self.assertFalse((source / "LICENSE").is_symlink())
                    self.assertEqual((source / "LICENSE").read_bytes(), b"COPYING")
                    for path in (source / "COPYING", source / "LICENSE"):
                        self.assertEqual(path.stat().st_mtime, cached_time)
                    self.assertEqual(
                        (root / "output").read_text().strip(),
                        "archive-sha256="
                        + hashlib.sha256(archive.read_bytes()).hexdigest(),
                    )

    def test_verify_wheel_set_requires_macos_and_linux_architectures(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            directory = Path(temporary_directory)
            arm64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_arm64.whl"
            x86_64 = directory / "mcp_console-0.0.2-py3-none-macosx_11_0_x86_64.whl"
            arm64.touch()
            x86_64.touch()
            for architecture in ("aarch64", "x86_64"):
                (
                    directory
                    / f"mcp_console-0.0.2-py3-none-manylinux_2_39_{architecture}.whl"
                ).touch()

            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertEqual(result.returncode, 0, result.stderr)

            x86_64.unlink()
            result = self.run_script("verify-wheel-set", str(directory), cwd=ROOT)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("expected exactly four wheels", result.stderr)


@unittest.skipUnless(os.name == "posix", "Unix Rscript executable fixture")
class RuntimeSourceValidationTests(unittest.TestCase):
    def test_r_home_selects_the_syntax_checker_without_r_on_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(ROOT / "src", root / "src")
            (root / "scripts").mkdir()
            script = root / "scripts/validate_runtime_sources.py"
            shutil.copyfile(ROOT / "scripts/validate_runtime_sources.py", script)
            shutil.copyfile(
                ROOT / "scripts/checkout_workflow.py",
                root / "scripts/checkout_workflow.py",
            )
            r_home = root / "selected-R"
            (r_home / "bin").mkdir(parents=True)
            write_python_executable(
                r_home / "bin/Rscript",
                # fmt: python
                f"""
                import sys

                assert sys.executable == {sys.executable!r}
                assert sys.argv[1:3] == ["--vanilla", "-e"]
                print("selected R syntax checker rejected source", file=sys.stderr)
                raise SystemExit(1)
                """,
            )
            environment = {**os.environ, "PATH": str(root), "R_HOME": str(r_home)}
            result = subprocess.run(
                [sys.executable, str(script)],
                env=environment,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertIn(
                "src/python/bridge.R: selected R syntax checker rejected source",
                result.stderr,
            )
            self.assertNotIn("checks skipped", result.stderr)


if __name__ == "__main__":
    unittest.main()
