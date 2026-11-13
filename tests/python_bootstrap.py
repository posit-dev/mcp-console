"""Exercise the Python installer and launcher through their process boundaries.

The executables are deterministic fixtures for installation and forwarding only.
They do not model Console, its sandbox, or its native runtime compatibility.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
import warnings
import zipfile

from support.normalization import code
from support.python import python_script_header


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(
    sys.platform in {"darwin", "linux"},
    "executable fixtures use POSIX scripts; Windows requires native launcher acceptance",
)
class PythonBootstrapTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory(prefix="mcp-console-bootstrap-")
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.package_root = self.directory / "python"
        self.package = self.package_root / "mcp_console"
        shutil.copytree(ROOT / "python/mcp_console", self.package)
        self.cache = self.directory / "cache"
        self.env = os.environ | {
            "PYTHONPATH": str(self.package_root),
            "PYTHONNOUSERSITE": "1",
            "MCP_CONSOLE_HOME": str(self.directory / "console-home"),
            "MCP_CONSOLE_BINARY_CACHE": str(self.cache),
            "MCP_CONSOLE_OFFLINE": "1",
        }
        self.env.pop("MCP_CONSOLE_BINARY", None)
        machine = {"arm64": "aarch64", "amd64": "x86_64"}.get(
            platform.machine().lower(), platform.machine().lower()
        )
        suffix = "apple-darwin" if sys.platform == "darwin" else "unknown-linux-gnu"
        self.target = f"{machine}-{suffix}"
        self.version = "0.0.8"
        self.executable = (
            python_script_header()
            + code("""
                import json
                import os
                import sys

                print(json.dumps({
                    "args": sys.argv[1:],
                    "stdin": sys.stdin.read(),
                    "pid": os.getpid(),
                }))
                print("native fixture stderr", file=sys.stderr)
                raise SystemExit(23 if "--exit-23" in sys.argv else 0)
                """)
        ).encode()
        self.entries = [
            ("bin/mcp-console", self.executable, stat.S_IFREG | 0o755),
            ("libexec/mcp-console-sandbox", b"companion fixture", stat.S_IFREG | 0o755),
            (
                "share/licenses/mcp-console/LICENSE",
                b"license fixture\n",
                stat.S_IFREG | 0o644,
            ),
            (
                "share/licenses/mcp-console/NOTICE",
                b"notice fixture\n",
                stat.S_IFREG | 0o644,
            ),
        ]
        if sys.platform == "linux":
            self.entries += [
                ("libexec/bwrap", b"bwrap fixture", stat.S_IFREG | 0o755),
                (
                    "share/licenses/mcp-console/bubblewrap-COPYING",
                    b"bwrap license\n",
                    stat.S_IFREG | 0o644,
                ),
                (
                    "share/licenses/mcp-console/bubblewrap-NOTICE",
                    b"bwrap notice\n",
                    stat.S_IFREG | 0o644,
                ),
                (
                    "share/licenses/mcp-console/bubblewrap-SOURCE.json",
                    b"{}\n",
                    stat.S_IFREG | 0o644,
                ),
            ]
        self.archive = self.directory / "native.zip"
        self.make_archive()

    def make_archive(self, entries=None) -> None:
        with warnings.catch_warnings():
            warnings.filterwarnings("ignore", message="Duplicate name:")
            with zipfile.ZipFile(self.archive, "w", zipfile.ZIP_DEFLATED) as archive:
                for name, content, mode in self.entries if entries is None else entries:
                    info = zipfile.ZipInfo(name)
                    info.create_system = 3
                    info.external_attr = mode << 16
                    archive.writestr(info, content)
        self.write_manifest()

    def write_manifest(self) -> None:
        content = self.archive.read_bytes()
        self.manifest = {
            "schema": 1,
            "version": self.version,
            "release": f"v{self.version}",
            "artifacts": {
                self.target: {
                    "filename": f"mcp-console-{self.version}-{self.target}.zip",
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                    "executable": "bin/mcp-console",
                }
            },
        }
        self.save_manifest()
        metadata = self.package_root / f"mcp_console-{self.version}.dist-info"
        metadata.mkdir(exist_ok=True)
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.3\nName: mcp-console\nVersion: {self.version}\n"
        )

    def save_manifest(self) -> None:
        (self.package / "_artifacts.json").write_text(json.dumps(self.manifest))

    def command(self, *args: str, module: str = "_binary") -> list[str]:
        return [sys.executable, "-m", f"mcp_console.{module}", *map(str, args)]

    def run_command(self, *args: str, module: str = "_binary", **kwargs):
        return subprocess.run(
            self.command(*args, module=module),
            cwd=self.directory,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
            **kwargs,
        )

    def install(self) -> Path:
        result = self.run_command("--archive", str(self.archive))
        self.assertEqual(result.returncode, 0, result.stderr)
        native = Path(result.stdout.strip())
        self.assertTrue(native.is_file(), result.stdout)
        return native

    def test_installs_complete_bundle_from_pinned_local_archive(self) -> None:
        native = self.install()
        prefix = native.parent.parent
        for relative, content, mode in self.entries:
            installed = prefix / relative
            self.assertEqual(installed.read_bytes(), content)
            self.assertEqual(stat.S_IMODE(installed.stat().st_mode), stat.S_IMODE(mode))
        self.assertTrue(native.is_relative_to(self.cache))

    def test_print_path_never_installs(self) -> None:
        result = self.run_command("--print-path")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.cache.exists())

    def test_warm_offline_install_reuses_cache(self) -> None:
        native = self.install()
        original_mtime = native.stat().st_mtime_ns
        self.archive.unlink()
        for arguments in ((), ("--print-path",)):
            with self.subTest(arguments=arguments):
                result = self.run_command(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(Path(result.stdout.strip()), native)
                self.assertEqual(native.stat().st_mtime_ns, original_mtime)

    def test_offline_missing_bundle_reports_actionable_error(self) -> None:
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("offline", result.stderr.lower())
        self.assertNotIn("Traceback", result.stderr)

    def test_wrong_digest_does_not_publish_installation(self) -> None:
        self.manifest["artifacts"][self.target]["sha256"] = "0" * 64
        self.save_manifest()
        result = self.run_command("--archive", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertRegex(result.stderr.lower(), "checksum|sha.?256|digest")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_truncated_archive_does_not_publish_installation(self) -> None:
        self.archive.write_bytes(self.archive.read_bytes()[:-20])
        result = self.run_command("--archive", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_rejects_archive_paths_that_can_escape_bundle(self) -> None:
        for name in (
            "../escaped",
            str(self.directory / "escaped"),
            "C:/escaped",
            "bin\\..\\escaped",
        ):
            with self.subTest(name=name):
                self.make_archive(
                    self.entries + [(name, b"escape", stat.S_IFREG | 0o644)]
                )
                result = self.run_command("--archive", str(self.archive))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse((self.directory / "escaped").exists())
                self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_rejects_links_special_files_and_duplicate_entries(self) -> None:
        for entry in (
            ("libexec/link", b"../../escaped", stat.S_IFLNK | 0o777),
            ("libexec/fifo", b"", stat.S_IFIFO | 0o644),
            self.entries[0],
        ):
            with self.subTest(entry=entry[0]):
                self.make_archive(self.entries + [entry])
                result = self.run_command("--archive", str(self.archive))
                self.assertNotEqual(result.returncode, 0, result.stdout)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_rejects_missing_or_nonexecutable_console(self) -> None:
        for entries in (
            self.entries[1:],
            [
                ("bin/mcp-console", self.executable, stat.S_IFREG | 0o644),
                *self.entries[1:],
            ],
        ):
            with self.subTest(entries=entries[0][0]):
                self.make_archive(entries)
                result = self.run_command("--archive", str(self.archive))
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)

    def test_concurrent_first_installs_resolve_same_complete_bundle(self) -> None:
        processes = [
            subprocess.Popen(
                self.command("--archive", str(self.archive)),
                cwd=self.directory,
                env=self.env,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            for _ in range(2)
        ]
        paths = []
        try:
            for process in processes:
                stdout, stderr = process.communicate(timeout=15)
                self.assertEqual(process.returncode, 0, stderr)
                paths.append(Path(stdout.strip()))
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate(timeout=5)
        self.assertEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_bytes(), self.executable)
        self.assertEqual(
            (paths[0].parent.parent / "libexec/mcp-console-sandbox").read_bytes(),
            self.entries[1][1],
        )

    def test_launcher_preserves_arguments_streams_and_exit_status(self) -> None:
        self.install()
        args = ("serve", "space and 'quotes'", "--exit-23")
        result = self.run_command(*args, module="_launcher", input="protocol bytes\n")
        self.assertEqual(result.returncode, 23, result.stderr)
        payload = json.loads(result.stdout)
        self.assertEqual(payload["args"], list(args))
        self.assertEqual(payload["stdin"], "protocol bytes\n")
        self.assertEqual(result.stderr, "native fixture stderr\n")

    def test_unix_launcher_executes_native_in_same_process(self) -> None:
        self.install()
        with subprocess.Popen(
            self.command("serve", module="_launcher"),
            cwd=self.directory,
            env=self.env,
            text=True,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        ) as process:
            stdout, stderr = process.communicate("input", timeout=15)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(json.loads(stdout)["pid"], process.pid)

    def test_explicit_binary_override_needs_no_manifest_or_download(self) -> None:
        native = self.directory / "custom-console"
        native.write_bytes(self.executable)
        native.chmod(0o755)
        self.env["MCP_CONSOLE_BINARY"] = str(native)
        (self.package / "_artifacts.json").unlink()
        result = self.run_command("serve", module="_launcher", input="override")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["stdin"], "override")
        self.assertFalse(self.cache.exists())

    def test_invalid_explicit_binary_does_not_fall_back_to_cache(self) -> None:
        self.install()
        for native in ("relative-console", str(self.directory / "missing")):
            with self.subTest(native=native):
                self.env["MCP_CONSOLE_BINARY"] = native
                result = self.run_command("serve", module="_launcher", input="")
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertIn("MCP_CONSOLE_BINARY", result.stderr)
                self.assertNotIn("Traceback", result.stderr)

    def test_versions_coexist_in_cache(self) -> None:
        original = self.install()
        shutil.rmtree(self.package_root / f"mcp_console-{self.version}.dist-info")
        self.version = "0.0.9"
        self.write_manifest()
        updated = self.install()
        self.assertNotEqual(original, updated)
        self.assertTrue(original.is_file())
        self.assertEqual(original.read_bytes(), updated.read_bytes())

    def test_malformed_manifest_fails_without_traceback(self) -> None:
        for content in ("{broken", "[]", '{"schema": 999}', '{"schema": 1}'):
            with self.subTest(content=content):
                (self.package / "_artifacts.json").write_text(content)
                result = self.run_command()
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, "")
                self.assertNotIn("Traceback", result.stderr)
                self.assertFalse(self.cache.exists())

    def test_missing_target_fails_before_attempting_installation(self) -> None:
        self.manifest["artifacts"] = {}
        self.save_manifest()
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(self.target, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_import_has_no_installation_side_effects(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", "import mcp_console; import mcp_console._binary"],
            cwd=self.directory,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
