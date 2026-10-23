#!/usr/bin/env python3
"""Exercise sandbox source reuse and ownership through the staging command."""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.capture import read_lines
from support.checkpoints import FifoCheckpoint
from support.normalization import code

ROOT = Path(__file__).resolve().parent.parent


class StagingTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name).resolve()
        self.source = self.directory / "upstream"
        workspace = self.source / "codex-rs"
        (workspace / "mcp-console-sandbox").mkdir(parents=True)
        (workspace / "Cargo.toml").touch()
        (workspace / "mcp-console-sandbox/Cargo.toml").touch()
        (workspace / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "fixture"\n'
        )
        (self.source / ".gitignore").write_text("/codex-rs/target/\n")
        for name in ("LICENSE", "NOTICE"):
            (self.source / name).write_text(name)
        subprocess.run(["git", "init", "-q", self.source], check=True)
        subprocess.run(
            ["git", "config", "maintenance.auto", "false"],
            cwd=self.source,
            check=True,
        )
        self.commit()
        self.pin = {
            "repository": "fixture/runner",
            "commit": self.revision(),
        }
        self.roots = [self.directory / name for name in ("first", "second")]
        for root in self.roots:
            (root / "scripts").mkdir(parents=True)
            for name in (
                "scripts/stage-sandbox-runner",
                "scripts/checkout_workflow.py",
            ):
                shutil.copyfile(ROOT / name, root / name)
            (root / "sandbox-runner.json").write_text(json.dumps(self.pin))
        commands = self.directory / "commands"
        commands.mkdir()
        self.write_command(
            commands / "rustup",
            # fmt: python
            r"""
            import json
            import os
            import sys
            from pathlib import Path

            assert sys.argv[1:5] == ["run", "--install", "fixture", "cargo"]
            output = Path(os.environ["CARGO_TARGET_DIR"])
            with Path(os.environ["BUILD_RECORD"]).open("a") as stream:
                stream.write(json.dumps(str(output)) + "\n")
            if os.environ.get("BUILD_REACHED"):
                with open(os.environ["BUILD_REACHED"], "wb", buffering=0) as stream:
                    stream.write(b"1")
                with open(os.environ["BUILD_RELEASE"], "rb", buffering=0) as stream:
                    assert stream.read(1) == b"1"
            binary = output / "aarch64-apple-darwin/release/mcp-console-sandbox"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_text(os.environ.get("RUSTFLAGS", "initial"))
            """,
        )
        self.write_command(commands / "xcrun", "pass\n")
        self.environment = os.environ | {
            "PATH": str(commands) + os.pathsep + os.environ["PATH"],
            "XDG_CACHE_HOME": str(self.directory / "cache"),
            "BUILD_RECORD": str(self.directory / "builds.jsonl"),
            "RUSTFLAGS": "initial",
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"url.{self.source.as_uri()}.insteadOf",
            "GIT_CONFIG_VALUE_0": "https://github.com/fixture/runner.git",
        }
        self.environment.pop("MCP_CONSOLE_SANDBOX_SOURCE", None)

    def write_command(self, path: Path, program: str) -> None:
        path.write_text(f"#!{sys.executable}\n" + code(program))
        path.chmod(0o755)

    def commit(self) -> None:
        subprocess.run(["git", "add", "."], cwd=self.source, check=True)
        subprocess.run(
            [
                "git",
                "-c",
                "user.name=Fixture",
                "-c",
                "user.email=fixture@example.invalid",
                "commit",
                "-qm",
                "Fixture",
                "--no-gpg-sign",
            ],
            cwd=self.source,
            check=True,
        )

    def revision(self) -> str:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=self.source, text=True
        ).strip()

    def command(self, root: Path, *arguments: str) -> list[str]:
        return [sys.executable, str(root / "scripts/stage-sandbox-runner"), *arguments]

    def stage(self, root: Path, **environment: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            self.command(root, "--target", "aarch64-apple-darwin"),
            env=self.environment | environment,
            capture_output=True,
            text=True,
            timeout=10,
        )

    def describe(self, root: Path, **environment: str) -> dict:
        result = subprocess.run(
            self.command(root, "--describe"),
            env=self.environment | environment,
            capture_output=True,
            text=True,
            check=True,
        )
        return json.loads(result.stdout)

    def test_separate_checkouts_reuse_the_pinned_source_and_cargo_output(self) -> None:
        first, second = self.roots
        descriptions = [self.describe(root) for root in self.roots]
        source = Path(descriptions[0]["source_checkout"])
        self.assertEqual(
            descriptions[0]["source_checkout"], descriptions[1]["source_checkout"]
        )
        self.assertTrue(source.is_relative_to(self.directory / "cache"))
        self.assertFalse(source.exists(), "description must not prepare the cache")
        result = self.stage(first)
        self.assertEqual(result.returncode, 0, result.stderr)
        target = Path(descriptions[0]["build_directory"])
        marker = target / "retained-build-data"
        marker.write_text("keep")
        result = self.stage(second, RUSTFLAGS="changed inputs")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(marker.read_text(), "keep")
        builds = [
            json.loads(line)
            for line in (self.directory / "builds.jsonl").read_text().splitlines()
        ]
        self.assertEqual(builds, [str(target), str(target)])
        self.assertEqual(
            (first / "wheel-data/data/libexec/mcp-console-sandbox").read_text(),
            "initial",
        )
        self.assertEqual(
            (second / "wheel-data/data/libexec/mcp-console-sandbox").read_text(),
            "changed inputs",
        )
        (self.source / "NOTICE").write_text("new revision")
        self.commit()
        (second / "sandbox-runner.json").write_text(
            json.dumps(self.pin | {"commit": self.revision()})
        )
        changed = self.describe(second)
        self.assertNotEqual(changed["source_checkout"], str(source))
        result = self.stage(second)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            (second / "wheel-data/data/share/licenses/mcp-console/NOTICE").read_text(),
            "new revision",
        )
        self.assertEqual(marker.read_text(), "keep")

    def test_source_ownership_covers_build_and_staging_across_checkouts(self) -> None:
        first, second = self.roots
        reached = FifoCheckpoint.create(self.directory / "reached")
        release = FifoCheckpoint.create(self.directory / "release")
        self.addCleanup(reached.close)
        self.addCleanup(release.close)
        process = subprocess.Popen(
            self.command(first, "--target", "aarch64-apple-darwin"),
            env=self.environment
            | {"BUILD_REACHED": str(reached.path), "BUILD_RELEASE": str(release.path)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        second_process = None
        try:
            reached.wait("first checkout owns the shared source")
            source = self.describe(first)["source_checkout"]
            # The explicit override must use the same lock as automatic selection.
            second_process = subprocess.Popen(
                self.command(second, "--target", "aarch64-apple-darwin"),
                env=self.environment | {"MCP_CONSOLE_SANDBOX_SOURCE": source},
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            assert second_process.stderr is not None
            self.assertIn(
                "waiting for sandbox source",
                read_lines(second_process.stderr, 1, "shared source wait")[0],
            )
            self.assertFalse((second / "wheel-data").exists())
        finally:
            release.release()
            stdout, stderr = process.communicate(timeout=10)
            if second_process is not None:
                second_stdout, second_stderr = second_process.communicate(timeout=10)
        self.assertEqual(process.returncode, 0, stdout + stderr)
        self.assertEqual(second_process.returncode, 0, second_stdout + second_stderr)
        self.assertEqual(
            (second / "wheel-data/data/libexec/mcp-console-sandbox").read_text(),
            "initial",
        )

    def test_relative_cache_path_is_rejected_before_preparation(self) -> None:
        result = self.stage(self.roots[0], XDG_CACHE_HOME="relative")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("XDG_CACHE_HOME must be an absolute path", result.stderr)
        self.assertFalse((self.directory / "builds.jsonl").exists())


if __name__ == "__main__":
    unittest.main()
