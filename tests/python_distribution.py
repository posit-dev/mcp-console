"""Exercise distribution preparation through its public command, without Rust."""

import hashlib
import importlib.util
import json
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    "aarch64-apple-darwin": "macosx_11_0_arm64",
    "x86_64-apple-darwin": "macosx_11_0_x86_64",
    "aarch64-unknown-linux-gnu": "manylinux_2_35_aarch64",
    "x86_64-unknown-linux-gnu": "manylinux_2_35_x86_64",
    "x86_64-pc-windows-msvc": "win_amd64",
}


class PythonDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "source"
        (self.root / "scripts").mkdir(parents=True)
        for name in ("release.py", "build-python-distribution.py"):
            source = ROOT / "scripts" / name
            if source.exists():
                shutil.copy2(source, self.root / "scripts" / name)
        for name in ("pyproject.toml", "README.md", "LICENSE"):
            shutil.copy2(ROOT / name, self.root / name)
        self.version = "0.0.8"
        self.set_version(self.version)
        package = self.root / "python/mcp_console"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("# Client package fixture.\n")
        for name in ("_launcher.py", "_binary.py"):
            (package / name).write_text("def main():\n    return 0\n")
        self.wheels = self.root / "native-wheels"
        self.wheels.mkdir()
        self.output = self.root / "prepared"
        self.write_wheels()

    def set_version(self, version):
        (self.root / "Cargo.toml").write_text(
            f'[package]\nname = "mcp-console"\nversion = "{version}"\n'
        )

    def write_wheels(self):
        for target, platform in TARGETS.items():
            path = self.wheels / f"mcp_console-{self.version}-py3-none-{platform}.whl"
            windows = "windows" in target
            native = "libexec/mcp-console.exe" if windows else "bin/mcp-console"
            files = {
                native: b"fixture native Console\n",
                "libexec/mcp-console-sandbox"
                + (".exe" if windows else ""): b"fixture companion\n",
                "share/licenses/mcp-console/LICENSE": b"fixture license\n",
                "share/licenses/mcp-console/NOTICE": b"fixture notice\n",
            }
            if "linux" in target:
                files.update(
                    {
                        "libexec/bwrap": b"fixture bwrap\n",
                        "share/licenses/mcp-console/bubblewrap-COPYING": b"fixture LGPL\n",
                        "share/licenses/mcp-console/bubblewrap-NOTICE": b"fixture notice\n",
                        "share/licenses/mcp-console/bubblewrap-SOURCE.json": b"{}\n",
                    }
                )
            with zipfile.ZipFile(path, "w") as archive:
                for relative, content in files.items():
                    scheme = (
                        "scripts/" + relative[4:]
                        if relative.startswith("bin/")
                        else "data/" + relative
                    )
                    info = zipfile.ZipInfo(f"mcp_console-{self.version}.data/{scheme}")
                    info.external_attr = (
                        stat.S_IFREG
                        | (
                            0o755
                            if relative.startswith(("bin/", "libexec/"))
                            else 0o644
                        )
                    ) << 16
                    archive.writestr(info, content)
                archive.writestr("mcp_console/__init__.py", "# Native wheel copy.\n")
                prefix = f"mcp_console-{self.version}.dist-info"
                archive.writestr(
                    f"{prefix}/METADATA",
                    f"Metadata-Version: 2.4\nName: mcp-console\nVersion: {self.version}\n",
                )
                archive.writestr(
                    f"{prefix}/WHEEL",
                    f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-{platform}\n",
                )

    def prepare(self, *arguments):
        return subprocess.run(
            [
                sys.executable,
                str(self.root / "scripts/build-python-distribution.py"),
                str(self.wheels),
                "--out",
                str(self.output),
                *arguments,
            ],
            capture_output=True,
            text=True,
        )

    def test_prepares_five_digest_bound_bundles_and_copies_python_metadata(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        project = self.output / "python-project"
        metadata = tomllib.loads((project / "pyproject.toml").read_text())
        original = tomllib.loads((self.root / "pyproject.toml").read_text())["project"]
        expected = original | {
            "version": self.version,
            "scripts": {
                "mcp-console": "mcp_console._launcher:main",
                "mcp-console-install": "mcp_console._binary:main",
            },
        }
        expected.pop("dynamic")
        expected["classifiers"] = [
            value
            for value in expected["classifiers"]
            if not value.startswith("License ::")
        ]
        self.assertEqual(metadata["project"], expected)
        self.assertEqual(
            metadata["build-system"]["build-backend"], "setuptools.build_meta"
        )
        self.assertNotIn("maturin", metadata["tool"])
        manifest = json.loads(
            (project / "python/mcp_console/_artifacts.json").read_text()
        )
        self.assertEqual(
            (manifest["schema"], manifest["version"], manifest["release"]),
            (1, self.version, "v" + self.version),
        )
        self.assertEqual(set(manifest["artifacts"]), set(TARGETS))
        for target, artifact in manifest["artifacts"].items():
            if "apple" in target:
                self.assertEqual(artifact["minimum_macos"], "11.0")
            path = self.output / "native" / artifact["filename"]
            self.assertEqual(
                artifact["sha256"], hashlib.sha256(path.read_bytes()).hexdigest()
            )
            self.assertEqual(artifact["size"], path.stat().st_size)
            with zipfile.ZipFile(path) as archive:
                native = artifact["executable"]
                self.assertEqual(archive.read(native), b"fixture native Console\n")
                self.assertEqual(
                    archive.read("share/licenses/mcp-console/LICENSE"),
                    b"fixture license\n",
                )
                self.assertTrue(archive.getinfo(native).external_attr >> 16 & 0o111)
                self.assertFalse(
                    any(
                        ".data/" in name or ".dist-info/" in name
                        for name in archive.namelist()
                    )
                )
        self.assertEqual(
            (project / "README.md").read_bytes(), (self.root / "README.md").read_bytes()
        )
        self.assertEqual(
            (project / "LICENSE").read_bytes(), (self.root / "LICENSE").read_bytes()
        )

    def test_requires_all_five_platforms(self):
        next(self.wheels.glob("*-win_amd64.whl")).unlink()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected exactly five wheels", result.stderr)
        self.assertFalse(self.output.exists())

    def test_rejects_version_mismatch(self):
        self.set_version("0.0.9")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version", result.stderr)
        self.assertFalse(self.output.exists())

    def test_requires_explicit_development_rehearsal(self):
        for path in self.wheels.iterdir():
            path.unlink()
        self.version += "+dev"
        self.set_version(self.version)
        self.write_wheels()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("--allow-dev", result.stderr)
        self.assertEqual(self.prepare("--allow-dev").returncode, 0)

    def test_rejects_unhandled_native_libraries_and_install_schemes(self):
        wheel = next(self.wheels.glob("*-manylinux_2_35_x86_64.whl"))
        original = wheel.read_bytes()
        for name in (
            "mcp_console/extra.so",
            "mcp_console.libs/library.so",
            f"mcp_console-{self.version}.data/platlib/extra.so",
            f"mcp_console-{self.version}.data/purelib/extra.so",
        ):
            with self.subTest(name=name):
                wheel.write_bytes(original)
                with zipfile.ZipFile(wheel, "a") as archive:
                    archive.writestr(name, b"\x7fELFfixture")
                result = self.prepare()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.output.exists())

    def test_rejects_unsafe_archive_paths(self):
        wheel = next(self.wheels.glob("*-manylinux_2_35_x86_64.whl"))
        original = wheel.read_bytes()
        for name in (
            "../escape",
            ".complete",
            "libexec/file.",
            "libexec/file ",
            "libexec/../escape",
        ):
            with self.subTest(name=name):
                wheel.write_bytes(original)
                with zipfile.ZipFile(wheel, "a") as archive:
                    archive.writestr(
                        f"mcp_console-{self.version}.data/data/{name}", b"bad"
                    )
                result = self.prepare()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("unsafe", result.stderr)
        self.assertFalse((self.root / "escape").exists())

    def test_rejects_case_insensitive_duplicates(self):
        wheel = next(self.wheels.glob("*-manylinux_2_35_x86_64.whl"))
        with zipfile.ZipFile(wheel, "a") as archive:
            archive.writestr(
                f"mcp_console-{self.version}.data/data/libexec/MCP-CONSOLE-SANDBOX",
                b"duplicate",
            )
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("duplicate bundle path", result.stderr)

    def test_archive_digests_do_not_depend_on_input_entry_order(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        original = (
            self.output / "python-project/python/mcp_console/_artifacts.json"
        ).read_bytes()
        shutil.rmtree(self.output)
        for wheel in self.wheels.iterdir():
            with zipfile.ZipFile(wheel) as archive:
                contents = [(info, archive.read(info)) for info in archive.infolist()]
            with zipfile.ZipFile(wheel, "w") as archive:
                for info, content in reversed(contents):
                    archive.writestr(info, content)
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            original,
            (
                self.output / "python-project/python/mcp_console/_artifacts.json"
            ).read_bytes(),
        )

    def test_rejects_missing_companion_and_preserves_existing_output(self):
        wheel = next(self.wheels.glob("*-win_amd64.whl"))
        with zipfile.ZipFile(wheel) as archive:
            contents = [
                (info, archive.read(info))
                for info in archive.infolist()
                if not info.filename.endswith("mcp-console-sandbox.exe")
            ]
        with zipfile.ZipFile(wheel, "w") as archive:
            for info, content in contents:
                archive.writestr(info, content)
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("mcp-console-sandbox.exe", result.stderr)
        self.assertFalse(self.output.exists())
        self.output.mkdir()
        marker = self.output / "keep"
        marker.write_text("existing work")
        self.assertNotEqual(self.prepare().returncode, 0)
        self.assertEqual(marker.read_text(), "existing work")

    @unittest.skipUnless(
        importlib.util.find_spec("setuptools") and shutil.which("uv"),
        "uv and setuptools are needed to build the staged pure wheel",
    )
    def test_staged_project_builds_a_small_pure_wheel_with_manifest_and_extras(self):
        prepared = self.prepare()
        self.assertEqual(prepared.returncode, 0, prepared.stderr)
        project = self.output / "python-project"
        result = subprocess.run(
            [
                shutil.which("uv"),
                "build",
                "--wheel",
                "--no-build-isolation",
                "--python",
                sys.executable,
                "--out-dir",
                str(project / "dist"),
                str(project),
            ],
            cwd=project,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        (wheel,) = tuple((project / "dist").glob("*-py3-none-any.whl"))
        self.assertLess(wheel.stat().st_size, 100_000)
        with zipfile.ZipFile(wheel) as archive:
            self.assertIn("mcp_console/_artifacts.json", archive.namelist())
            entry_points = archive.read(
                f"mcp_console-{self.version}.dist-info/entry_points.txt"
            ).decode()
            self.assertIn("mcp-console = mcp_console._launcher:main", entry_points)
            self.assertIn(
                "mcp-console-install = mcp_console._binary:main", entry_points
            )
            metadata = archive.read(
                f"mcp_console-{self.version}.dist-info/METADATA"
            ).decode()
            for extra in (
                "client",
                "anthropic",
                "chatlas",
                "openai",
                "openai-agents",
                "codex",
            ):
                self.assertIn("Provides-Extra: " + extra, metadata)
            self.assertIn(
                "Root-Is-Purelib: true",
                archive.read(f"mcp_console-{self.version}.dist-info/WHEEL").decode(),
            )


if __name__ == "__main__":
    unittest.main()
