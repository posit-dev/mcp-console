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
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import unittest
import warnings
import zipfile

from support.normalization import code
from support.python import python_script_header


ROOT = Path(__file__).resolve().parent.parent
WINDOWS = sys.platform == "win32"
FLOOR = {"darwin": "minimum_macos", "linux": "minimum_glibc"}.get(sys.platform)


class BootstrapCase(unittest.TestCase):
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
        system = {"darwin": "apple-darwin", "win32": "pc-windows-msvc"}.get(
            sys.platform, "unknown-linux-gnu"
        )
        self.target = f"{machine}-{system}"
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
        self.native = "libexec/mcp-console.exe" if WINDOWS else "bin/mcp-console"
        self.entries = [
            (self.native, self.executable, stat.S_IFREG | 0o755),
            (
                "libexec/mcp-console-sandbox" + (".exe" if WINDOWS else ""),
                b"companion fixture",
                stat.S_IFREG | 0o755,
            ),
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
            "version": self.version,
            "artifacts": {
                self.target: {
                    "sha256": hashlib.sha256(content).hexdigest(),
                    "size": len(content),
                }
                | ({FLOOR: "1.0"} if FLOOR else {})
            },
        }
        self.save_manifest()

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


class InstallerTests(BootstrapCase):
    def test_installs_complete_bundle_from_pinned_local_archive(self) -> None:
        native = self.install()
        prefix = native.parent.parent
        self.assertEqual(native, prefix / self.native)
        for relative, content, mode in self.entries:
            installed = prefix / relative
            self.assertEqual(installed.read_bytes(), content)
            if not WINDOWS:
                self.assertEqual(
                    stat.S_IMODE(installed.stat().st_mode), stat.S_IMODE(mode)
                )
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
        self.assertIn("SHA-256", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_truncated_archive_does_not_publish_installation(self) -> None:
        self.archive.write_bytes(self.archive.read_bytes()[:-20])
        result = self.run_command("--archive", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.run_command("--print-path").returncode, 0)

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

    def test_versions_coexist_in_cache(self) -> None:
        original = self.install()
        self.version = "0.0.9"
        self.write_manifest()
        updated = self.install()
        self.assertNotEqual(original, updated)
        self.assertTrue(original.is_file())
        self.assertEqual(original.read_bytes(), updated.read_bytes())

    def test_unsupported_target_fails_before_attempting_installation(self) -> None:
        self.manifest["artifacts"] = {}
        self.save_manifest()
        result = self.run_command()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(self.target, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.cache.exists())

    @unittest.skipUnless(FLOOR, "Windows wheels never carried an OS version floor")
    def test_host_below_native_floor_fails_before_attempting_installation(self) -> None:
        self.manifest["artifacts"][self.target][FLOOR] = "9999.0"
        self.save_manifest()
        result = self.run_command("--archive", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("9999.0 or newer", result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertFalse(self.cache.exists())

    def test_relative_cache_override_is_rejected(self) -> None:
        self.env["MCP_CONSOLE_BINARY_CACHE"] = "relative-cache"
        result = self.run_command("--archive", str(self.archive))
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("MCP_CONSOLE_BINARY_CACHE", result.stderr)
        self.assertFalse((self.directory / "relative-cache").exists())

    def test_failed_download_names_the_pinned_release_asset(self) -> None:
        # No fixture can serve github.com over HTTPS. A refusing local proxy
        # shows which asset the installer requests and how a failure reads.
        requests = []
        with socket.create_server(("127.0.0.1", 0)) as proxy:
            proxy.settimeout(15)

            def refuse() -> None:
                connection, _ = proxy.accept()
                with connection:
                    requests.append(connection.recv(4096).split(b"\r\n")[0])
                    connection.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")

            thread = threading.Thread(target=refuse)
            thread.start()
            address = f"http://127.0.0.1:{proxy.getsockname()[1]}"
            for name in ("MCP_CONSOLE_OFFLINE", "NO_PROXY", "no_proxy"):
                self.env.pop(name, None)
            self.env |= {"HTTPS_PROXY": address, "https_proxy": address}
            result = self.run_command()
            thread.join()
        self.assertEqual([request.split()[:2] for request in requests], [[b"CONNECT", b"github.com:443"]])
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(
            "https://github.com/posit-dev/mcp-console/releases/download/"
            f"v{self.version}/mcp-console-{self.version}-{self.target}.zip",
            result.stderr,
        )
        self.assertNotIn("Traceback", result.stderr)
        self.assertNotEqual(self.run_command("--print-path").returncode, 0)

    def test_installer_ignores_explicit_binary(self) -> None:
        self.env["MCP_CONSOLE_BINARY"] = str(self.directory / "custom-console")
        self.assertTrue(self.install().is_relative_to(self.cache))

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


@unittest.skipUnless(
    sys.platform in {"darwin", "linux"},
    "the native fixture is a POSIX script; a Windows launch needs a real executable",
)
class LauncherTests(BootstrapCase):
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

    def test_explicit_binary_override_ignores_pinned_bundle(self) -> None:
        native = self.directory / "custom-console"
        native.write_bytes(self.executable)
        native.chmod(0o755)
        self.env["MCP_CONSOLE_BINARY"] = str(native)
        result = self.run_command("serve", module="_launcher", input="override")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)["stdin"], "override")
        self.assertFalse(self.cache.exists())

    def test_missing_explicit_binary_does_not_fall_back_to_cache(self) -> None:
        self.install()
        missing = str(self.directory / "missing")
        self.env["MCP_CONSOLE_BINARY"] = missing
        result = self.run_command("serve", module="_launcher", input="")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn(missing, result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_explicit_binary_cannot_name_the_launcher(self) -> None:
        launcher = self.directory / "mcp-console"
        launcher.write_text(
            python_script_header()
            + code("""
                from mcp_console._launcher import main

                raise SystemExit(main())
                """)
        )
        launcher.chmod(0o755)
        self.env["MCP_CONSOLE_BINARY"] = str(launcher)
        result = subprocess.run(
            [launcher, "serve"],
            cwd=self.directory,
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "")
        self.assertIn("MCP_CONSOLE_BINARY", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
