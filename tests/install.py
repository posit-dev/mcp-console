"""Check source and wheel installations using one shared Cargo target directory."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from email.parser import BytesParser
from pathlib import Path

from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from checkout_workflow import checkout_owner


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "requires macOS or Linux")
class InstallationTests(unittest.TestCase):
    def test_uv_reinstall_rebuilds_companion_when_native_flags_change(self) -> None:
        with tempfile.TemporaryDirectory(
            prefix="mcp-console-native-install-"
        ) as temporary:
            directory = Path(temporary)
            source = directory / "source"
            (source / "scripts").mkdir(parents=True)
            (source / "src").mkdir()
            for name in (
                "pyproject.toml",
                "README.md",
                "LICENSE",
                "build.rs",
                "scripts/checkout_workflow.py",
                "scripts/checkout_windows.py",
                "scripts/build_backend.py",
            ):
                shutil.copyfile(ROOT / name, source / name)
            shutil.copytree(ROOT / "python", source / "python")
            shutil.copyfile(
                ROOT / "scripts/stage-sandbox-runner",
                source / "scripts/stage-sandbox-runner",
            )
            data = source / "wheel-data/data"
            data.mkdir(parents=True)
            (data / ".gitignore").write_text("/*\n!/.gitignore\n")
            (source / "Cargo.toml").write_text(
                code("""
                    [package]
                    name = "mcp-console"
                    version = "0.0.3"
                    edition = "2024"
                    [build-dependencies]
                    cc = "1"
                    embed-resource = "3"
                    serde_json = "1"
                    sha2 = "0.11"
                    """)
            )
            shutil.copyfile(ROOT / "Cargo.lock", source / "Cargo.lock")
            (source / "src/main.rs").write_text(
                code("""
                    fn main() {
                        println!("console {}", env!("RUSTUP_TOOLCHAIN"));
                        let executable = std::env::current_exe().unwrap().canonicalize().unwrap();
                        let runner = executable.parent().unwrap().parent().unwrap()
                            .join("libexec/mcp-console-sandbox");
                        let status = std::process::Command::new(runner).status().unwrap();
                        assert!(status.success());
                    }
                    """)
            )
            # Exercise the build script with empty native sources at their real paths.
            for native_source in (ROOT / "src").rglob("*.c"):
                destination = source / native_source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            runner_source = directory / "runner"
            workspace = runner_source / "codex-rs"
            workspace.mkdir(parents=True)
            runner_toolchain = subprocess.check_output(
                ["rustup", "show", "active-toolchain"], text=True
            ).split()[0]
            # The caller selects the available compiler by path while the
            # runner checkout selects its named toolchain. This avoids
            # downloading a second compiler just for this fixture.
            console_toolchain = subprocess.check_output(
                ["rustc", "--print", "sysroot"], text=True
            ).strip()
            (workspace / "rust-toolchain.toml").write_text(
                f"[toolchain]\nchannel = {json.dumps(runner_toolchain)}\n"
            )
            (runner_source / ".gitignore").write_text("/codex-rs/target\n")
            (workspace / "Cargo.toml").write_text(
                code("""
                    [workspace]
                    members = ["mcp-console-sandbox", "bwrap"]
                    resolver = "2"
                    """)
            )
            (workspace / ".cargo").mkdir()
            (workspace / ".cargo/config.toml").touch()
            for package, binary in (
                ("mcp-console-sandbox", "mcp-console-sandbox"),
                ("codex-bwrap", "bwrap"),
            ):
                crate = workspace / binary
                (crate / "src").mkdir(parents=True)
                (crate / "Cargo.toml").write_text(
                    code(f"""
                        [package]
                        name = "{package}"
                        version = "0.1.0"
                        edition = "2024"
                        [[bin]]
                        name = "{binary}"
                        path = "src/main.rs"
                        [build-dependencies]
                        cc = "1"
                        """)
                )
                (crate / "build.rs").write_text(
                    code("""
                        fn main() {
                            let mut build = cc::Build::new();
                            build.file("value.c");
                            let libcap = std::env::var("CARGO_CFG_TARGET_OS").unwrap() == "linux"
                                && std::env::var("CARGO_PKG_NAME").unwrap() == "codex-bwrap";
                            if libcap {
                                build.define("LINK_LIBCAP", None);
                            }
                            build.compile("value");
                            if libcap {
                                println!("cargo:rustc-link-lib=cap");
                            }
                        }
                        """)
                )
                (crate / "value.c").write_text(
                    code("""
                        #ifdef LINK_LIBCAP
                        extern void *cap_get_proc(void);
                        extern int cap_free(void *);
                        #endif
                        int value(void) {
                        #ifdef LINK_LIBCAP
                            cap_free(cap_get_proc());
                        #endif
                            return FIXTURE_VALUE;
                        }
                        """)
                )
                (crate / "src/main.rs").write_text(
                    code("""
                        unsafe extern "C" { fn value() -> i32; }
                        fn main() {
                            println!("{} {}", unsafe { value() }, env!("RUSTUP_TOOLCHAIN"));
                        }
                        """)
                )
            for name in ("LICENSE", "NOTICE"):
                (runner_source / name).write_text(name)
            (workspace / "vendor/bubblewrap").mkdir(parents=True)
            (workspace / "vendor/bubblewrap/COPYING").write_text("fixture license")
            (workspace / "vendor/bubblewrap/bubblewrap.c").write_text(
                "/* Copyright fixture author; SPDX-License-Identifier: LGPL-2.0-or-later */\n"
            )
            subprocess.run(
                ["cargo", "generate-lockfile", "--offline"],
                cwd=workspace,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["cargo", "generate-lockfile", "--offline"],
                cwd=source,
                check=True,
                capture_output=True,
            )
            subprocess.run(["git", "init", "--quiet", str(runner_source)], check=True)
            subprocess.run(["git", "add", "."], cwd=runner_source, check=True)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "commit",
                    "--quiet",
                    "--no-gpg-sign",
                    "-m",
                    "Runner fixture",
                ],
                cwd=runner_source,
                check=True,
            )
            pin = {
                "repository": "fixture/runner",
                "commit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=runner_source, text=True
                ).strip(),
            }
            (source / "sandbox-runner.json").write_text(json.dumps(pin))
            environment = os.environ.copy()
            environment.pop("MCP_CONSOLE_SANDBOX_SOURCE", None)
            environment.update(
                {
                    "UV_TOOL_DIR": str(directory / "tools"),
                    "UV_TOOL_BIN_DIR": str(directory / "bin"),
                    "CARGO_TARGET_DIR": str(source / "target"),
                    "XDG_CACHE_HOME": str(directory / "cache"),
                    "RUSTUP_TOOLCHAIN": console_toolchain,
                    "GIT_CONFIG_COUNT": "1",
                    "GIT_CONFIG_KEY_0": f"url.{runner_source.as_uri()}.insteadOf",
                    "GIT_CONFIG_VALUE_0": "https://github.com/fixture/runner.git",
                }
            )
            for editable, value in ((True, 42), (True, 84), (False, 126), (False, 168)):
                with self.subTest(editable=editable, value=value):
                    result = subprocess.run(
                        [
                            "uv",
                            "tool",
                            "install",
                            "--reinstall",
                            *(["--editable"] if editable else []),
                            ".",
                        ],
                        cwd=source,
                        env=environment | {"CFLAGS": f"-DFIXTURE_VALUE={value}"},
                        check=False,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    self.assertEqual(
                        result.returncode, 0, result.stdout + result.stderr
                    )
                    result = subprocess.run(
                        [directory / "bin/mcp-console"],
                        capture_output=True,
                        text=True,
                        check=True,
                    )
                    self.assertEqual(
                        result.stdout,
                        f"console {console_toolchain}\n{value} {runner_toolchain}\n",
                    )

            # The same backend and staging recipe must be present when uv
            # receives a source archive instead of a working checkout.
            result = subprocess.run(
                ["uv", "build", "--sdist", "--out-dir", str(directory / "dist")],
                cwd=source,
                env=environment,
                check=False,
                capture_output=True,
                text=True,
                timeout=60,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            archives = list((directory / "dist").glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            result = subprocess.run(
                ["uv", "tool", "install", "--reinstall", str(archives[0])],
                cwd=source,
                env=environment | {"CFLAGS": "-DFIXTURE_VALUE=210"},
                check=False,
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(
                subprocess.check_output([directory / "bin/mcp-console"], text=True),
                f"console {console_toolchain}\n210 {runner_toolchain}\n",
            )

    def test_uv_installs_a_relocatable_bundle_from_unstaged_sources(self) -> None:
        # Hiding the build artifacts below uses a rename on this filesystem.
        with tempfile.TemporaryDirectory(
            prefix="mcp-console-install-", dir=ROOT.parent
        ) as temporary:
            directory = Path(temporary)
            source = directory / "source"
            target = ROOT / "target"
            environment = os.environ.copy()
            # Automatic staging shares the caller's pinned source/build cache.
            # An explicit source override is retained for CI and release builds.
            environment |= {
                "CARGO_TARGET_DIR": str(target),
                "UV_TOOL_DIR": str(directory / "uv-tools"),
                "UV_TOOL_BIN_DIR": str(directory / "uv-bin"),
            }
            tracked = (
                subprocess.check_output(
                    [
                        "git",
                        "ls-files",
                        "--cached",
                        "--others",
                        "--exclude-standard",
                        "-z",
                    ],
                    cwd=ROOT,
                )
                .decode()
                .split("\0")
            )
            for name in filter(None, tracked):
                if not (ROOT / name).is_file():
                    continue
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(ROOT / name, destination)

            private_files = (
                "libexec/mcp-console-sandbox",
                "share/licenses/mcp-console/LICENSE",
                "share/licenses/mcp-console/NOTICE",
                *(
                    (
                        "libexec/bwrap",
                        "share/licenses/mcp-console/bubblewrap-COPYING",
                        "share/licenses/mcp-console/bubblewrap-NOTICE",
                        "share/licenses/mcp-console/bubblewrap-SOURCE.json",
                    )
                    if sys.platform == "linux"
                    else ()
                ),
            )

            def stage_stale_wheel_data() -> None:
                stale = (
                    "libexec/obsolete-runner",
                    "share/licenses/mcp-console/Codex-LICENSE",
                    "share/licenses/mcp-console/Codex-NOTICE",
                )
                if sys.platform == "linux":
                    stale += private_files
                for relative in stale:
                    destination = source / "wheel-data/data" / relative
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(b"stale macOS wheel data\n")

            for editable in (True, False):
                if not editable:
                    for relative in ("libexec", "share"):
                        shutil.rmtree(source / "wheel-data/data" / relative)
                if sys.platform == "linux":
                    stage_stale_wheel_data()
                result = subprocess.run(
                    [
                        "uv",
                        "tool",
                        "install",
                        "--reinstall",
                        *(["--editable"] if editable else []),
                        ".",
                    ],
                    cwd=source,
                    env=environment,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                print(result.stdout, flush=True)
                result = subprocess.run(
                    [
                        directory / "uv-bin/mcp-console",
                        "sandbox",
                        "--",
                        "/usr/bin/true",
                    ],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            for remove_companions in (True, False):
                # Check both missing companions and stale additions after Cargo
                # has cached a build with all current companions still present.
                if remove_companions:
                    for relative in ("libexec", "share"):
                        shutil.rmtree(source / "wheel-data/data" / relative)
                stage_stale_wheel_data()
                # Reuse the source and target directory for wheel construction.
                result = subprocess.run(
                    [
                        "uv",
                        "build",
                        "--wheel",
                        "--config-setting",
                        "maturin.build-args=--compatibility pypi",
                        "--out-dir",
                        str(directory / "dist"),
                    ],
                    cwd=source,
                    env=environment,
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=1800,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                print(result.stdout, flush=True)
                wheels = list((directory / "dist").glob("*.whl"))
                self.assertEqual(len(wheels), 1)
                with zipfile.ZipFile(wheels[0]) as archive:
                    metadata_path = next(
                        name
                        for name in archive.namelist()
                        if name.endswith(".dist-info/METADATA")
                    )
                    metadata = BytesParser().parsebytes(archive.read(metadata_path))
                    self.assertEqual(metadata["Requires-Python"], ">=3.11")
                    actual = sorted(
                        name.split(".data/data/", 1)[1]
                        for name in archive.namelist()
                        if ".data/data/" in name
                    )
                self.assertEqual(actual, sorted(private_files))
            result = subprocess.run(
                ["uv", "build", "--sdist", "--out-dir", str(directory / "dist")],
                cwd=source,
                env=environment,
                check=False,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=120,
            )
            self.assertEqual(result.returncode, 0, result.stdout)
            archives = list((directory / "dist").glob("*.tar.gz"))
            self.assertEqual(len(archives), 1)
            with tarfile.open(archives[0]) as archive:
                wheel_data = [
                    name.split("/", 1)[1]
                    for name in archive.getnames()
                    if "/wheel-data/" in name
                ]
            self.assertEqual(wheel_data, ["wheel-data/data/.gitignore"])
            bundle = directory / "relocated"
            (bundle / "bin").mkdir(parents=True)
            binary = bundle / "bin/mcp-console"
            shutil.copy2(target / "release/mcp-console", binary)
            for relative in ("libexec", "share/licenses/mcp-console"):
                shutil.copytree(target / relative, bundle / relative)
            shutil.rmtree(source)
            # Make every compiled-in build path unavailable during runtime checks.
            hidden = directory / "build-artifacts"
            target.rename(hidden)
            try:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(ROOT / "scripts" / "release.py"),
                        "smoke-wheel",
                        str(wheels[0]),
                        str(binary),
                    ],
                    cwd=ROOT,
                    env=environment
                    | {
                        "UV_TOOL_DIR": str(directory / "wheel-tools"),
                        "UV_TOOL_BIN_DIR": str(directory / "wheel-bin"),
                    },
                    check=False,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=600,
                )
                self.assertEqual(result.returncode, 0, result.stdout)
                print(result.stdout, flush=True)
                for python_version in ("3.11", "3.14"):
                    with self.subTest(python=python_version):
                        venv = directory / f"python-{python_version}"
                        python = str(venv / "bin/python")
                        for command in (
                            [
                                "uv",
                                "venv",
                                str(venv),
                                "--python",
                                python_version,
                            ],
                            [
                                "uv",
                                "pip",
                                "install",
                                "--python",
                                python,
                                f"{wheels[0]}[client,anthropic,chatlas,openai,openai-agents,codex]",
                            ],
                            [
                                python,
                                str(ROOT / "tests/fixtures/python_api_smoke.py"),
                                sys.executable,
                            ],
                        ):
                            result = subprocess.run(
                                command,
                                cwd=directory,
                                env=environment | {"UV_PYTHON_DOWNLOADS": "automatic"},
                                check=False,
                                text=True,
                                stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT,
                                timeout=600,
                            )
                            self.assertEqual(result.returncode, 0, result.stdout)
                            print(result.stdout, flush=True)
                for installed in (
                    binary,
                    directory / "uv-bin" / "mcp-console",
                    directory / "wheel-bin" / "mcp-console",
                ):
                    with self.subTest(installer=installed):
                        result = subprocess.run(
                            [
                                sys.executable,
                                str(ROOT / "tests" / "sandbox_installation.py"),
                                str(installed),
                            ],
                            check=False,
                            capture_output=True,
                            text=True,
                            timeout=180,
                        )
                        self.assertEqual(
                            result.returncode, 0, result.stdout + result.stderr
                        )
            finally:
                hidden.rename(target)


if __name__ == "__main__":
    with checkout_owner(ROOT):
        unittest.main()
