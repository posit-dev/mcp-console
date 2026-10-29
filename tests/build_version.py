"""Check development provenance through Cargo builds with the real build script."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest

ROOT = Path(__file__).resolve().parent.parent


class BuildVersionTests(unittest.TestCase):
    def test_versions_follow_git_without_source_changes(self) -> None:
        with tempfile.TemporaryDirectory(prefix="mcp-console-version-") as temporary:
            directory = Path(temporary)
            source = directory / "source"
            source.mkdir()
            for name in (
                "build.rs",
                "scripts/with-checkout",
                "scripts/checkout_workflow.py",
                "scripts/checkout_windows.py",
                "src/windows/console.manifest",
                "src/windows/console.rc",
            ):
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(ROOT / name, destination)
            for name in ("src/r_graphics.c", "src/r_repl.c", "src/worker/interrupt.c"):
                destination = source / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            (source / "src/main.rs").write_text(
                'fn main() { println!("{}", env!("MCP_CONSOLE_VERSION")); }\n'
            )
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            manifest = (
                '[package]\nname = "version-fixture"\nversion = "0.1.0+dev"\n'
                'edition = "2024"\n[build-dependencies]\n'
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            (source / "Cargo.toml").write_text(manifest.replace("0.1.0+dev", "0.1.0"))
            shutil.copyfile(ROOT / "Cargo.lock", source / "Cargo.lock")
            # The build script validates the real staged bundle; the fixture
            # executable only reports its build version and never starts it.
            self.stage(source)
            self.assertEqual(self.build(source), "0.1.0")
            # Establish companion outputs before checking that development
            # provenance leaves unchanged builds fresh.
            (source / "Cargo.toml").write_text(manifest)
            self.assertEqual(self.build(source), "0.1.0+dev")
            self.assertEqual(self.build(source, fresh=True), "0.1.0+dev")

            (source / ".gitignore").write_text("/target\n/.dev-workflow\n/wheel-data\n")
            self.git(source, "init", "--initial-branch=main")
            self.git(source, "add", ".")
            first = self.commit(source, "Initial fixture")
            # Turning an already-built archive into a checkout needs an explicit
            # rebuild; absent Git metadata is not a watched build input.
            (source / "build.rs").touch()
            self.assertEqual(self.build(source), f"0.1.0+dev.g{first[:12]}")
            self.assertEqual(self.build(source, fresh=True), f"0.1.0+dev.g{first[:12]}")

            second = self.commit(source, "Advance branch without source changes")
            self.assertEqual(self.build(source), f"0.1.0+dev.g{second[:12]}")
            self.git(source, "pack-refs", "--all", "--prune")
            self.assertEqual(self.build(source), f"0.1.0+dev.g{second[:12]}")
            self.assertEqual(
                self.build(source, fresh=True), f"0.1.0+dev.g{second[:12]}"
            )
            third = self.commit(source, "Advance packed branch")
            self.assertEqual(self.build(source), f"0.1.0+dev.g{third[:12]}")

            self.git(source, "checkout", "--detach", first)
            self.assertEqual(self.build(source), f"0.1.0+dev.g{first[:12]}")
            self.git(source, "checkout", "--detach", second)
            self.assertEqual(self.build(source), f"0.1.0+dev.g{second[:12]}")

            worktree = directory / "worktree"
            self.git(source, "worktree", "add", "-b", "fixture", str(worktree), third)
            self.stage(worktree)
            self.assertEqual(self.build(worktree), f"0.1.0+dev.g{third[:12]}")
            fourth = self.commit(worktree, "Advance worktree branch")
            self.assertEqual(self.build(worktree), f"0.1.0+dev.g{fourth[:12]}")

            (source / "Cargo.toml").write_text(manifest.replace("0.1.0+dev", "0.1.1"))
            self.assertEqual(self.build(source), "0.1.1")

    def stage(self, source: Path) -> None:
        (source / "target").mkdir(exist_ok=True)
        shutil.copyfile(ROOT / "sandbox-runner.json", source / "sandbox-runner.json")
        shutil.copyfile(
            ROOT / "target/sandbox-runner-build.json",
            source / "target/sandbox-runner-build.json",
        )
        shutil.copytree(ROOT / "wheel-data", source / "wheel-data")

    def build(self, source: Path, *, fresh: bool | None = None) -> str:
        target = source.parent / "build"
        result = subprocess.run(
            [
                sys.executable,
                str(source / "scripts/with-checkout"),
                "cargo",
                "build",
                "--message-format=json",
                "--target-dir",
                str(target),
            ],
            cwd=source,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        if fresh is not None:
            artifacts = [json.loads(line) for line in result.stdout.splitlines()]
            (binary,) = (
                artifact
                for artifact in artifacts
                if artifact["reason"] == "compiler-artifact"
                and artifact["target"]["name"] == "version-fixture"
            )
            self.assertEqual(binary["fresh"], fresh, result.stdout + result.stderr)
        suffix = ".exe" if os.name == "nt" else ""
        return subprocess.check_output(
            [target / f"debug/version-fixture{suffix}"], text=True
        ).strip()

    def git(self, source: Path, *arguments: str) -> str:
        return subprocess.check_output(
            [
                "git",
                "-c",
                "user.name=Version fixture",
                "-c",
                "user.email=version-fixture@example.invalid",
                "-c",
                "commit.gpgsign=false",
                *arguments,
            ],
            cwd=source,
            text=True,
            stderr=subprocess.PIPE,
        ).strip()

    def commit(self, source: Path, message: str) -> str:
        self.git(source, "commit", "--allow-empty", "-m", message)
        return self.git(source, "rev-parse", "HEAD")


if __name__ == "__main__":
    unittest.main()
