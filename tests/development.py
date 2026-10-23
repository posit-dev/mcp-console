#!/usr/bin/env python3
"""Public command tests for local development reports."""

import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from support.normalization import code
from support.requirements import command, gnu_tar

ROOT = Path(__file__).resolve().parent.parent


class DevelopmentTests(unittest.TestCase):
    def test_ci_rebuilds_checkout_sources_without_rebuilding_dependencies(self) -> None:
        if not command("cargo").available:
            self.skipTest("Cargo is required for the CI build-cache regression")
        self.write(
            "Cargo.toml",
            """[package]
name = "mcp-console"
version = "0.0.0"
edition = "2021"

[dependencies]
cache-fixture = { path = "dependency" }
""",
        )
        self.write(
            "dependency/Cargo.toml",
            """[package]
name = "cache-fixture"
version = "0.0.0"
edition = "2021"
""",
        )
        self.write("dependency/src/lib.rs", 'pub const VALUE: &str = "dependency";\n')
        source = 'fn main() { println!("{} cached", cache_fixture::VALUE); }\n'
        self.write("src/main.rs", source)
        for name in (
            "with-checkout",
            "with-checkout.cmd",
            "checkout_workflow.py",
            "checkout_windows.py",
        ):
            target = self.root / "scripts" / name
            target.parent.mkdir(exist_ok=True)
            shutil.copy2(ROOT / "scripts" / name, target)

        def run(*arguments: str) -> str:
            return subprocess.check_output(
                arguments,
                cwd=self.root,
                text=True,
                stderr=subprocess.STDOUT,
                timeout=60,
            )

        build = ("cargo", "build", "--offline", "--target-dir", "target")
        executable = (
            self.root
            / "target/debug"
            / ("mcp-console.exe" if os.name == "nt" else "mcp-console")
        )
        run(*build)
        self.assertEqual(run(str(executable)), "dependency cached\n")
        dependency = next(
            (self.root / "target/debug/deps").glob("libcache_fixture-*.rlib")
        )
        dependency_time = dependency.stat().st_mtime_ns
        modified = (self.root / "src/main.rs").stat().st_mtime_ns
        self.write("src/main.rs", source.replace("cached", "source"))
        # A cache restored after checkout can contain newer compiler fingerprints.
        os.utime(self.root / "src/main.rs", ns=(modified, modified))
        run(*build)
        self.assertEqual(run(str(executable)), "dependency cached\n")

        workflow = (ROOT / ".github/workflows/ci.yaml").read_text()
        job = "windows" if os.name == "nt" else "check"
        section = workflow.split(f"  {job}:\n", 1)[1]

        def cache_paths(name: str) -> set[Path]:
            step = section.split(f"      - name: {name}\n", 1)[1].split(
                "\n      - ", 1
            )[0]
            paths = step.split("          path: |\n", 1)[1].split(
                "\n          key:", 1
            )[0]
            return {
                Path(path.strip())
                for path in paths.splitlines()
                if path.strip().startswith("target/")
            }

        restore = (
            "Restore Windows build and dependency caches"
            if os.name == "nt"
            else "Restore Rust build data"
        )
        save = "Save Windows caches" if os.name == "nt" else "Save Rust build data"
        cached_target = self.root / "cached-target"
        (self.root / "target").rename(cached_target)
        # Restore only what CI saves, including Cargo's target-directory marker.
        for path in cache_paths(save) & cache_paths(restore):
            source = cached_target / path.relative_to("target")
            destination = self.root / path
            if source.is_dir():
                shutil.copytree(source, destination)
            elif source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
        step = section.partition(
            "      - name: Discard restored Console build artifacts\n"
        )[2]
        if step:
            program = (
                step.split("\n      - ", 1)[0].split("        run: ", 1)[1].strip()
            )
            arguments = shlex.split(program)
            arguments[0] = str(Path(arguments[0]))
            subprocess.run(
                arguments, shell=os.name == "nt", cwd=self.root, check=True, timeout=60
            )
        run(*build)
        self.assertEqual(run(str(executable)), "dependency source\n")
        self.assertEqual(dependency.stat().st_mtime_ns, dependency_time)

    @unittest.skipUnless(os.name == "nt", "native Windows CI archive paths")
    def test_windows_ci_source_archive_round_trip(self) -> None:
        for requirement in (command("pwsh"), gnu_tar()):
            if not requirement.available:
                self.skipTest(requirement.reason)
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text()
        step = workflow.split(
            "      - name: Prepare native Windows sandbox source archive\n", 1
        )[1].split("\n      - name:", 1)[0]
        program = code(step.split("        run: |\n", 1)[1])
        scripts = self.root / "scripts"
        scripts.mkdir()
        shutil.copy2(
            ROOT / "scripts/restore-source-timestamps",
            scripts / "restore-source-timestamps",
        )
        source = self.root / ".sandbox-runner-source"
        source.mkdir()
        subprocess.run(["git", "init", "-q", source], check=True)
        tracked = source / "codex-rs/main.rs"
        tracked.parent.mkdir()
        tracked.write_text("fn main() {}\n")
        (source / ".gitignore").write_text("codex-rs/target/\n")
        for arguments in (
            ["add", "."],
            [
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.org",
                "-c",
                "commit.gpgsign=false",
                "commit",
                "-qm",
                "Fixture",
            ],
        ):
            subprocess.run(["git", "-C", source, *arguments], check=True)
        target = source / "codex-rs/target"
        target.mkdir()
        (target / "build-output").write_text("excluded build output")
        temporary = self.root / "runner temp"
        temporary.mkdir()
        archive = temporary / "sandbox-source.tar"
        self.assertTrue(archive.drive)
        output = temporary / "output"
        environment = os.environ | {
            "RUNNER_TEMP": str(temporary),
            "GITHUB_OUTPUT": str(output),
            "MCP_CONSOLE_HOME": str(self.root / "home"),
        }
        timestamp = 1_600_000_000
        os.utime(tracked, (timestamp, timestamp))
        for cache in ("miss", "hit"):
            self.assertEqual(archive.exists(), cache == "hit")
            if cache == "hit":
                os.utime(tracked, (timestamp + 1, timestamp + 1))
            result = subprocess.run(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "$ErrorActionPreference = 'Stop'\n" + program,
                ],
                cwd=self.root,
                env=environment,
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(
                result.returncode, 0, f"cache {cache}: {result.stdout}{result.stderr}"
            )
            self.assertEqual(tracked.read_text(), "fn main() {}\n")
            self.assertEqual(tracked.stat().st_mtime, timestamp)
            with tarfile.open(archive) as contents:
                names = contents.getnames()
            self.assertIn("./codex-rs/main.rs", names)
            self.assertFalse(any(name.startswith("./.git/") for name in names))
            self.assertNotIn("./.git", names)
            self.assertFalse(
                any(name.startswith("./codex-rs/target") for name in names)
            )
        digests = output.read_text().splitlines()
        self.assertEqual(len(digests), 2)
        self.assertEqual(digests[0], digests[1])
        self.assertRegex(digests[0], r"^archive-sha256=[0-9a-f]{64}$")

    def test_windows_ci_reuses_complete_sandbox_builds(self) -> None:
        workflow = (ROOT / ".github/workflows/ci.yaml").read_text()
        windows = workflow.split("  windows:\n", 1)[1].split("\n  check:", 1)[0]
        self.assertIn(
            "      MCP_CONSOLE_SANDBOX_SOURCE: ${{ github.workspace }}/.sandbox-runner-source",
            windows,
        )

        def step(name):
            return windows.split(f"      - name: {name}\n", 1)[1].split(
                "\n      - ", 1
            )[0]

        outputs = step("Restore native Windows sandbox outputs")
        build = step("Restore native Windows sandbox build intermediates")
        downloads = step("Restore Windows Cargo downloads")
        for path in ("~/.cargo/registry", "~/.cargo/git"):
            self.assertIn(path, downloads)
            self.assertIn(path, step("Save Windows Cargo downloads"))
            self.assertNotIn(path, step("Restore Windows build and dependency caches"))
        for path in ("wheel-data/data", "target/sandbox-runner-build.json"):
            self.assertIn(path, outputs)
            self.assertIn(path, step("Save native Windows sandbox outputs"))
        self.assertNotIn("restore-keys:", outputs)
        self.assertIn(".sandbox-runner-source/codex-rs/target", build)
        self.assertIn("restore-keys:", build)
        self.assertIn("steps.sandbox-source.outputs.archive-sha256", build)
        for cache in (outputs, build):
            self.assertNotIn("steps.rust.outputs.cachekey", cache)
            for identity in (
                "CI_BUILD_CACHE_VERSION",
                "steps.epoch.outputs.week",
                "steps.epoch.outputs.image",
                "runner.os",
                "runner.arch",
                "x86_64-pc-windows-msvc",
                ".sandbox-runner-source/codex-rs/rust-toolchain.toml",
            ):
                self.assertIn(identity, cache)
        for input in (
            "sandbox-runner.json",
            "scripts/stage-sandbox-runner",
            "build.rs",
        ):
            self.assertIn(input, outputs)
        staging = step("Stage native Windows sandbox")
        for cache in ("sandbox-cache", "sandbox-build-cache"):
            self.assertIn(f"steps.{cache}.outputs.cache-hit != 'true'", staging)
        validation = step("Validate native Windows sandbox artifacts")
        self.assertIn(
            "scripts/with-checkout.cmd cargo build --target-dir target", validation
        )
        self.assertIn("LastWriteTimeUtc", validation)
        for name in (
            "Save native Windows sandbox source timestamps",
            "Save native Windows sandbox outputs",
            "Save native Windows sandbox build intermediates",
            "Save Windows Cargo downloads",
        ):
            saving = step(name)
            self.assertNotIn("always()", saving)
            self.assertNotIn("!cancelled()", saving)
            self.assertLess(
                windows.index("Validate native Windows sandbox artifacts"),
                windows.index(name),
            )
            self.assertLess(
                windows.index(name),
                windows.index("Probe MXC and run Windows sandbox contracts"),
            )
        self.assertIn("scripts/check.cmd --full", windows)
        source = step("Prepare native Windows sandbox source archive")
        self.assertIn("--exclude=./.git", source)
        self.assertIn("--exclude=./codex-rs/target", source)
        self.assertIn(
            "persist-credentials: false",
            step("Check out native Windows sandbox source"),
        )

    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.git("init", "-q")
        self.git("config", "user.name", "Test")
        self.git("config", "user.email", "test@example.org")

    def git(self, *arguments: str) -> str:
        return subprocess.check_output(
            ["git", *arguments], cwd=self.root, text=True
        ).strip()

    def write(self, name: str, source: str) -> None:
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source)

    def commit(self) -> str:
        self.git("add", ".")
        self.git("commit", "-qm", "Fixture")
        return self.git("rev-parse", "HEAD")

    def command(
        self,
        name: str,
        *arguments: str,
        script_root: Path = ROOT,
        environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, script_root / "scripts" / name, *arguments],
            cwd=self.root,
            env=environment,
            capture_output=True,
            text=True,
            timeout=15,
        )

    def test_review_diff_uses_intended_base_and_separates_generated_changes(
        self,
    ) -> None:
        self.write("src/lib.rs", "first\n")
        first = self.commit()
        self.write("src/lib.rs", "first\nlower layer\n")
        parent = self.commit()
        self.write(
            "src/lib.rs",
            """first
lower layer
new behavior
""",
        )
        self.write("tests/snapshots/case.yaml", "result: 42\nstatus: done\n")
        self.write("tests/case.py", "assert True\n")
        self.write("docs/change.md", "Explanation\n")
        self.write("scripts/a tool", "command\n")
        (self.root / "tests/binary").write_bytes(b"\0binary")
        self.git("add", ".")
        self.write("scratch.txt", "not staged\n")
        result = self.command("review-diff", parent, "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["base"], parent)
        self.assertEqual(report["groups"]["production"]["added"], 1)
        self.assertEqual(report["groups"]["snapshots"]["added"], 2)
        self.assertEqual(report["groups"]["tests"]["binary_files"], 1)
        self.assertEqual(report["groups"]["tooling"]["files"], 1)
        self.assertEqual(report["groups"]["documentation"]["added"], 1)
        self.assertEqual(report["untracked"], ["scratch.txt"])
        wider = self.command("review-diff", first, "--json")
        self.assertEqual(json.loads(wider.stdout)["groups"]["production"]["added"], 2)
        human = self.command("review-diff", parent)
        self.assertEqual(human.returncode, 0, human.stderr)
        self.assertIn("snapshots", human.stdout)
        self.assertIn("1 untracked file(s) excluded", human.stdout)

    def test_review_diff_excludes_parent_changes_after_the_layer_forks(self) -> None:
        self.write("src/shared.rs", "shared\n")
        fork = self.commit()
        self.git("checkout", "-qb", "parent")
        self.write("src/shared.rs", "shared\nparent addition\n")
        self.write("src/parent.rs", "parent only\n")
        parent = self.commit()
        self.git("checkout", "-qb", "layer", fork)
        self.write("src/layer.rs", "committed\n")
        self.commit()
        self.write("src/layer.rs", "committed\nstaged\n")
        self.git("add", "src/layer.rs")
        self.write(
            "src/layer.rs",
            """committed
staged
unstaged
""",
        )

        result = self.command("review-diff", "parent", "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["base"], parent)
        self.assertEqual(
            report["groups"]["production"],
            {"files": 1, "added": 3, "deleted": 0, "binary_files": 0},
        )

    def test_review_diff_requires_a_valid_explicit_base(self) -> None:
        self.write("README.md", "Fixture\n")
        self.commit()
        for arguments in ((), ("missing-branch",)):
            with self.subTest(arguments=arguments):
                result = self.command("review-diff", *arguments)
                self.assertNotEqual(result.returncode, 0)
                self.assertTrue(result.stderr)

    @unittest.skipIf(
        os.name == "nt",
        "Unix capabilities; Windows preflight is covered by windows_workflow.py",
    )
    def test_preflight_inventories_artifacts_and_optional_skips_without_building(
        self,
    ) -> None:
        for name in (
            "scripts/preflight",
            "scripts/stage-sandbox-runner",
            "scripts/checkout_workflow.py",
            "sandbox-runner.json",
        ):
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / name, target)
        shutil.copytree(
            ROOT / "tests/support",
            self.root / "tests/support",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        commands = self.root / "commands"
        commands.mkdir()
        (commands / "git").symlink_to(shutil.which("git"))
        for name in ("uv", "cargo", "rustup", "R", "Rscript"):
            self.write(
                f"commands/{name}",
                f"#!{sys.executable}\n"
                # fmt: python
                + code(r"""
                    import json
                    import os
                    import sys
                    from pathlib import Path

                    name = Path(sys.argv[0]).name
                    arguments = sys.argv[1:]
                    with open("probes.jsonl", "a") as log:
                        log.write(json.dumps([name, *arguments]) + "\n")
                    if arguments == ["--version"]:
                        print(name + " fixture version")
                    elif os.environ.get("FAIL_PROBE") == name:
                        print("fixture metadata probe failed", file=sys.stderr)
                        raise SystemExit(9)
                    elif name == "uv" and arguments == ["cache", "dir"]:
                        print(os.environ["FIXTURE_CACHE"])
                    elif name == "R" and arguments == ["RHOME"]:
                        print(os.environ["FIXTURE_R_HOME"])
                    elif name == "rustup" and arguments == ["show", "active-toolchain"]:
                        print("fixture-console-toolchain (default)")
                    else:
                        raise AssertionError((name, arguments))
                    """),
            )
            (commands / name).chmod(0o755)
        environment = os.environ | {
            "PATH": str(commands),
            "HOME": str(self.root / "home"),
            "XDG_CACHE_HOME": "",
            "FIXTURE_CACHE": str(self.root / "shared-cache"),
            "FIXTURE_R_HOME": str(self.root / "R-home"),
            "R_HOME": "",
            "RETICULATE_PYTHON": "/explicit/workload/python",
            "MCP_CONSOLE_SANDBOX_SOURCE": str(self.root / "runner-source"),
        }
        self.write(
            "runner-source/codex-rs/rust-toolchain.toml",
            '[toolchain]\nchannel = "fixture-runner-toolchain"\n',
        )
        self.commit()
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["checkout"], str(self.root.resolve()))
        self.assertEqual(report["required_missing"], [])
        self.assertNotIn("preparation_needed", report)
        self.assertFalse(any(report["artifacts"].values()))
        self.assertEqual(
            report["companion"]["source_toolchain"], "fixture-runner-toolchain"
        )
        self.assertEqual(
            report["runtime"]["python_selection"], "/explicit/workload/python"
        )
        self.assertEqual(report["runtime"]["r_home"], str(self.root / "R-home"))
        self.assertEqual(report["caches"]["uv"], str(self.root / "shared-cache"))
        self.assertNotIn("host_budget", report["caches"])
        self.assertNotIn("providers", report)
        self.assertFalse((self.root / "target").exists())
        self.assertFalse((self.root / ".dev-workflow").exists())
        for name, label in (
            ("rustup", "rustup_toolchain"),
            ("uv", "uv_cache"),
            ("R", "r_home"),
        ):
            with self.subTest(failing_metadata=name):
                result = self.command(
                    "preflight",
                    "--json",
                    script_root=self.root,
                    environment=environment | {"FAIL_PROBE": name},
                )
                self.assertEqual(result.returncode, 1, result.stderr)
                report = json.loads(result.stdout)
                self.assertEqual(
                    report["probe_errors"][label], "fixture metadata probe failed"
                )
                self.assertEqual(report["required_missing"], [])
        pin = json.loads((self.root / "sandbox-runner.json").read_text())
        self.write(
            "target/sandbox-runner-build.json",
            json.dumps({"source_revision": pin["commit"], "target": "fixture-target"}),
        )
        for path in (
            "target/release/mcp-console",
            "target/libexec/mcp-console-sandbox",
            "wheel-data/data/libexec/mcp-console-sandbox",
        ):
            self.write(path, "fixture\n")
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertTrue(all(report["artifacts"].values()))
        self.assertEqual(report["companion"]["staged_target"], "fixture-target")
        self.write(
            "target/sandbox-runner-build.json",
            json.dumps({"source_revision": "0" * 40, "target": "fixture-target"}),
        )
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            json.loads(result.stdout)["companion"]["staged_revision"], "0" * 40
        )
        (commands / "cargo").write_text(
            """#!/bin/sh
echo no installed toolchain >&2
exit 9
"""
        )
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("cargo", report["required_missing"])
        self.assertIn("no installed toolchain", report["tools"]["cargo"]["error"])
        self.write(
            "commands/cargo",
            f"#!{sys.executable}\n"
            # fmt: python
            + code("""
                import signal

                signal.pause()
                """),
        )
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        report = json.loads(result.stdout)
        self.assertIn("cargo", report["required_missing"])
        self.assertIn("timed out", report["tools"]["cargo"]["error"])
        (commands / "cargo").unlink()
        result = self.command(
            "preflight", "--json", script_root=self.root, environment=environment
        )
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("cargo", json.loads(result.stdout)["required_missing"])


if __name__ == "__main__":
    unittest.main()
