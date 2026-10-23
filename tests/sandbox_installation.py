from __future__ import annotations

import argparse
import fcntl
import json
import os
import select
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.native import build_interposer


@unittest.skipUnless(sys.platform in ("darwin", "linux"), "requires macOS or Linux")
class SandboxInstallationTests(unittest.TestCase):
    binary_source: Path

    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="mcp-console-installation-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        prefix = self.root / "installation"
        (prefix / "bin").mkdir(parents=True)
        self.binary = prefix / "bin" / "mcp-console"
        shutil.copy2(self.binary_source, self.binary)
        source_prefix = self.binary_source.parent.parent
        for relative in ("libexec", "share/licenses/mcp-console"):
            shutil.copytree(source_prefix / relative, prefix / relative)
        self.home = self.root / "home"
        self.home.mkdir()
        self.path = self.root / "path"
        self.path.mkdir()
        decoy = self.path / "mcp-console-sandbox"
        decoy.write_text(
            "#!/bin/sh\nprintf 'PATH runner was used\\n'\n", encoding="utf-8"
        )
        decoy.chmod(0o755)
        self.environment = os.environ | {"PATH": str(self.path), "HOME": str(self.home)}

    @property
    def runner(self) -> Path:
        return self.binary.parent.parent / "libexec/mcp-console-sandbox"

    def run_sandbox(
        self, binary: Path | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [str(binary or self.binary), "sandbox", "--", "/usr/bin/true"],
            input=b"",
            capture_output=True,
            cwd=self.root,
            env=self.environment,
            timeout=30,
            check=False,
        )

    def test_runs_original_binary(self) -> None:
        result = self.run_sandbox(self.binary_source)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_resolves_private_runner_through_public_symlink(self) -> None:
        link = self.root / "mcp-console"
        link.symlink_to(self.binary)
        result = self.run_sandbox(link)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((result.stdout, result.stderr), (b"", b""))

    def test_setup_is_one_shot_with_idle_stdin(self) -> None:
        result = self.run_sandbox()
        self.assertEqual(result.returncode, 0, result.stderr)
        request = {
            "command": ["/bin/sh", "-c", "printf 'ready\\n'; exec /bin/cat"],
            "cwd": str(self.root.resolve()),
            "environment": {},
            "filesystem": {
                "kind": "restricted",
                "entries": [
                    {
                        "path": {"type": "special", "value": {"kind": "root"}},
                        "access": "read",
                    }
                ],
            },
            "network": "restricted",
            "proxy": None,
        }
        payload = json.dumps(request).encode()
        read, write = os.pipe()
        relocated = fcntl.fcntl(read, fcntl.F_DUPFD_CLOEXEC, 73)
        os.close(read)
        with (
            os.fdopen(relocated, "rb", buffering=0) as setup_read,
            os.fdopen(write, "wb") as setup_write,
        ):
            process = subprocess.Popen(
                [self.runner, "--bootstrap-fd", str(relocated)],
                pass_fds=(relocated,),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            setup_read.close()
            try:
                setup_write.write(len(payload).to_bytes(4, "big") + payload)
                setup_write.flush()
                # Both setup and target-input writers remain open and idle.
                ready, _, _ = select.select([process.stdout], [], [], 10)
                self.assertTrue(ready, "setup waited for EOF or consumed target stdin")
                self.assertEqual(os.read(process.stdout.fileno(), 6), b"ready\n")
                with self.assertRaises(BrokenPipeError):
                    os.write(setup_write.fileno(), b"no setup reader may remain")
                sentinel = bytes(range(256))
                stdout, stderr = process.communicate(sentinel, timeout=10)
                self.assertEqual(
                    (process.returncode, stdout, stderr), (0, sentinel, b"")
                )
            finally:
                if process.poll() is None:
                    process.stdin.close()
                    process.kill()
                    process.wait(timeout=10)
                for stream in (process.stdin, process.stdout, process.stderr):
                    stream.close()

    def test_relocates_the_bundle_without_a_writable_home_or_path_lookup(self) -> None:
        relocated = self.root / "relocated"
        self.binary.parent.parent.rename(relocated)
        self.binary = relocated / "bin/mcp-console"
        self.home.rmdir()
        self.home.touch()
        result = self.run_sandbox()
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (0, b"", b"")
        )

    def test_sandbox_cannot_write_to_the_installed_runner(self) -> None:
        marker = self.runner.parent / "modified"
        result = subprocess.run(
            [str(self.binary), "sandbox", "--", "/usr/bin/touch", str(marker)],
            capture_output=True,
            env=self.environment,
            timeout=30,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(marker.exists())

    def test_rejects_a_non_executable_private_runner(self) -> None:
        self.runner.chmod(0o644)
        result = self.run_sandbox()
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, b"")
        self.assertIn(b"private sandbox runner", result.stderr)

    def test_rejects_invalid_bundle_files(self) -> None:
        prefix = self.binary.parent.parent
        for relative in (
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
        ):
            artifact = prefix / relative
            original = artifact.read_bytes()
            for defect in ("missing", "modified", "fifo"):
                with self.subTest(artifact=relative, defect=defect):
                    artifact.unlink(missing_ok=True)
                    if defect == "modified":
                        artifact.write_bytes(original + b"\0")
                        if relative.startswith("libexec/"):
                            artifact.chmod(0o755)
                    elif defect == "fifo":
                        os.mkfifo(artifact)
                    result = self.run_sandbox()
                    self.assertNotEqual(result.returncode, 0)
                    self.assertEqual(result.stdout, b"")
                    self.assertIn(
                        b"bubblewrap"
                        if relative == "libexec/bwrap"
                        else b"private sandbox runner",
                        result.stderr,
                    )
                    if relative == "libexec/bwrap" and defect == "modified":
                        self.assertIn(b"digest mismatch", result.stderr)
                    if defect == "fifo" and relative != "libexec/bwrap":
                        self.assertIn(
                            b"private artifact is not a readable file or executable",
                            result.stderr,
                        )
            artifact.unlink()
            artifact.write_bytes(original)
            artifact.chmod(0o755 if relative.startswith("libexec/") else 0o644)

    @unittest.skipUnless(sys.platform == "linux", "requires Linux helper selection")
    def test_unused_bundled_helper_does_not_block_a_suitable_host_helper(self) -> None:
        # Native discovery excludes helpers inside cwd. Supply a trusted helper
        # outside that tree independently of the relocated bundle we damage.
        host = tempfile.TemporaryDirectory(prefix="mcp-console-host-helper-")
        self.addCleanup(host.cleanup)
        helper = Path(host.name) / "bwrap"
        shutil.copy2(self.binary_source.parent.parent / "libexec/bwrap", helper)
        (self.path / "bwrap").symlink_to(helper)
        bundled = self.binary.parent.parent / "libexec/bwrap"
        for defect in ("missing", "modified", "fifo"):
            with self.subTest(defect=defect):
                bundled.unlink(missing_ok=True)
                if defect == "modified":
                    bundled.write_bytes(b"modified")
                    bundled.chmod(0o755)
                elif defect == "fifo":
                    os.mkfifo(bundled)
                result = self.run_sandbox()
                self.assertEqual(
                    (result.returncode, result.stdout, result.stderr), (0, b"", b"")
                )

    @unittest.skipUnless(
        sys.platform == "darwin", "requires the macOS allocator interposer"
    )
    def test_first_and_repeated_launches_use_bounded_allocations(self) -> None:
        interposer = build_interposer(self.root, "bounded_allocation")
        self.environment["DYLD_INSERT_LIBRARIES"] = str(interposer)
        for launch in ("first", "repeated"):
            with self.subTest(launch=launch):
                result = self.run_sandbox()
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("binary", type=Path)
    args = parser.parse_args()
    SandboxInstallationTests.binary_source = args.binary.resolve()
    unittest.main(argv=[sys.argv[0]])
