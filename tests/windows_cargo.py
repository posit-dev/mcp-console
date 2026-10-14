"""Public Cargo build acceptance for Windows companion updates."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "native Windows packaging")
class WindowsCargo(unittest.TestCase):
    def test_staging_accepts_long_source_paths(self):
        with tempfile.TemporaryDirectory(prefix="console staging ") as temporary:
            root = Path(temporary)
            checkout = root / ("source-" + "a" * 45)
            checkout.mkdir()
            files = {
                ".gitattributes": "* text eol=lf\n",
                ".gitignore": "codex-rs/target/\n",
                "LICENSE": "fixture license\n",
                "NOTICE": "fixture notice\n",
                "codex-rs/Cargo.toml": """[workspace]
members = ["mcp-console-sandbox"]
resolver = "3"
""",
                "codex-rs/mcp-console-sandbox/Cargo.toml": """[package]
name = "mcp-console-sandbox"
version = "0.0.0"
edition = "2024"
[[bin]]
name = "mcp-console-sandbox"
path = "main.rs"
""",
                "codex-rs/mcp-console-sandbox/main.rs": "fn main() {}\n",
            }
            channel = subprocess.check_output(
                ["rustup", "show", "active-toolchain"], text=True
            ).split()[0]
            files["codex-rs/rust-toolchain.toml"] = (
                f"[toolchain]\nchannel = {json.dumps(channel)}\n"
            )
            for relative, contents in files.items():
                path = checkout / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(contents, newline="\n")
            snapshot = (
                checkout
                / "codex-rs/snapshots"
                / ("long-snapshot-" + "a" * 140 + ".snap")
            )
            self.assertGreater(len(str(snapshot)), 260)
            snapshot.parent.mkdir()
            snapshot.write_text("tracked fixture\n", newline="\n")
            subprocess.run(
                ["cargo", "generate-lockfile", "--offline"],
                cwd=checkout / "codex-rs",
                check=True,
                capture_output=True,
                timeout=60,
            )
            git = ["git", "-C", str(checkout), "-c", "core.longpaths=true"]
            for arguments in (
                ["init"],
                ["add", "."],
                [
                    "-c",
                    "user.name=Fixture",
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "commit.gpgsign=false",
                    "commit",
                    "-m",
                    "fixture",
                ],
                ["config", "core.longpaths", "false"],
            ):
                subprocess.run([*git, *arguments], check=True, capture_output=True)
            revision = subprocess.check_output(
                [*git, "rev-parse", "HEAD"], text=True
            ).strip()
            (root / "scripts").mkdir()
            for source in (
                "scripts/stage-sandbox-runner",
                "scripts/checkout_workflow.py",
                "scripts/checkout_windows.py",
            ):
                shutil.copyfile(ROOT / source, root / source)
            (root / "sandbox-runner.json").write_text(
                json.dumps(
                    {
                        "repository": "fixture/runner",
                        "release": "fixture",
                        "commit": revision,
                    }
                )
            )
            libexec = root / "wheel-data/data/libexec"
            libexec.mkdir(parents=True)
            retired = (
                "mcp-console-sandbox-setup.exe",
                "mcp-console-sandbox-runner.exe",
            )
            for name in retired:
                (libexec / name).write_bytes(b"obsolete bundle member")
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "scripts/stage-sandbox-runner"),
                    str(checkout),
                ],
                cwd=root,
                env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            manifest = json.loads(
                (root / "target/sandbox-runner-build.json").read_text()
            )
            self.assertEqual(manifest["source_revision"], revision)
            self.assertEqual(
                {path.name for path in libexec.iterdir()}, {"mcp-console-sandbox.exe"}
            )

            staged = [root / "target/sandbox-runner-build.json"] + [
                path for path in (root / "wheel-data/data").rglob("*") if path.is_file()
            ]
            for path in staged:
                os.utime(path, ns=(1_600_000_000_000_000_000,) * 2)
            timestamps = {path: path.stat().st_mtime_ns for path in staged}
            repeated = subprocess.run(
                result.args,
                cwd=root,
                env=dict(os.environ, MCP_CONSOLE_HOME=str(root / "home")),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(repeated.returncode, 0, repeated.stderr)
            self.assertEqual(
                {path: path.stat().st_mtime_ns for path in staged}, timestamps
            )

            # Rehearse CI's separate source-mtime and Cargo-target restores at
            # the same path as a fresh checkout; neither archive contains .git.
            source_archive = root / "source.tar"
            build_archive = root / "build.tar"
            for archive, extra in (
                (
                    source_archive,
                    ["--exclude=./.git", "--exclude=./codex-rs/target", "."],
                ),
                (build_archive, ["codex-rs/target"]),
            ):
                subprocess.run(
                    ["tar", "-cf", str(archive), "-C", str(checkout), *extra],
                    check=True,
                    capture_output=True,
                )
            build_outputs = list((checkout / "codex-rs/target").rglob("*.exe"))
            previous = checkout.with_name("previous-source")
            checkout.rename(previous)
            subprocess.run(
                [
                    "git",
                    "-c",
                    "core.longpaths=true",
                    "clone",
                    str(previous),
                    str(checkout),
                ],
                check=True,
                capture_output=True,
            )
            for archive in (source_archive, build_archive):
                subprocess.run(
                    ["tar", "-xf", str(archive), "-C", str(checkout)],
                    check=True,
                    capture_output=True,
                )
            # Tar can round timestamps; require Cargo to retain the restored
            # executables, rather than requiring nanosecond archive precision.
            build_times = {path: path.stat().st_mtime_ns for path in build_outputs}
            restored = subprocess.run(
                result.args[:2],
                cwd=root,
                env=dict(
                    os.environ,
                    MCP_CONSOLE_HOME=str(root / "home"),
                    MCP_CONSOLE_SANDBOX_SOURCE=str(checkout),
                ),
                capture_output=True,
                text=True,
                timeout=180,
            )
            self.assertEqual(restored.returncode, 0, restored.stderr)
            self.assertNotIn("Compiling", restored.stderr)
            self.assertEqual(
                {path: path.stat().st_mtime_ns for path in build_outputs}, build_times
            )
            self.assertEqual(
                {path: path.stat().st_mtime_ns for path in staged}, timestamps
            )

    def test_rebuild_preserves_running_companion(self):
        with tempfile.TemporaryDirectory(prefix="console cargo ") as temporary:
            root = Path(temporary)
            (root / "src/windows").mkdir(parents=True)
            (root / "src/main.rs").write_text("fn main() {}\n")
            for source in (ROOT / "src").rglob("*.c"):
                destination = root / source.relative_to(ROOT)
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.touch()
            for source in (
                "build.rs",
                "src/windows/console.rc",
                "src/windows/console.manifest",
            ):
                shutil.copyfile(ROOT / source, root / source)
            dependencies = tomllib.loads((ROOT / "Cargo.toml").read_text())[
                "build-dependencies"
            ]
            (root / "Cargo.toml").write_text(
                '[package]\nname = "windows-companion-build"\nversion = "0.0.0"\n'
                'edition = "2024"\n[build-dependencies]\n'
                + "".join(
                    f"{name} = {json.dumps(version)}\n"
                    for name, version in dependencies.items()
                )
            )
            shutil.copyfile(ROOT / "Cargo.lock", root / "Cargo.lock")
            helper_source = root / "helper.rs"
            helper_source.write_text("""fn main() {
    println!("ready");
    std::io::stdin().read_line(&mut String::new()).unwrap();
}
""")
            helper = root / "helper.exe"
            subprocess.run(
                ["rustc", "--edition=2024", str(helper_source), "-o", str(helper)],
                check=True,
                capture_output=True,
                timeout=120,
            )
            target = subprocess.check_output(
                ["rustc", "--print", "host-tuple"], text=True
            ).strip()
            pin = json.loads((ROOT / "sandbox-runner.json").read_text())
            (root / "sandbox-runner.json").write_text(json.dumps(pin))
            (root / "target").mkdir()
            environment = dict(os.environ, MCP_CONSOLE_HOME=str(root / "home"))
            arguments = [
                "cargo",
                "build",
                "--target",
                target,
                "--target-dir",
                str(root / "target"),
            ]
            artifacts = {
                "mcp-console-sandbox.exe": helper.read_bytes(),
                "LICENSE": b"license",
                "NOTICE": b"notice",
            }

            def stage():
                for name, contents in artifacts.items():
                    directory = (
                        "libexec"
                        if name.endswith(".exe")
                        else "share/licenses/mcp-console"
                    )
                    path = root / "wheel-data/data" / directory / name
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(contents)
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

            def build(*extra):
                result = subprocess.run(
                    [*arguments, *extra],
                    cwd=root,
                    env=environment,
                    capture_output=True,
                    text=True,
                    timeout=180,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                return result

            stage()
            build()
            name = "mcp-console-sandbox.exe"
            installed = list((root / "target" / target).rglob(name))
            self.assertEqual(len(installed), 1, installed)
            previous = installed[0]
            original = previous.read_bytes()
            with subprocess.Popen(
                [str(previous)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            ) as running:
                try:
                    self.assertEqual(running.stdout.readline().strip(), "ready")
                    artifacts[name] += b"updated companion"
                    stage()
                    build()
                    self.assertIsNone(running.poll(), "build retired the active helper")
                    self.assertEqual(previous.read_bytes(), original)
                    installed = list((root / "target" / target).rglob(name))
                    self.assertEqual(len(installed), 2, installed)
                    replacement = next(path for path in installed if path != previous)
                    self.assertEqual(replacement.read_bytes(), artifacts[name])
                    # Observe newly installed files, then require an unchanged
                    # build to reuse the executable while the old helper lives.
                    build()
                    result = build("--message-format=json")
                    executables = [
                        event
                        for line in result.stdout.splitlines()
                        if (event := json.loads(line)).get("executable")
                    ]
                    self.assertEqual(len(executables), 1, result.stdout)
                    self.assertTrue(executables[0]["fresh"], result.stderr)
                finally:
                    running.communicate("exit\n", timeout=10)
                self.assertEqual(running.returncode, 0)

            # The workflow forces this existing public Cargo validation before
            # saving or using exact cache hits, including incomplete restores.
            record = root / "target/sandbox-runner-build.json"
            for invalid in ("pin", "target", "digest", "json", "record", *artifacts):
                with self.subTest(invalid=invalid):
                    stage()
                    manifest = json.loads(record.read_text())
                    if invalid in ("pin", "target"):
                        field = "source_revision" if invalid == "pin" else "target"
                        manifest[field] = "incompatible"
                        record.write_text(json.dumps(manifest))
                    elif invalid == "digest":
                        manifest["artifacts"][name] = "0" * 64
                        record.write_text(json.dumps(manifest))
                    elif invalid == "json":
                        record.write_text("incomplete JSON")
                    elif invalid == "record":
                        record.unlink()
                    else:
                        directory = (
                            "libexec"
                            if invalid.endswith(".exe")
                            else "share/licenses/mcp-console"
                        )
                        (root / "wheel-data/data" / directory / invalid).unlink()
                    # Match CI's touch: a cached Console build must revalidate.
                    if record.exists():
                        os.utime(record, None)
                    rejected = subprocess.run(
                        arguments,
                        cwd=root,
                        env=environment,
                        capture_output=True,
                        text=True,
                        timeout=180,
                    )
                    self.assertNotEqual(rejected.returncode, 0, rejected.stderr)
                    self.assertIn("failed to run custom build command", rejected.stderr)


if __name__ == "__main__":
    unittest.main()
