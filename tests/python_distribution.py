"""Exercise distribution preparation through its public command, without Rust."""

import base64
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path

from support.normalization import code
from support.python import python_script_header

ROOT = Path(__file__).resolve().parent.parent
TARGETS = {
    "aarch64-apple-darwin": "macosx_11_0_arm64",
    "x86_64-apple-darwin": "macosx_10_12_x86_64",
    "aarch64-unknown-linux-gnu": "manylinux_2_35_aarch64",
    "x86_64-unknown-linux-gnu": "manylinux_2_35_x86_64",
    "x86_64-pc-windows-msvc": "win_amd64",
}
FLOORS = {
    "aarch64-apple-darwin": {"minimum_macos": "11.0"},
    "x86_64-apple-darwin": {"minimum_macos": "10.12"},
    "aarch64-unknown-linux-gnu": {"minimum_glibc": "2.35"},
    "x86_64-unknown-linux-gnu": {"minimum_glibc": "2.35"},
    "x86_64-pc-windows-msvc": {},
}
NATIVE = (
    python_script_header()
    + code("""
        import json
        import sys

        print(json.dumps(sys.argv[1:]))
        """)
).encode()
ENTRY_POINTS = (
    "[console_scripts]\n"
    "mcp-console = mcp_console._launcher:main\n"
    "mcp-console-install = mcp_console._binary:main\n"
)


class PythonDistributionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.version = "0.0.8"
        self.package = {
            "mcp_console/" + path.relative_to(ROOT / "python/mcp_console").as_posix(): (
                path.read_bytes()
            )
            for path in sorted((ROOT / "python/mcp_console").rglob("*"))
            if path.is_file() and path.suffix != ".pyc"
        }
        self.wheels = self.root / "native-wheels"
        self.wheels.mkdir()
        self.output = self.root / "prepared"
        for target in TARGETS:
            self.write_wheel(target)

    def metadata(self, version=None):
        return (
            "Metadata-Version: 2.4\n"
            "Name: mcp-console\n"
            f"Version: {version or self.version}\n"
            "Provides-Extra: client\n"
            "\n"
            "Fixture description.\n"
        )

    def wheel(self, target):
        (path,) = self.wheels.glob(f"*-{TARGETS[target]}.whl")
        return path

    def write_wheel(self, target, version=None):
        version = version or self.version
        tag = TARGETS[target]
        windows = "windows" in target
        # A Windows checkout gives the README and license CRLF line endings.
        newline = "\r\n" if windows else "\n"
        files = {
            "libexec/mcp-console.exe" if windows else "bin/mcp-console": NATIVE,
            "libexec/mcp-console-sandbox"
            + (".exe" if windows else ""): b"fixture companion\n",
            "share/licenses/mcp-console/LICENSE": b"fixture license\n",
            "share/licenses/mcp-console/NOTICE": b"fixture notice\n",
        }
        if "linux" in target:
            files |= {
                "libexec/bwrap": b"fixture bwrap\n",
                "share/licenses/mcp-console/bubblewrap-COPYING": b"fixture LGPL\n",
                "share/licenses/mcp-console/bubblewrap-NOTICE": b"fixture notice\n",
                "share/licenses/mcp-console/bubblewrap-SOURCE.json": b"{}\n",
            }
        prefix = f"mcp_console-{version}.dist-info"
        path = self.wheels / f"mcp_console-{version}-py3-none-{tag}.whl"
        with zipfile.ZipFile(path, "w") as archive:
            for relative, content in files.items():
                scheme = (
                    "scripts/" + relative.removeprefix("bin/")
                    if relative.startswith("bin/")
                    else "data/" + relative
                )
                info = zipfile.ZipInfo(f"mcp_console-{version}.data/{scheme}")
                executable = relative.startswith(("bin/", "libexec/"))
                info.external_attr = (
                    stat.S_IFREG | (0o755 if executable else 0o644)
                ) << 16
                archive.writestr(info, content)
            for name, content in self.package.items():
                archive.writestr(name, content)
            archive.writestr(
                f"{prefix}/METADATA",
                self.metadata(version).replace("\n", newline),
            )
            archive.writestr(
                f"{prefix}/WHEEL",
                f"Wheel-Version: 1.0\nRoot-Is-Purelib: false\nTag: py3-none-{tag}\n",
            )
            archive.writestr(
                f"{prefix}/licenses/LICENSE", f"Console license{newline}"
            )
            archive.writestr(
                f"{prefix}/sboms/mcp-console.cyclonedx.json", f'"{target}"'
            )
            if windows:
                archive.writestr(
                    f"{prefix}/entry_points.txt",
                    "[console_scripts]\nmcp-console = mcp_console._launcher:main\n",
                )
            archive.writestr(f"{prefix}/RECORD", "")

    def prepare(self):
        return subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/build-python-distribution.py"),
                str(self.wheels),
                "--out",
                str(self.output),
            ],
            capture_output=True,
            text=True,
        )

    def prepared_wheel(self):
        result = self.prepare()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [path.name for path in (self.output / "pypi").iterdir()],
            [f"mcp_console-{self.version}-py3-none-any.whl"],
        )
        return self.output / f"pypi/mcp_console-{self.version}-py3-none-any.whl"

    def test_prepares_five_digest_bound_bundles_and_one_pure_wheel(self):
        wheel = self.prepared_wheel()
        prefix = f"mcp_console-{self.version}.dist-info"
        with zipfile.ZipFile(wheel) as archive:
            files = {name: archive.read(name) for name in archive.namelist()}
        manifest = json.loads(files.pop("mcp_console/_artifacts.json"))
        record = files.pop(f"{prefix}/RECORD").decode().splitlines()
        self.assertEqual(
            sorted(record),
            sorted(
                [
                    f"{prefix}/RECORD,,",
                    "mcp_console/_artifacts.json,sha256="
                    + record_digest(json_bytes(manifest))
                    + f",{len(json_bytes(manifest))}",
                    *(
                        f"{name},sha256={record_digest(content)},{len(content)}"
                        for name, content in files.items()
                    ),
                ]
            ),
        )
        self.assertEqual(
            files,
            self.package
            | {
                f"{prefix}/METADATA": self.metadata().encode(),
                f"{prefix}/WHEEL": (
                    b"Wheel-Version: 1.0\n"
                    b"Generator: build-python-distribution\n"
                    b"Root-Is-Purelib: true\n"
                    b"Tag: py3-none-any\n"
                ),
                f"{prefix}/entry_points.txt": ENTRY_POINTS.encode(),
                f"{prefix}/licenses/LICENSE": b"Console license\n",
            },
        )
        self.assertEqual(manifest["version"], self.version)
        self.assertEqual(set(manifest["artifacts"]), set(TARGETS))
        self.assertEqual(
            sorted(path.name for path in (self.output / "native").iterdir()),
            sorted(f"mcp-console-{self.version}-{target}.zip" for target in TARGETS),
        )
        for target, artifact in manifest["artifacts"].items():
            with self.subTest(target=target):
                path = self.output / f"native/mcp-console-{self.version}-{target}.zip"
                self.assertEqual(
                    artifact,
                    {
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "size": path.stat().st_size,
                    }
                    | FLOORS[target],
                )
                windows = "windows" in target
                native = "libexec/mcp-console.exe" if windows else "bin/mcp-console"
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    self.assertEqual(archive.read(native), NATIVE)
                    self.assertTrue(archive.getinfo(native).external_attr >> 16 & 0o111)
                    self.assertEqual(
                        archive.read("share/licenses/mcp-console/LICENSE"),
                        b"fixture license\n",
                    )
                    self.assertEqual(
                        archive.read("share/licenses/mcp-console/console-LICENSE"),
                        b"Console license\n",
                    )
                    self.assertEqual(
                        archive.read("share/sboms/mcp-console.cyclonedx.json"),
                        f'"{target}"'.encode(),
                    )
                self.assertEqual(names, sorted(names))
                self.assertFalse(
                    any(
                        ".data/" in name or ".dist-info/" in name or "mcp_console/" in name
                        for name in names
                    )
                )

    def test_output_does_not_depend_on_input_entry_order(self):
        original = self.prepared_wheel().read_bytes()
        shutil.rmtree(self.output)
        for wheel in self.wheels.iterdir():
            with zipfile.ZipFile(wheel) as archive:
                contents = [(info, archive.read(info)) for info in archive.infolist()]
            with zipfile.ZipFile(wheel, "w") as archive:
                for info, content in reversed(contents):
                    archive.writestr(info, content)
        self.assertEqual(self.prepared_wheel().read_bytes(), original)

    def test_requires_all_five_platforms(self):
        self.wheel("x86_64-pc-windows-msvc").unlink()
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("expected exactly five wheels", result.stderr)
        self.assertFalse(self.output.exists())

    def test_rejects_wheels_of_different_versions(self):
        self.wheel("x86_64-pc-windows-msvc").unlink()
        self.write_wheel("x86_64-pc-windows-msvc", "0.0.9")
        result = self.prepare()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("version", result.stderr)
        self.assertFalse(self.output.exists())

    def test_rejects_members_it_cannot_relocate_identically(self):
        wheel = self.wheel("x86_64-unknown-linux-gnu")
        original = wheel.read_bytes()
        for name in (
            "mcp_console/extra.so",
            "mcp_console.libs/library.so",
            f"mcp_console-{self.version}.data/platlib/extra.so",
            f"mcp_console-{self.version}.data/purelib/extra.py",
        ):
            with self.subTest(name=name):
                wheel.write_bytes(original)
                with zipfile.ZipFile(wheel, "a") as archive:
                    archive.writestr(name, b"\x7fELFfixture")
                result = self.prepare()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(name, result.stderr)
                self.assertFalse(self.output.exists())

    def test_rejects_missing_companion_and_preserves_existing_output(self):
        wheel = self.wheel("x86_64-pc-windows-msvc")
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

    @unittest.skipUnless(shutil.which("uv"), "uv installs the prepared wheel")
    def test_prepared_wheel_installs_offline_and_runs_its_pinned_bundle(self):
        wheel = self.prepared_wheel()
        machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(
            platform.machine().lower(), platform.machine().lower()
        )
        system = {"darwin": "apple-darwin", "win32": "pc-windows-msvc"}.get(
            sys.platform, "unknown-linux-gnu"
        )
        archive = (
            self.output / f"native/mcp-console-{self.version}-{machine}-{system}.zip"
        )
        environment = self.root / "environment"
        cache = self.root / "cache"
        env = os.environ | {
            "MCP_CONSOLE_BINARY_CACHE": str(cache),
            "MCP_CONSOLE_OFFLINE": "1",
        }
        env.pop("MCP_CONSOLE_BINARY", None)
        for command in (
            ["uv", "venv", "--quiet", "--python", sys.executable, environment],
            [
                *("uv", "pip", "install", "--quiet", "--offline", "--no-index"),
                *("--no-deps", "--python", environment, wheel),
            ],
        ):
            result = subprocess.run(command, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        scripts = environment / ("Scripts" if sys.platform == "win32" else "bin")
        result = subprocess.run(
            [scripts / "mcp-console-install", "--archive", archive],
            env=env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(result.stdout.strip()).is_relative_to(cache))
        # The native fixture is a POSIX script; Windows cannot execute it.
        if sys.platform != "win32":
            result = subprocess.run(
                [scripts / "mcp-console", "serve", "--flag"],
                env=env,
                capture_output=True,
                text=True,
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout), ["serve", "--flag"])


def json_bytes(manifest):
    return (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode()


def record_digest(content):
    digest = hashlib.sha256(content).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


if __name__ == "__main__":
    unittest.main()
